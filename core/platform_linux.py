"""Linux 原始信号读取 —— X11（libX11 + libXss）与 Wayland（systemd-logind D-Bus）。

本模块是 ``core/signals.py`` 的 Linux 对应物：只回答两个**物理问题**，
不产生任何业务语义。语义归一在 ``core/adapters.py`` 的 ``LinuxAdapter`` 里做。

    两个问题
    --------
    idle_sec   距最后一次键鼠输入的秒数（系统自己算好的标量）
    locked     屏幕是否处于「要解锁才能回来」的状态

隐私白名单（与 Windows 侧同一条底线，见 DESIGN.md §10）
------------------------------------------------------
只读「空闲时长」和「锁定标志」这两个标量。本模块里**没有**、也不应该有：

    evdev / ``/dev/input/*`` 读取、XI2 raw 事件、XRecord、uinput、
    XGrabKeyboard 之类的抓取、窗口标题、进程列表、屏幕像素。

这些路径一旦出现，插件就从「知道你在不在」变成「知道你在干什么」。
这不是「暂时没写」，是设计红线：需求里那句「不记录键鼠内容」靠的是
**根本没有读输入的代码**，而不是靠自觉不去用。

降级链
------
Wayland（niri / GNOME / KDE / sway …）
    ``loginctl show-session``  ← 首选：给出 ``IdleSinceHintMonotonic``，直接算秒数
    ``busctl ... GetAll``      ← loginctl 不在、或会话 id 解析失败时
    → 都拿不到就返回「不知道」，由 ``LinuxAdapter`` 退到宿主快照

X11
    ``libXss``（ctypes）       ← 首选：不开子进程，精度 ~10ms
    ``xprintidle``             ← libXss 缺失时的降级（x11-utils 包）

⚠️ Wayland 会话下**不要**去问 X11。XWayland 的输入事件是合成出来的，
``XScreenSaverQueryInfo`` 只会报一个偏小甚至恒为 0 的 idle —— 那不是
「你一直在动」，而是「这条信号测的不是你」。见 :func:`detect_session`。

线程模型
--------
所有 ``read()`` 都是同步的，**只在采样协程里调用**（每 30 秒一次）。
外部命令超时压到 ``CMD_TIMEOUT_SEC``，所以在事件循环里同步跑几毫秒是
完全可以接受的代价 —— 换来的是不需要 ``asyncio.to_thread``、也不需要
为缓存的 X Display 句柄考虑跨线程安全。别在多线程里同时调 ``read()``。
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Protocol

# ---- 会话类型 ----
SESSION_WAYLAND = "wayland"
SESSION_X11 = "x11"
SESSION_HEADLESS = "headless"  # tty / 无显示：没有可信的空闲来源

# ---- 后端名（进 Probed.backend，最终写进探针的 ``src``）----
BACKEND_LOGIND = "logind"
BACKEND_X11 = "x11"
BACKEND_NONE = "none"

#: 外部命令超时（秒）。刻意远小于采样间隔（30s），又宽于正常耗时（~10ms）。
#: 卡住的 D-Bus 不该把采样链拖死，超时就当「这一路没信号」。
CMD_TIMEOUT_SEC = 0.8

#: 单个 idle 值的上限（秒）。超过它一定是布局/时钟对不上，不是真的空闲了 100 年。
MAX_PLAUSIBLE_IDLE_SEC = 366 * 24 * 3600.0


@dataclass(frozen=True)
class Probed:
    """一次原始探测的结果。

    ``None`` 一律表示**不知道**，不等于 0 / 不等于 False：
    空闲 0 秒是「刚动过」，锁屏 False 是「确定没锁」，两者和「问不到」
    在业务层要走不同分支（见 ``Signal.usable``）。
    """

    idle_sec: float | None = None
    locked: bool | None = None
    session: str = ""
    backend: str = BACKEND_NONE
    detail: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        """是否至少拿到一个可信字段。"""
        return self.idle_sec is not None or self.locked is not None


class Backend(Protocol):
    """平台后端的最小接口 —— 上层只要这两句话就能工作。

    需求点名的 ``get_idle_time_seconds()`` / ``is_screen_locked()`` 由
    :class:`_Backend` 在这层之上提供；内部热路径统一走 ``read()``，
    一次拿全两个字段，省掉一次多余的子进程/D-Bus 往返。
    """

    name: str

    def read(self) -> Probed: ...


# ==========================================================
# 通用小工具
# ==========================================================


def _run(argv: list[str], timeout: float = CMD_TIMEOUT_SEC) -> str | None:
    """跑一条外部命令，成功返回 stdout，任何失败返回 ``None``。

    与 ``core/signals.py`` 同一条纪律：**绝不抛异常**。这里尤其重要 ——
    logind / busctl 在容器、无 systemd 的发行版、权限受限的会话里都可能
    直接不存在，那属于正常环境差异，不是错误。
    """
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


_TRUE_WORDS = frozenset({"yes", "true", "1", "y", "on"})
_FALSE_WORDS = frozenset({"no", "false", "0", "n", "off"})


def _truthy(raw: object) -> bool | None:
    """``yes/no``（loginctl）与 ``true/false``（busctl）统一成 ``bool``。

    认不出来就返回 ``None``，不猜。
    """
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return None


def _as_int(raw: object) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


_LOGINCTL_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9]*)=(.*)$")
#: busctl 把 variant 打印成 ``"IdleHint": <b true>``：先类型字符，再值。
_BUSCTL_VARIANT = re.compile(r'"([A-Za-z][A-Za-z0-9]*)":\s*<(.)\s*([^>]*)>')


def parse_props(text: str) -> dict[str, str]:
    """把 loginctl 的 ``Key=value`` 或 busctl 的 ``a{sv}`` 打印结果统一成字典。

    两种格式都只留字符串，``bool``/``int`` 的解释交给 :func:`idle_from_props`，
    这样「yes / true / 1」三种写法只需要在一处兼容。

    解析失败返回空字典 —— 调用方据此降级，不抛。
    """
    if not text:
        return {}
    if _BUSCTL_VARIANT.search(text):
        return {m.group(1): m.group(3).strip() for m in _BUSCTL_VARIANT.finditer(text)}
    props: dict[str, str] = {}
    for line in text.splitlines():
        match = _LOGINCTL_LINE.match(line.strip())
        if match:
            props[match.group(1)] = match.group(2).strip()
    return props


def idle_from_props(props: dict[str, str], now: float | None = None) -> float | None:
    """从 logind 属性算出「距最后一次输入的秒数」。

    三条判据缺一不可，每一条都对应一种真实故障：

    1. ``IdleHint`` 缺失 → ``None``。这个会话根本没有 idle 追踪
       （裸 tty、容器、部分 compositor）。报 0 会变成「你一直在电脑前」。
    2. ``IdleHint == false`` → ``0.0``。这是 logind 的正面回答：此刻不空闲。
    3. ``IdleHint == true`` 但 ``IdleSinceHintMonotonic`` 为 0/缺失 →
       ``None``。知道「已经空闲」却不知道「空闲了多久」，照样没法判定。

    ``IdleSinceHintMonotonic`` 是 **CLOCK_MONOTONIC 微秒**，与 Python 的
    ``time.monotonic()`` 同一个时钟，所以能直接相减：不受改系统时间、
    NTP 校正影响，也是唯一能跨观测点给出绝对空闲时长的正确算法
    （``IdleHint`` 只是个布尔，测不出「多久」）。

    ⚠️ 第 2 条成立的前提是「有人在维护这个 hint」。niri 这类不自报 idle 的
    compositor 会让 ``IdleHint`` 长期停在 false —— 本函数如实返回 ``0.0``，
    由 ``LinuxAdapter`` 用宿主快照的空闲秒数取较大值兜底，
    ``tools/check_linux_signals.py`` 能把这种情况直接显出来。
    """
    hint = _truthy(props.get("IdleHint"))
    if hint is None:
        return None
    if not hint:
        return 0.0
    since_us = _as_int(props.get("IdleSinceHintMonotonic"))
    if since_us is None or since_us <= 0:
        return None
    clock = time.monotonic() if now is None else float(now)
    idle = clock - since_us / 1_000_000.0
    # hint 说「空闲」但算出的时长离谱 → 时钟基准对不上，宁可不报
    if idle < 0 or idle > MAX_PLAUSIBLE_IDLE_SEC:
        return None
    return round(idle, 1)


class _Backend:
    """给 :class:`Backend` 补上需求点名的两个单字段查询。

    单独问一个字段时各读一次 —— 会多一次子进程往返，所以业务热路径
    用的是 ``read()``，不是这两个。它们存在的意义是让「平台接口」这件事
    对上需求里的形状，也方便排障脚本和上层按需取。
    """

    name: str = BACKEND_NONE

    def read(self) -> Probed:  # pragma: no cover - 由子类覆盖
        return Probed(backend=self.name)

    def get_idle_time_seconds(self) -> float | None:
        """距最后一次键鼠输入的秒数；``None`` = 这一路不可用。"""
        return self.read().idle_sec

    def is_screen_locked(self) -> bool | None:
        """是否锁屏；``None`` = 不知道（不是「没锁」）。"""
        return self.read().locked


# ==========================================================
# Wayland / 通用：systemd-logind
# ==========================================================

_LOGIN1_NAME = "org.freedesktop.login1"
_SESSION_SELF = "/org/freedesktop/login1/session/self"
_SESSION_IFACE = "org.freedesktop.login1.Session"
_PROPS_IFACE = "org.freedesktop.DBus.Properties"

#: 只取需要的四个属性：IdleHint（在不在空闲）、IdleSinceHintMonotonic（空闲了多久）、
#: LockedHint（锁没锁）、Type（会话类型，排障用）。
_LOGIND_WANTED = (
    "-p",
    "IdleHint",
    "-p",
    "IdleSinceHintMonotonic",
    "-p",
    "LockedHint",
)


def _logind_props() -> tuple[dict[str, str], str]:
    """读 logind 会话属性，返回 ``(属性字典, 来源说明)``。

    两条路，都只依赖 systemd 自带的命令行：

    1. ``loginctl show-session <id> -p ...`` —— 首选。它替我们做会话 id 解析
       （``$XDG_SESSION_ID`` 在多数会话里都有，没有就用字面量 ``self``），
       输出是最好解析的 ``Key=value``。
    2. ``busctl call ... GetAll`` —— loginctl 不在、或 ``self`` 解析失败时。
       logind 在系统总线上，所以**不加** ``--user``。

    失败返回 ``({}, "")``。
    """
    sid = (os.environ.get("XDG_SESSION_ID") or "self").strip()
    out = _run(["loginctl", "show-session", sid, *_LOGIND_WANTED])
    if out:
        props = parse_props(out)
        if "IdleHint" in props or "LockedHint" in props:
            return props, f"loginctl {sid}"
    out = _run(
        [
            "busctl",
            "call",
            _LOGIN1_NAME,
            _SESSION_SELF,
            _PROPS_IFACE,
            "GetAll",
            "s",
            _SESSION_IFACE,
        ]
    )
    if out:
        props = parse_props(out)
        if props:
            return props, "busctl GetAll"
    return {}, ""


def _logind_locked_hint() -> bool | None:
    props, _ = _logind_props()
    return _truthy(props.get("LockedHint"))


class LogindBackend(_Backend):
    """Wayland 首选后端：systemd-logind 的 ``IdleHint`` / ``LockedHint``。

    为什么不碰 compositor 的私有协议：``swaymsg``、``niri msg``、
    ``hyprctl`` 各说各话且都没承诺 ABI 稳定；logind 是 systemd 的公开
    D-Bus 接口，GNOME / KDE 都在往里写，是唯一「跨 compositor 同一个形状」
    的东西。代价是要看 compositor 的脸色：它不报 idle，这里就没有秒数 ——
    这种时候如实返回 ``None``，而不是编一个 0。
    """

    name = BACKEND_LOGIND

    def read(self) -> Probed:
        props, how = _logind_props()
        if not props:
            return Probed(
                session=SESSION_WAYLAND,
                backend=self.name,
                detail=("logind 属性读取失败（loginctl/busctl 都不可用）",),
            )
        idle = idle_from_props(props)
        detail = [f"via {how}"]
        if idle is None:
            detail.append("IdleHint 未上报，取不到空闲时长")
        return Probed(
            idle_sec=idle,
            locked=_truthy(props.get("LockedHint")),
            session=SESSION_WAYLAND,
            backend=self.name,
            detail=tuple(detail),
        )


# ==========================================================
# X11：libX11 + libXss（ctypes），xprintidle 兜底
# ==========================================================

# XScreenSaverInfo.state 的取值（X11/extensions/scrnsaver.h）。
# ScreenSaverOn = 屏保已激活；>= Blanked 表示屏幕已黑（DPMS off 之类）。
_SS_STATE_OFF = 0
_SS_STATE_ON = 1
_SS_STATE_DISABLED = 2
_SS_STATE_BLANKED = 3


class _XScreenSaverInfo(ctypes.Structure):
    """``XScreenSaverInfo``。

    字段顺序/类型必须与 X11/extensions/scrnsaver.h 逐字节一致，否则读到的
    是相邻字段的垃圾值。C 里最后那个 ``unsigned long idle``（毫秒）就是
    我们要的东西；前面几个 ``Pixmap`` 字段在协议上是历史遗留，没人用。

    64 位下 sizeof = 48（8 + 4 + 4 padding + 8*4）。
    """

    _fields_ = [
        ("window", ctypes.c_ulong),
        ("state", ctypes.c_int),
        ("til_or_since", ctypes.c_ulong),
        ("idle_pixmap", ctypes.c_ulong),
        ("event_mask", ctypes.c_ulong),
        ("idle_ms", ctypes.c_ulong),
    ]


class _X11:
    """libX11 / libXss 句柄缓存。

    Display 只连接一次并长期复用：``XOpenDisplay`` + 握手要几毫秒，
    每 30 秒开一次纯属浪费。X server 重启（登出再进来）后句柄会失效，
    :meth:`query` 检测到失败就关掉，下次自动重连。

    库名用 ``libX11.so.6`` / ``libXss.so.1``（soname）：Linux 上没有不带
    版本号的 ``libXss.so``（那只有 ``-dev`` 包才装），写 soname 才不用装开发包。
    """

    def __init__(self) -> None:
        self._xlib: ctypes.CDLL | None = None
        self._xss: ctypes.CDLL | None = None
        self._dpy: int | None = None
        self._root: int = 0
        self._unavailable = False  # 库本身不存在：一次失败就永久放弃

    def _open(self) -> bool:
        if self._dpy is not None:
            return True
        if self._unavailable:
            return False
        try:
            xlib = ctypes.CDLL("libX11.so.6")
            xss = ctypes.CDLL("libXss.so.1")
        except OSError:
            self._unavailable = True
            return False

        xlib.XOpenDisplay.restype = ctypes.c_void_p
        xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        xlib.XCloseDisplay.restype = ctypes.c_int
        xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        xlib.XDefaultRootWindow.restype = ctypes.c_ulong
        xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        xss.XScreenSaverQueryExtension.restype = ctypes.c_int
        xss.XScreenSaverQueryExtension.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        xss.XScreenSaverQueryInfo.restype = ctypes.c_int
        xss.XScreenSaverQueryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(_XScreenSaverInfo),
        ]

        dpy = xlib.XOpenDisplay(None)
        if not dpy:
            self._unavailable = True
            return False
        event_base = ctypes.c_int()
        error_base = ctypes.c_int()
        if not xss.XScreenSaverQueryExtension(
            dpy, ctypes.byref(event_base), ctypes.byref(error_base)
        ):
            # X server 没加载 XScreenSaver 扩展（少见）→ 关连接，走 xprintidle
            xlib.XCloseDisplay(dpy)
            self._unavailable = True
            return False

        self._xlib, self._xss, self._dpy = xlib, xss, dpy
        self._root = xlib.XDefaultRootWindow(dpy)
        return True

    def _close(self) -> None:
        if self._dpy is not None and self._xlib is not None:
            self._xlib.XCloseDisplay(self._dpy)
        self._dpy = None
        self._root = 0

    def query(self) -> _XScreenSaverInfo | None:
        """查一次屏保信息；拿不到返回 ``None``（不抛）。"""
        if not self._open():
            return None
        info = _XScreenSaverInfo()
        info.state = -1
        if not self._xss.XScreenSaverQueryInfo(self._dpy, self._root, ctypes.byref(info)):
            # 连接已断（X server 重启）：关掉，下次重连
            self._close()
            return None
        return info


def _busctl_screensaver_active() -> bool | None:
    """``org.freedesktop.ScreenSaver.GetActive`` —— GNOME/KDE/XFCE/xscreensaver 的锁屏标志。

    这个接口在**会话总线**上，所以 ``busctl`` 必须带 ``--user``
    （它默认连系统总线，不带就永远报找不到服务）。
    """
    out = _run(
        [
            "busctl",
            "--user",
            "call",
            "org.freedesktop.ScreenSaver",
            "/org/freedesktop/ScreenSaver",
            "org.freedesktop.ScreenSaver",
            "GetActive",
        ]
    )
    if not out:
        return None
    return _truthy(out.strip())


def _xprintidle() -> float | None:
    """``xprintidle`` 降级：输出空闲毫秒数（x11-utils 包，非默认安装）。"""
    out = _run(["xprintidle"])
    if not out:
        return None
    millis = _as_int(out.strip())
    return None if millis is None else round(millis / 1000.0, 1)


class X11Backend(_Backend):
    """X11 后端：``XScreenSaverQueryInfo`` 拿空闲毫秒，再判锁屏。

    锁屏在 X11 上**没有单一权威 API**（这正是 Wayland 那边改用 logind 的理由），
    所以按可信度往下问：

    1. ``state == ScreenSaverOn``：屏保已激活。xscreensaver / GNOME / XFCE 下
       屏保激活即锁屏（只要开了锁）—— 最廉价，不用开子进程。
    2. ``org.freedesktop.ScreenSaver.GetActive``：桌面环境自己维护的锁屏标志。
    3. logind ``LockedHint``：由 locker 或 xss-lock 写进去的。

    三路都没有 → ``None``（「不知道」）。特别注意 **i3lock / slock 这类
    直接抢键盘的锁屏器**：既不设屏保状态，也不实现 ScreenSaver 接口，
    这时锁屏判据只能交给空闲时长（``w`` 一直涨 → 判 away），而不是
    返回 ``False`` 假装确定没锁。
    """

    name = BACKEND_X11

    def __init__(self) -> None:
        self._x = _X11()
        #: 结构体布局是否已被 xprintidle 对账确认。None = 还没对过账。
        self._layout_ok: bool | None = None

    def read(self) -> Probed:
        detail: list[str] = []
        info = self._x.query()

        # ---- 锁屏：先看屏保状态，问不到再往下退 ----
        if info is not None and int(info.state) == _SS_STATE_ON:
            detail.append("screensaver on")
            locked: bool | None = True
        else:
            if info is not None:
                # 屏黑 ≠ 锁屏（可能只是 DPMS 灭屏、压根没设锁）。只记进 detail，
                # 「人走没走」继续交给 idle —— 那是更准的判据。
                if int(info.state) >= _SS_STATE_BLANKED:
                    detail.append("blanked")
                elif int(info.state) == _SS_STATE_DISABLED:
                    detail.append("screensaver disabled")
            locked = self._locked(detail)

        # ---- 空闲时长：ctypes 优先，取不到就退 xprintidle ----
        idle = self._idle_from(info, detail) if info is not None else None
        if idle is None:
            idle = _xprintidle()
            if idle is not None:
                detail.append("via xprintidle")
            elif info is None:
                return Probed(
                    session=SESSION_X11,
                    backend=self.name,
                    detail=("libXss 与 xprintidle 都不可用",),
                )

        return Probed(
            idle_sec=idle,
            locked=locked,
            session=SESSION_X11,
            backend=self.name,
            detail=tuple(detail),
        )

    def _idle_from(self, info: _XScreenSaverInfo, detail: list[str]) -> float | None:
        """从结构体里取空闲毫秒，并在首次使用时验证布局。"""
        state = int(info.state)
        # 取值域检查：state 只可能是 0..4。越界说明字段对齐错了 —— 这是唯一
        # 能在不依赖头文件的情况下自证「读到的确实不是别的字段」的办法。
        if not 0 <= state <= 4:
            detail.append(f"state 越界({state})，XScreenSaverInfo 布局不匹配")
            return None
        millis = int(info.idle_ms)
        # 布局错位时会读到一个指针/窗口 id（几十位），比任何真实空闲都大
        if not 0 <= millis <= 2**32:
            detail.append("idle 值异常，XScreenSaverInfo 布局不匹配")
            return None

        secs = round(millis / 1000.0, 1)
        if self._layout_ok is None:
            self._layout_ok = self._cross_check(secs, detail)
        return secs if self._layout_ok else None

    @staticmethod
    def _cross_check(secs: float, detail: list[str]) -> bool:
        """首次使用时拿 ``xprintidle`` 对一次账，确认 ctypes 布局抄对了。

        为什么必须对账：``XScreenSaverInfo`` 的字段顺序只有 xorg 头文件能保证，
        而这里是用 ctypes 手抄的。抄错一格**不会崩，只会静默读到一个看起来
        合理的错值** —— 那是最难查的一类 bug。``xprintidle`` 由真头文件编译，
        所以它就是标准答案。

        - 相差 ≤2s → 布局可信，之后不再对账（省掉每次的子进程）
        - 差得多   → 本实例永久弃用 ctypes 路径，改用 xprintidle
        - xprintidle 缺席 → 无从对账，只能依赖上面的取值域检查
        """
        probe = _xprintidle()
        if probe is None:
            detail.append("xprintidle 缺席，布局未经对账")
            return True
        if abs(probe - secs) <= 2.0:
            detail.append("layout ok")
            return True
        detail.append(f"layout 不符(ctypes {secs:.0f}s / xprintidle {probe:.0f}s)，改用 xprintidle")
        return False

    @staticmethod
    def _locked(detail: list[str]) -> bool | None:
        active = _busctl_screensaver_active()
        if active is not None:
            detail.append("freedesktop.ScreenSaver")
            return active
        hint = _logind_locked_hint()
        if hint is not None:
            detail.append("logind LockedHint")
            return hint
        return None


class _NoBackend(_Backend):
    """没有可信来源（裸 tty / 无 systemd / 被显式关掉）。"""

    name = BACKEND_NONE

    def read(self) -> Probed:
        return Probed(
            session=SESSION_HEADLESS,
            backend=BACKEND_NONE,
            detail=("没有可用的空闲/锁屏来源",),
        )


# ==========================================================
# 选择后端
# ==========================================================


def detect_session(env: dict[str, str] | None = None) -> str:
    """判断当前图形会话类型：``wayland`` / ``x11`` / ``headless``。

    ⚠️ **判定顺序是这里唯一的坑**：Wayland 会话也会为 XWayland 导出
    ``DISPLAY``，所以 ``DISPLAY`` 必须放在最后判，否则 niri 这类机器会被
    误判成 X11，然后拿 XWayland 的假 idle 当真值。

    顺序：``WAYLAND_DISPLAY`` → ``XDG_SESSION_TYPE`` → ``DISPLAY``。
    """
    env = os.environ if env is None else env
    if env.get("WAYLAND_DISPLAY"):
        return SESSION_WAYLAND
    kind = (env.get("XDG_SESSION_TYPE") or "").strip().lower()
    if kind in (SESSION_WAYLAND, SESSION_X11):
        return kind
    if kind == "mir":  # Mir 也是 Wayland 一类，没有 X11 idle 可言
        return SESSION_WAYLAND
    if env.get("DISPLAY"):
        return SESSION_X11
    return SESSION_HEADLESS


def pick_backend(session: str | None = None) -> Backend:
    """按会话类型挑后端。

    环境变量 ``NEKO_LINUX_BACKEND`` 可强制指定，用于排障：

        auto（默认） / ``x11`` / ``logind`` / ``none``

    ``x11`` 在 Wayland 会话下也能强制打开（比如确实想拿 XWayland 的 idle
    做对照），但这一定是排障用法，不是生产配置。
    """
    session = detect_session() if session is None else session
    forced = (os.environ.get("NEKO_LINUX_BACKEND") or "auto").strip().lower()
    if forced == BACKEND_X11:
        return X11Backend()
    if forced == BACKEND_LOGIND:
        return LogindBackend()
    if forced == BACKEND_NONE:
        return _NoBackend()
    if session == SESSION_X11:
        return X11Backend()
    if session == SESSION_WAYLAND:
        return LogindBackend()
    return _NoBackend()


def probe() -> tuple[Backend, Probed]:
    """一次性探测：``(后端, 结果)``。给排障脚本用。"""
    backend = pick_backend()
    return backend, backend.read()


def describe(probed: Probed) -> str:
    """把一次探测压成一行，写日志用。"""
    idle = "?" if probed.idle_sec is None else f"{probed.idle_sec:.0f}s"
    locked = {True: "locked", False: "unlocked", None: "lock?"}[probed.locked]
    extra = f" ({', '.join(probed.detail)})" if probed.detail else ""
    return f"{probed.session}/{probed.backend} idle={idle} {locked}{extra}"