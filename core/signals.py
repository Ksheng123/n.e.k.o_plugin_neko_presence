"""Windows 系统信号采集 —— 纯标准库 ctypes，零 SDK 依赖。

四个信号，彼此独立，任何一个不可用都不影响其他的返回：

    idle_seconds()        距最后一次键盘/鼠标输入的秒数
    notification_state()  通知/全屏/锁屏状态码
    foreground_exe()      前台窗口的进程名（仅文件名）
    monotonic_now()       单调时钟读数

设计约束
--------
1. **绝不抛异常**：失败一律返回 ``None``（或空串），由调用方决定怎么降级。
   这个插件是长期后台运行的，任何一次 API 调用失败都不该让采样链断掉。
2. **隐私白名单**（见 DESIGN.md §10）：只读键鼠时间戳、窗口状态、进程名。
   不读屏幕像素、不读窗口标题、不读键盘内容 —— 这三样才是最敏感的内容。
3. **不 import ``plugin.sdk``**：本模块要能脱离宿主单独运行，
   这样改判定逻辑时可以直接在本机跑测试，不用起宿主。
"""

from __future__ import annotations

import os
import time

try:  # 非 Windows 或运行环境缺少 ctypes 时整块降级
    import ctypes
    from ctypes import wintypes

    _HAS_CTYPES = True
except Exception:  # noqa: BLE001 - 见模块 docstring 第 1 条
    ctypes = None  # type: ignore[assignment]
    wintypes = None  # type: ignore[assignment]
    _HAS_CTYPES = False


# QUNS 状态码（SHQueryUserNotificationState 的返回值）。
# 名字只用于日志和调试可读性，判定逻辑用数字。
QUNS_NOT_PRESENT = 1       # 锁屏 / 屏保
QUNS_BUSY = 2              # 全屏应用运行
QUNS_D3D_FULLSCREEN = 3    # 全屏 D3D（游戏）
QUNS_PRESENTATION = 4      # 演示模式
QUNS_NORMAL = 5            # 普通桌面
QUNS_QUIET_TIME = 6        # 刚进全屏的几秒

_FULLSCREEN_STATES = frozenset({QUNS_BUSY, QUNS_D3D_FULLSCREEN, QUNS_QUIET_TIME})


def has_ctypes() -> bool:
    """当前环境是否具备调用 Windows API 的能力。"""
    return _HAS_CTYPES


def monotonic_now() -> float:
    """单调时钟读数（秒）。不受系统时间修改影响，重启后归零。"""
    return time.monotonic()


def wall_now() -> float:
    """墙钟读数（Unix 时间戳）。能被用户改时间/NTP 校正影响，但跨重启连续。"""
    return time.time()


def idle_seconds() -> float | None:
    """距最后一次键盘/鼠标输入的秒数。

    实现要点（``GetLastInputInfo`` + ``GetTickCount``）：

    - ``info.dwTime`` 是**最后一次输入那一刻**的系统 tick（系统每次输入时刷新它）
    - ``GetTickCount()`` 是**此刻**的系统 tick（一直在涨）
    - 两者相减 = 已经安静了多久

    所以本函数返回的是"从现在往回看"的瞬时值，每次现算、不是累加。
    插件重启不影响它，因为基准在系统侧；但**系统重启会让 tick 归零**。

    精度：两个 32 位毫秒数直接相减，误差约 1ms，且**与空闲时长无关**
    （空闲 4 小时和空闲 0.5 秒的精度一样）。
    """
    if not _HAS_CTYPES:
        return None
    try:
        class _LastInputInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(_LastInputInfo)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        # GetTickCount 与 dwTime 都是 32 位、约 49.7 天回绕；
        # 掩码相减可自然处理回绕，不需要 64 位版本
        elapsed_ms = (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
        return round(elapsed_ms / 1000.0, 1)
    except Exception:  # noqa: BLE001 - 见模块 docstring 第 1 条
        return None


def notification_state() -> int | None:
    """``SHQueryUserNotificationState`` 的状态码。

    判定里最有用的三个：1 = 锁屏（人确定不在）、2/3 = 全屏（可能在消费内容）、
    5 = 普通桌面。已实测切换滞后不超过一个采样间隔。
    """
    if not _HAS_CTYPES:
        return None
    try:
        state = ctypes.c_int()
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return None
        return state.value
    except Exception:  # noqa: BLE001
        return None


def is_locked(state: int | None) -> bool:
    return state == QUNS_NOT_PRESENT


def is_fullscreen(state: int | None) -> bool:
    return state in _FULLSCREEN_STATES


def foreground_exe() -> str:
    """前台窗口的进程名（小写、仅文件名，不含路径）。

    只取 exe 名是有意的：窗口标题里常有网页标题、文件名、私信内容，
    那才是真正的隐私内容；进程名只到"你在用浏览器/播放器"这一层。

    重要限制：**人离开后这个值会停在最后一个前台应用上**，
    它不反映"此刻在做什么"，只反映"最后切换到的窗口是谁"。
    """
    if not _HAS_CTYPES:
        return ""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        # PROCESS_QUERY_LIMITED_INFORMATION：拿到路径所需的最小权限
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(512)
            buf = ctypes.create_unicode_buffer(512)
            if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size)
            ):
                return ""
            return os.path.basename(buf.value).lower()
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return ""
