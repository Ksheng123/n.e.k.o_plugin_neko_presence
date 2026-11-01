"""平台适配层 —— 唯一允许感知「设备 / 操作系统」的层。

依据《Agent 插件跨端输入适配方案：中断处理与事件抽象设计》的分层原则：

    底层（core/signals.py）   读系统信号，Windows 专属实现都关在这里
    适配层（本模块）          把不同来源归一成设备无关的 :class:`Signal`
    业务层（judge / state）   只消费语义字段：空闲多久、屏幕是锁着还是全屏

为什么要有这一层
----------------
1. **跨平台**：PC 用 ``GetLastInputInfo`` 读键鼠空闲，Android / iOS / macOS
   拿不到同一套 API。以后宿主在别的平台提供了自己的信号，只要多写一个
   适配器，判定与记账代码一行都不用改。
2. **优雅降级**：每个字段都可缺失（``None`` / ``SCREEN_UNKNOWN``），
   适配器绝不编造。业务层看到「不知道」就按「没有记录」处理，
   而不是猜成「你在操作」—— 猜出来的陪伴时长比没有更糟。
3. **可测试**：适配器输出的是纯数据，``tests/`` 里可以直接构造，
   不需要真的锁屏、开全屏去测判定分支。

语义字段
--------
    idle_sec    距最后一次键鼠输入的秒数；None = 这一路信号不可用
    screen      normal / locked / fullscreen / unknown，四态
    foreground  前台进程名（仅文件名，Windows 才有；其他平台留空）
    privacy     宿主隐私状态；不是 visible 时一律不描述用户行为
    source      win32 / host_sdk / none，写进探针，方便回放时知道数据从哪来

注意：本模块**不 import ``plugin.sdk``**。宿主快照通过构造函数注入一个
``async`` 可调用对象，这样核心层仍然能脱离宿主单测。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from . import signals
from .signals import (
    QUNS_BUSY,
    QUNS_D3D_FULLSCREEN,
    QUNS_NOT_PRESENT,
    QUNS_QUIET_TIME,
)

# ---- 屏幕语义（设备无关）----
SCREEN_NORMAL = "normal"          # 普通桌面，是否在场要再看 idle
SCREEN_LOCKED = "locked"          # 锁屏 / 屏保：确认级「人不在」
SCREEN_FULLSCREEN = "fullscreen"  # 全屏应用：人在看片或游戏的可能性高
SCREEN_UNKNOWN = "unknown"        # 这一路信号拿不到屏幕状态

# ---- 数据来源（写进探针 chunk 的 ``src`` 字段）----
SOURCE_WIN32 = "win32"
SOURCE_HOST = "host_sdk"
SOURCE_NONE = "none"

_FULLSCREEN_Q = frozenset({QUNS_BUSY, QUNS_D3D_FULLSCREEN, QUNS_QUIET_TIME})

SnapshotCallable = Callable[[], Awaitable[Any]]


def screen_from_q(q: int | None) -> str:
    """把 Windows 的 ``SHQueryUserNotificationState`` 状态码翻成语义屏幕态。

    只有锁屏和全屏两种「强语义」被保留；``q=4``（演示模式）与
    ``q=5``（普通桌面）都归 ``normal``，因为这两者是否需要判「在场」
    仍然取决于空闲时长，交给业务层。
    """
    if q == QUNS_NOT_PRESENT:
        return SCREEN_LOCKED
    if q in _FULLSCREEN_Q:
        return SCREEN_FULLSCREEN
    if q is None:
        return SCREEN_UNKNOWN
    return SCREEN_NORMAL


@dataclass(frozen=True)
class Signal:
    """一条设备无关的语义采样。

    它不描述「鼠标移动了多少像素」这类底层细节，只回答业务层需要的三个问题：
    **你多久没动了？屏幕是什么状态？我能不能信这份数据？**
    """

    t: float                          # wall clock 秒
    m: float                          # 单调时钟秒
    idle_sec: float | None = None     # 距最后一次键鼠输入的秒数
    screen: str = SCREEN_UNKNOWN      # 语义屏幕态
    foreground: str = ""              # 前台进程名（仅 Windows）
    privacy: str = "unavailable"      # 宿主隐私状态
    category: str = ""                # 宿主给的粗分类（work/gaming…）
    sdk_idle: float | None = None     # 宿主快照里的空闲秒数（对照用）
    q: int | None = None              # Windows 原始状态码（探针保留）
    source: str = SOURCE_NONE         # 哪个适配器给的

    @property
    def usable(self) -> bool:
        """这条采样是否足以支撑一次判定。

        只有「知道空闲时长」或「确定锁屏/全屏」才算可用；
        ``normal + idle=None`` 等于什么都不知道，宁可丢弃也不猜。
        """
        return self.idle_sec is not None or self.screen in (
            SCREEN_LOCKED,
            SCREEN_FULLSCREEN,
        )


def _as_float(raw: Any) -> float | None:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if val == val else None


async def _call_snapshot(snapshot: SnapshotCallable | None) -> Any:
    """调用宿主快照；任何异常都吞掉，返回 ``None``。

    适配层的第一原则是「绝不因为一个信号失败而打断采样链」。
    """
    if snapshot is None:
        return None
    try:
        return await snapshot()
    except Exception:  # noqa: BLE001 - 见模块 docstring 第 2 条
        return None


def _host_fields(snap: Any) -> tuple[str, float | None, str]:
    """从宿主快照里取 (privacy, idle, category)，并执行隐私门控。"""
    privacy = str(getattr(snap, "privacy_state", "unavailable") or "unavailable")
    if privacy != "visible":
        # 宿主明确说「现在不该观察用户」→ 一个业务字段都不读
        return privacy, None, ""
    idle = _as_float(getattr(snap, "system_idle_seconds", None))
    category = str(getattr(snap, "foreground_category", "") or "").strip().lower()
    return privacy, idle, category


class NullAdapter:
    """什么信号都拿不到的平台。返回的采样永远 ``usable=False``。"""

    source = SOURCE_NONE

    async def sample(self) -> Signal:
        return Signal(t=time.time(), m=time.monotonic(), source=SOURCE_NONE)


class Win32Adapter:
    """Windows 原生适配器：键鼠空闲 + 通知状态 + 前台进程名。

    宿主快照仍会被调用，但只用于两件事：记录 ``p/i/c`` 对照字段，
    以及原生空闲 API 失败时兜底（仅隐私状态为 ``visible`` 时）。
    """

    source = SOURCE_WIN32

    def __init__(self, host_snapshot: SnapshotCallable | None = None) -> None:
        self._host = host_snapshot

    async def sample(self) -> Signal:
        t = time.time()
        m = time.monotonic()

        # 先问宿主隐私态，再决定要不要读本地信号。
        # 顺序不能反：反了就成了「先读了再假装没读」。
        privacy = "unavailable"
        sdk_idle: float | None = None
        category = ""
        if self._host is not None:
            snap = await _call_snapshot(self._host)
            if snap is not None:
                privacy, sdk_idle, category = _host_fields(snap)
                if privacy == "private":
                    # 宿主明确要求「别观察」→ 连原生信号也不读。
                    # 注意 unavailable 不在此列：那表示宿主自己拿不到信号，
                    # 不是用户的隐私指令；本机就属于这种情况。
                    return Signal(
                        t=t,
                        m=m,
                        privacy=privacy,
                        source=SOURCE_WIN32,
                    )

        q = signals.notification_state()
        w = signals.idle_seconds()
        f = signals.foreground_exe()

        # 原生 API 失败才退回宿主；宿主字段只代表「多久没动」，
        # 不提供锁屏/全屏，所以屏幕态仍以 Windows 原生为准
        if w is None and sdk_idle is not None:
            w = sdk_idle

        return Signal(
            t=t,
            m=m,
            idle_sec=w,
            screen=screen_from_q(q),
            foreground=f,
            privacy=privacy,
            category=category,
            sdk_idle=sdk_idle,
            q=q,
            source=SOURCE_WIN32,
        )


class HostSdkAdapter:
    """宿主快照适配器：给非 Windows 平台（或原生信号不可用）用。

    宿主只承诺两件事：``privacy_state`` 和 ``system_idle_seconds``。
    它不提供「锁屏 / 全屏」的细分，所以屏幕态只能是 ``unknown``，
    判定退化为「只看空闲多久」—— 这仍然能区分在操作、短暂离席、静默离开。
    """

    source = SOURCE_HOST

    def __init__(self, host_snapshot: SnapshotCallable) -> None:
        self._host = host_snapshot

    async def sample(self) -> Signal:
        t = time.time()
        m = time.monotonic()
        snap = await _call_snapshot(self._host)
        if snap is None:
            return Signal(t=t, m=m, source=SOURCE_NONE)

        privacy, idle, category = _host_fields(snap)
        return Signal(
            t=t,
            m=m,
            idle_sec=idle,
            screen=SCREEN_UNKNOWN,
            privacy=privacy,
            category=category,
            sdk_idle=idle,
            source=SOURCE_HOST,
        )


def build_adapter(host_snapshot: SnapshotCallable | None = None):
    """按当前平台挑一个适配器。

    - Windows 且有 ctypes → :class:`Win32Adapter`（信息最全）
    - 其他平台且有宿主快照 → :class:`HostSdkAdapter`（降级但可用）
    - 都没有 → :class:`NullAdapter`（诚实地说「没有记录」）
    """
    if os.name == "nt" and signals.has_ctypes():
        return Win32Adapter(host_snapshot)
    if host_snapshot is not None:
        return HostSdkAdapter(host_snapshot)
    return NullAdapter()
