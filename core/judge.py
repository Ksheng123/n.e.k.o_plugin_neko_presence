"""在场判定状态机 —— 纯函数，零 SDK 依赖，可直接单测。

依据 DESIGN.md §6.3：每 30 秒一条采样，按优先级判定，先命中先算。

| 优先级 | 状态 | 规则 | 性质 |
|---|---|---|---|
| 1 | ``locked``          | ``q == 1``（锁屏/屏保）              | 确认离开 |
| 2 | ``away_fullscreen`` | ``q ∈ {2,3,6}`` 且 ``w > 全屏长尾``   | 推断离开 |
| 3 | ``fullscreen``      | ``q ∈ {2,3,6}``                      | 观察事实 |
| 4 | ``interactive``     | ``w < 互动阈值``                     | 观察事实 |
| 5 | ``short_break``     | ``互动阈值 ≤ w ≤ 静默阈值``           | 观察事实 |
| 6 | ``away``            | ``w > 静默阈值``                      | 推断离开 |
| — | ``unknown``         | 空闲与屏幕状态都不可用                | **没有记录**，不编造 |

``unknown`` 不是一种「在场状态」，而是数据层诚实的空缺：适配器拿不到信号时
（非 Windows 且宿主快照也不可用、隐私状态非 visible、API 调用失败）返回它，
状态机据此切断当前段，这段时间在面板上显示为「没有记录」。
**宁可少记一段，也不把「不知道」写成「她陪你」或「你离开了」。**

两个关键修正（都来自真机实测，见 §6.4）：

1. **`q` 会滞后一个采样**：解锁瞬间可能出现 ``w`` 已归零但 ``q`` 仍为 1。
   所以 ``q == 1`` 且 ``w`` 很小的时候，判 interactive 而不是 locked。
2. **全屏算"陪伴"不算"离开"**：人就在屏幕前看片，她确实陪着。
   只有全屏而且**连续零输入超过长尾阈值**，才认定人走了、画面还挂着。
"""

from __future__ import annotations

from dataclasses import dataclass

from .adapters import SCREEN_FULLSCREEN, SCREEN_LOCKED, screen_from_q

# 状态名（对外可见的稳定标识，日志与存储都用这些字面量）
LOCKED = "locked"
AWAY_FULLSCREEN = "away_fullscreen"
FULLSCREEN = "fullscreen"
INTERACTIVE = "interactive"
SHORT_BREAK = "short_break"
AWAY = "away"
#: 数据不可用。不是「状态」，是「没有记录」——见模块 docstring 的优先级表。
UNKNOWN = "unknown"

#: 算作「她陪着你」的状态 —— 你在屏幕前（含全屏看片）
COMPANION_STATES = frozenset({INTERACTIVE, FULLSCREEN})

#: 算作「她等着你」的状态 —— 你不在
WAITING_STATES = frozenset({LOCKED, AWAY, AWAY_FULLSCREEN})


@dataclass(frozen=True)
class Sample:
    """一条采样。字段名与存储键一一对应，避免来回翻译。

    ``screen`` 是适配层给的语义屏幕态；留空时回退到 ``q`` 的 Windows 状态码。
    旧探针 chunk 只有 ``q`` 没有 ``screen``，因此两条路都要能走。
    """

    t: float                     # wall clock 秒
    m: float                     # 单调时钟秒
    w: float | None              # 距最后一次键鼠输入的秒数
    q: int | None = None         # SHQueryUserNotificationState（旧数据）
    f: str = ""                  # 前台进程名（仅文件名）
    screen: str = ""             # normal / locked / fullscreen / unknown


@dataclass(frozen=True)
class Config:
    """判定阈值。全部可从 plugin.toml 覆盖，出厂值见 DESIGN.md §6.3。"""

    interactive_sec: float = 60.0            # w 小于此值 = 在操作
    silent_leave_sec: float = 900.0          # w 大于此值 = 静默离开（T=900，已定稿）
    fullscreen_tail_sec: float = 3 * 3600.0  # 全屏连续零输入超过此值 = 人走了（N=3h）


def classify(sample: Sample, cfg: Config) -> str:
    """判定这一刻处于哪个状态；数据不可用时返回 ``UNKNOWN``。

    - ``screen`` 优先；留空时把 Windows 的 ``q`` 码翻成同一套语义，
      这样旧探针数据和新适配器数据走同一条判定路径。
    - ``w is None`` 意味着空闲时长不可用。此时只有锁屏/全屏这类**强语义**
      还能支撑判定；其余情况一律 ``UNKNOWN``，绝不猜成 ``interactive``。
    """
    w = sample.w
    screen = sample.screen or screen_from_q(sample.q)

    if w is None:
        if screen == SCREEN_LOCKED:
            return LOCKED          # 锁屏是确认级判据，不需要 idle
        if screen == SCREEN_FULLSCREEN:
            return FULLSCREEN      # 只能确认「在放全屏」，不能推断人走没走
        return UNKNOWN             # 什么都不知道 → 没有记录

    # 修正一：q 会滞后一拍 —— 已经回来了（w 很小）就不要判成锁屏
    if screen == SCREEN_LOCKED and w < cfg.interactive_sec:
        return INTERACTIVE

    # 优先级 1：锁屏 = 确认离开（最强判据）
    if screen == SCREEN_LOCKED:
        return LOCKED

    # 优先级 2/3：全屏。先看是否超过长尾
    if screen == SCREEN_FULLSCREEN:
        if w > cfg.fullscreen_tail_sec:
            return AWAY_FULLSCREEN
        return FULLSCREEN

    # 优先级 4~6：无全屏信号，只能靠 w
    if w < cfg.interactive_sec:
        return INTERACTIVE
    if w <= cfg.silent_leave_sec:
        return SHORT_BREAK
    return AWAY


def counts_as_companion(state: str) -> bool:
    """这个状态下的时间，算不算「她陪着你」。"""
    return state in COMPANION_STATES


def counts_as_waiting(state: str) -> bool:
    """这个状态下的时间，算不算「她等着你」。

    注意：``away_fullscreen`` 只算**超出长尾阈值的那部分**，
    那段减法在 state 层做（那里才知道本段已经持续了多久）。
    """
    return state in WAITING_STATES
