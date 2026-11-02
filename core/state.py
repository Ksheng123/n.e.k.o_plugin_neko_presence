"""区间管理与日聚合 —— 纯逻辑，零 SDK 依赖。

职责边界
--------
- ``judge.py`` 回答"这一刻是什么状态"（无状态纯函数）
- **本模块**回答"这段从哪开始、峰值多少、怎么记账"（有状态，需要落盘）
- ``__init__.py`` 只做编排（timer 取采样 → 调本模块 → 落盘），不含业务判断

核心取舍（改之前先读）
----------------------
1. **陪伴时长靠累加，等待时长靠峰值。**
   - 陪伴（`w` 很小）：`idle` 测不出"陪了多久"，只能把采样间隔加总。
   - 等待（`w` 很大）：`w` 本身就是"距最后一次输入的绝对秒数"，
     天然回溯、不受漏采样影响，所以取本段峰值即可，不用累加。
2. **`away_fullscreen` 只把超出长尾阈值的部分算作等待。**
   依据 DESIGN.md §6.3：全屏且连续零输入超过 N 小时才认定人走了，
   前 N 小时仍算"她在陪你"。

⚠️ **三本账，永远不可并列相加**（2026-10-27 定稿，改之前必读）
-----------------------------------------------------------------
| 账本 | 字段 | 口径 | 回答什么 | 缺口期间 |
|---|---|---|---|---|
| A 采样进度 | ``covered_sec`` / ``span_sec`` | duration 之和 / 真实跨度 | 数据完整吗 | 分子不计、分母计 → 比值 < 1 |
| B 互斥归因 | ``companion`` / ``away`` / ``short_break`` / ``locked`` | duration（只算有采样的间隔） | 有采样的时间花在哪 | **不计** |
| C 真实时长 | ``waiting_sec`` / ``longest_wait_sec`` | ``peak_w``（系统绝对量） | 你实际离开了多久 | **照样准确** |

- B 的 ``locked`` 与 C 的 ``waiting`` **时间上天然重叠**（锁屏那段既算锁屏也算离开），
  面板并排展示会让用户加出超过总时长的数 —— 这是硬禁令。
- A 的分母**必须是 `span_sec`**。早期版本用 ``attributed / covered`` 做覆盖率，
  分子分母都是 duration 之和，**恒等于 1.00**，等于没做检查（2026-10-27 发现）。
- **「无记录」不等于「不在」**：插件没运行时没有采样，A 会暴露出来（< 1）；
  那段时间既不能说"她陪着你"（B 不会计），也不能说"你安静了"（C 可能会计入，
  因为它读的是系统绝对量）—— 面板对 A 不足的时段必须标"没有记录"。
"""

from __future__ import annotations

from dataclasses import dataclass

from .judge import (
    AWAY,
    AWAY_FULLSCREEN,
    FULLSCREEN,
    INTERACTIVE,
    LOCKED,
    SHORT_BREAK,
    UNKNOWN,
    Config,
    Sample,
    classify,
    counts_as_companion,
)

#: 单条采样允许计入的最大间隔（秒）。超过就视为"中间断了"——
#: 合盖/睡眠/插件重启都会造成这种跳变，不能把整段空白算成一次陪伴。
MAX_STEP_SEC = 120.0

#: 所有"人不在操作"的状态。一次离开常在这几个状态之间流转
#: （``short_break`` → ``away`` → ``locked``），但它们是**同一次离开**。
#: ``peak_w`` 是"距最后一次输入的绝对秒数"，每段结尾都在涨，
#: 所以每切一段就取一次会把前面的时间重复累加 —— 必须合并成一个事件。
LEAVE_STATES = frozenset({SHORT_BREAK, AWAY, AWAY_FULLSCREEN, LOCKED})


@dataclass
class Segment:
    """一段连续状态。段结束时结算进日聚合。"""

    state: str = INTERACTIVE
    start_t: float = 0.0        # 段起点 wall clock
    start_m: float = 0.0        # 段起点 单调时钟
    last_t: float = 0.0         # 最后一次采样的 wall clock
    last_m: float = 0.0         # 最后一次采样的单调时钟
    duration: float = 0.0       # 累加的采样间隔之和（秒）
    peak_w: float = 0.0         # 段内 w 峰值 —— 等待时长的权威来源
    samples: int = 0

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "start_t": round(self.start_t, 1),
            "start_m": round(self.start_m, 1),
            "last_t": round(self.last_t, 1),
            "last_m": round(self.last_m, 1),
            "duration": round(self.duration, 1),
            "peak_w": round(self.peak_w, 1),
            "samples": self.samples,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Segment:
        def num(key: str) -> float:
            try:
                return float(raw.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        return cls(
            state=str(raw.get("state") or INTERACTIVE),
            start_t=num("start_t"),
            start_m=num("start_m"),
            last_t=num("last_t"),
            last_m=num("last_m"),
            duration=num("duration"),
            peak_w=num("peak_w"),
            samples=int(raw.get("samples") or 0),
        )


@dataclass
class DayStat:
    """一天的聚合结果。面板、月报、年报都只读这一份。"""

    date: str = ""
    first_seen: float | None = None      # 当天第一条采样的 wall clock
    last_seen: float | None = None       # 当天最后一条采样的 wall clock
    companion_sec: float = 0.0           # [账本B] 她陪了你多久（interactive + fullscreen，duration 口径）
    waiting_sec: float = 0.0             # [账本C] 离开累计（peak_w 口径，含无采样缺口，与 B 不可加）
    waiting_count: int = 0               # [账本C] 离开几次
    longest_wait_sec: float = 0.0        # [账本C] 最长一次离开
    short_break_sec: float = 0.0         # [账本B] 60~900 秒的离席，不计入 waiting
    locked_sec: float = 0.0              # [账本B] 锁屏期间的 duration（与 waiting 重叠，勿加）
    away_sec: float = 0.0                # [账本B] 静默离开段的 duration
    fullscreen_sec: float = 0.0          # [账本B] 全屏活动（companion 的子集，仅备查）
    covered_sec: float = 0.0             # [账本A] 有采样的时长之和 —— 覆盖率分子
    last_state: str = ""                 # 内部：上一段结算进来的状态，用于合并连续离开事件
    open_wait_sec: float = 0.0           # 内部：当前还没结算的离开事件峰值（peak_w 口径）

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "companion_sec": round(self.companion_sec, 1),
            "waiting_sec": round(self.waiting_sec, 1),
            "waiting_count": self.waiting_count,
            "longest_wait_sec": round(self.longest_wait_sec, 1),
            "short_break_sec": round(self.short_break_sec, 1),
            "locked_sec": round(self.locked_sec, 1),
            "away_sec": round(self.away_sec, 1),
            "fullscreen_sec": round(self.fullscreen_sec, 1),
            "covered_sec": round(self.covered_sec, 1),
            # 内部字段也必须落盘：插件重启后要从上次状态继续合并离开事件，
            # 丢了会让同一次离开被当成两次（waiting 重复累加）
            "last_state": self.last_state,
            "open_wait_sec": round(self.open_wait_sec, 1),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> DayStat:
        def num(key: str) -> float:
            try:
                return float(raw.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        first = raw.get("first_seen")
        last = raw.get("last_seen")
        return cls(
            date=str(raw.get("date") or ""),
            first_seen=float(first) if isinstance(first, (int, float)) else None,
            last_seen=float(last) if isinstance(last, (int, float)) else None,
            companion_sec=num("companion_sec"),
            waiting_sec=num("waiting_sec"),
            waiting_count=int(raw.get("waiting_count") or 0),
            longest_wait_sec=num("longest_wait_sec"),
            short_break_sec=num("short_break_sec"),
            locked_sec=num("locked_sec"),
            away_sec=num("away_sec"),
            fullscreen_sec=num("fullscreen_sec"),
            covered_sec=num("covered_sec"),
            last_state=str(raw.get("last_state") or ""),
            open_wait_sec=num("open_wait_sec"),
        )

    @property
    def span_sec(self) -> float:
        """[账本A] 当天真实时间跨度：第一条采样 → 最后一条采样。

        这是覆盖率的**分母**。它由 wall clock 首尾算出，天然包含中间所有
        无采样的缺口（电脑待机、N.E.K.O 没开、插件重启）。
        """
        if self.first_seen is None or self.last_seen is None:
            return 0.0
        return max(0.0, self.last_seen - self.first_seen)

    @property
    def attributed_sec(self) -> float:
        """[账本B] 互斥归因总额：每个采样段的 duration 只归到一个状态。

        ⚠️ **不能用 ``waiting_sec`` 参与归因** —— 它是 ``peak_w`` 口径的
        产品指标，与 ``locked_sec`` 等状态时长**语义重叠**（同一段锁屏会同时
        进 ``locked_sec`` 和 ``waiting_sec``）。早期版本把两者一起加，
        导致总额超过采样时长（2026-10-25 实测 12.15h vs 10.70h）。

        正常应当 **≈ ``covered_sec``**（两者都是 duration 之和）。
        明显不等说明记账漏了某个状态分支。
        """
        return (
            self.companion_sec
            + self.away_sec
            + self.short_break_sec
            + self.locked_sec
        )

    @property
    def covered_ratio(self) -> float:
        """[账本A] 采样覆盖率 = 有采样的时长 / 真实时间跨度。

        < 1 说明中间有盲区。面板应把那段时间显示成「没有记录」，
        **而不能显示成「你不在」**（后者是在编造）。

        ⚠️ 分母必须是 ``span_sec``。早期版本写的是 ``attributed / covered``，
        分子分母都是 duration 之和，**恒等于 1.00**，等于完全没做检查
        （2026-10-27 发现：10-26 实际覆盖率只有 0.76，却一直显示 1.00）。

        不截断到 1.0：超过 1 说明有跨天段的重复归因，要让它暴露出来。
        """
        span = self.span_sec
        if span <= 0:
            return 0.0
        return self.covered_sec / span

    @property
    def waiting_total_sec(self) -> float:
        """[账本C] 含未结算事件在内的离开累计 —— **展示一律用这个**。

        ``waiting_sec`` 只存"已经结束"的离开事件（回到陪伴时才结算），
        当前仍处于离开态的那次峰值存在 ``open_wait_sec`` 里。
        直接读 ``waiting_sec`` 会让当天最后一段离开凭空消失。

        ``open_wait_sec`` 的值要么是 0、要么已经越过 T，所以直接相加
        不会把"起身上厕所"算进来。
        """
        return self.waiting_sec + self.open_wait_sec


@dataclass
class StepResult:
    """推进一条采样的结果。"""

    current: Segment                    # 推进之后的当前段
    closed: Segment | None = None       # 状态切换时结算出去的旧段
    state: str = INTERACTIVE
    step_sec: float = 0.0               # 本条采样实际计入的时长
    stale: bool = False                 # True = 间隔过大，本条未计入时长


def step(
    current: Segment,
    sample: Sample,
    cfg: Config,
    *,
    max_step_sec: float = MAX_STEP_SEC,
) -> StepResult:
    """用一条采样推进当前段。

    采样间隔（相邻两条的 ``m`` 差值）超过 ``max_step_sec`` 时，说明中途断了
    （合盖/睡眠/插件挂起/电脑关机）。此时 **就地切断段**：旧段原样结算
    （``last_t`` 保持最后一条有效采样的时刻），新段从这条采样重新开始。

    ⚠️ **不能把段"按时钟接上"继续用** —— 那样 ``last_t`` 会跳到恢复后的时刻，
    段的跨度凭空膨胀而 ``duration`` 不变，覆盖率被算低（2026-10-27 实测：
    重放时跨天段把 10-24 的 ``last_seen`` 拉到 10-25 11:01，两天互相污染）。
    切断后缺口不再属于任何段，只体现在首尾跨度与采样时长的差上 —— 这才是
    「无记录」的正确表示。

    段切换发生在两条采样之间。按保守口径把这段 ``delta`` 归给**旧段**
    （旧状态确实一直持续到上一条采样为止）。

    **``unknown`` 采样不建段。** 适配器拿不到信号时判定为 ``UNKNOWN``：
    此时就地结算当前段（结束时刻停在最后一条有效采样），然后清空当前段。
    从「最后一条有效采样」到「下一条有效采样」之间的时间不属于任何段，
    在面板上就是「没有记录」—— 这正是「不知道 ≠ 不在」。
    """
    state = classify(sample, cfg)

    # 数据不可用：切断，不猜。旧段原样结算（last_t 停在最后一条有效采样）
    if state == UNKNOWN:
        closed = current if current.samples > 0 else None
        return StepResult(
            current=Segment(),
            closed=closed,
            state=UNKNOWN,
            step_sec=0.0,
            stale=closed is not None,
        )

    peak = float(sample.w or 0.0)

    # 首条采样：起一个新段
    if current.samples == 0:
        fresh = Segment(
            state=state,
            start_t=sample.t,
            start_m=sample.m,
            last_t=sample.t,
            last_m=sample.m,
            duration=0.0,
            peak_w=peak,
            samples=1,
        )
        return StepResult(current=fresh, state=state, step_sec=0.0)

    delta_m = sample.m - current.last_m
    # 断档判据**双保险**：只让 `m` 判断会漏掉跨重启 ——
    # 电脑重启后单调时钟归零甚至倒退，`delta_m` 变负被截成 0，
    # 段就被静默续上，跨度直接跨到第二天（2026-10-27 重放实测：
    # 10-24 的 last_seen 被拉成 10-25 11:01）。
    # 所以取 `m` 差与 `t` 差的较大者做判据；时长累加仍只用 `m`
    # （它不受用户改系统时间影响，`t` 会）。
    gap = max(delta_m, sample.t - current.last_t)

    # 断档：切断，旧段就地结算，新段从这条采样起
    if gap > max_step_sec:
        closed = current
        fresh = Segment(
            state=state,
            start_t=sample.t,
            start_m=sample.m,
            last_t=sample.t,
            last_m=sample.m,
            duration=0.0,
            peak_w=peak,
            samples=1,
        )
        return StepResult(current=fresh, closed=closed, state=state, step_sec=0.0, stale=True)

    counted = max(0.0, delta_m)

    if state == current.state:
        current.duration += counted
        current.peak_w = max(current.peak_w, peak)
        current.samples += 1
        current.last_t = sample.t
        current.last_m = sample.m
        return StepResult(current=current, state=state, step_sec=counted, stale=False)

    # 状态切换：旧段结算出去，新段从这条采样开始
    current.duration += counted
    current.last_t = sample.t
    current.last_m = sample.m
    closed = current
    fresh = Segment(
        state=state,
        start_t=sample.t,
        start_m=sample.m,
        last_t=sample.t,
        last_m=sample.m,
        duration=0.0,
        peak_w=peak,
        samples=1,
    )
    return StepResult(current=fresh, closed=closed, state=state, step_sec=counted, stale=False)


def roll_segment(seg: Segment, limit: float) -> Segment | None:
    """同一个状态累计够久就切一刀，返回续段（不该切返回 ``None``）。

    为什么需要：日聚合只在**段结算**时更新。用户连续几小时停在同一个状态
    （一直操作、一直没离开），段就永远不结束，``daily_v1`` 一直停在
    "上次结算那一刻" —— 2026-10-31 真机：内存里已经 4 小时，
    ``daily_v1`` 只有 238 秒。展示侧能靠试算补上，但那段时间只存在内存
    和 ``current_v1`` 一份，插件一重载风险就全压在一个兜底上。

    切段不是状态切换，规则是「新旧段首尾相接、口径一分不差」：

    - 续段 ``start_t`` / ``start_m`` 取原段的 ``last_t`` / ``last_m`` ——
      下一条采样算 gap 时差值正常（若把 ``start_m`` 归零，``delta_m`` 会
      变成整个开机时长，每一拍都被判成断档）
    - 续段 ``peak_w`` **延续原段峰值** —— 离开事件跨段仍然只取 max，
      ``waiting_count`` 也就只 +1（``open_wait_sec > 0`` 开着，走延续分支）
    - 续段 ``duration`` 从 0 起，``samples=1``（起点那条采样算它的）
    - 两段首尾重合，前端 ``mergeSegs()`` 会并成一块，时间轴看不出切口

    ``away_fullscreen`` 拒绝切：它按 ``peak_w - fullscreen_tail`` 做非线性
    切分（只有超出长尾的部分才算离开），切成多段会**每段都重新扣一次长尾**，
    ``away_sec`` 直接虚高。那种段继续长着，代价只是日聚合落盘滞后。
    """
    if limit <= 0 or seg.duration < limit:
        return None
    if seg.state == AWAY_FULLSCREEN:
        return None
    if seg.last_m <= 0 or seg.last_t <= 0:
        return None
    return Segment(
        state=seg.state,
        start_t=seg.last_t,
        start_m=seg.last_m,
        last_t=seg.last_t,
        last_m=seg.last_m,
        duration=0.0,
        peak_w=seg.peak_w,
        samples=1,
    )


def leave_peak(seg: Segment, cfg: Config) -> float:
    """这一段贡献的"离开峰值"（``peak_w`` 口径）。

    - ``away`` / ``locked``：``peak_w`` 本身就是"距最后一次输入的绝对秒数"
    - ``away_fullscreen``：只有超出全屏长尾阈值的部分才算离开
    - ``short_break``：不单独产出峰值，但它标志"这次离开已经开始"
    """
    if seg.state == AWAY_FULLSCREEN:
        return max(0.0, seg.peak_w - cfg.fullscreen_tail_sec)
    if seg.state in (AWAY, LOCKED):
        return seg.peak_w
    return 0.0


def settle_leave_event(day: DayStat) -> None:
    """把当前离开事件的峰值结算进 ``waiting_sec``，然后清空事件。

    用 ``open_wait_sec > 0`` 作判据而不是看 ``last_state``：
    ``open_wait_sec`` 只在峰值**首次越过 T** 时才被设成非 0，
    所以它本身就是"这次离开够格了"的标记。短离席（``peak < T``）
    从头到尾都不会把事件打开，自然不会被记成一次离开。
    """
    if day.open_wait_sec <= 0:
        return
    day.waiting_sec += day.open_wait_sec
    day.open_wait_sec = 0.0


def apply_segment(day: DayStat, seg: Segment, cfg: Config) -> DayStat:
    """把一段结算进当日聚合（原地修改并返回同一个对象）。

    **两套口径，别混用：**

    | 口径 | 字段 | 算什么 | 用途 |
    |---|---|---|---|
    | ``duration`` | companion / away / short_break / locked | 有采样的间隔之和，互斥 | 归因（和 ≈ covered_sec） |
    | ``peak_w`` | waiting_sec / longest_wait_sec | 距最后一次输入的绝对秒数 | 你实际离开了多久（含无采样缺口） |

    ⚠️ **离开事件必须跨段合并**：一次离开常被切成
    ``short_break → away → locked`` 好几段，而 ``peak_w`` 每段结尾都在涨，
    逐段累加会把前面的时间重复算一遍（2026-10-27 重放实测：10-26 的
    离开累计从 184min 虚增到 251min）。
    所以只在「从陪伴转入离开」时开事件，事件内只取峰值，回到陪伴时才结算。
    """
    day.covered_sec += seg.duration
    # 所有状态都参与 first/last 标记 —— 覆盖率的分母需要真实的采样首尾，
    # 只记陪伴类会让"整天在离开"的那天 span 变成 0
    _track_seen(day, seg)

    # ---------- 陪伴类：离开事件在此结算 ----------
    if counts_as_companion(seg.state):
        settle_leave_event(day)
        day.companion_sec += seg.duration
        if seg.state == FULLSCREEN:
            day.fullscreen_sec += seg.duration
        day.last_state = seg.state
        return day

    # ---------- 状态时长（duration 口径，互斥归因）----------
    if seg.state == SHORT_BREAK:
        day.short_break_sec += seg.duration
    elif seg.state == AWAY_FULLSCREEN:
        # 前 N 小时仍算"她陪着你"，超出部分才认定为离开
        wait = max(0.0, seg.peak_w - cfg.fullscreen_tail_sec)
        companion = max(0.0, seg.duration - wait)
        day.companion_sec += companion
        day.fullscreen_sec += companion
        day.away_sec += max(0.0, seg.duration - companion)
    elif seg.state == LOCKED:
        day.locked_sec += seg.duration
    else:  # AWAY
        day.away_sec += seg.duration

    # ---------- 离开事件（peak_w 口径，跨段合并，只取峰值）----------
    peak = leave_peak(seg, cfg)
    # ``open_wait_sec`` 的取值要么是 0，要么已经越过 T ——
    # 所以「未结算事件」可以不传 cfg 直接加进展示口径（见 waiting_total_sec）。
    # 用 open_wait_sec 是否 > 0 判断"事件是否已开"，而不是看上一个段的状态：
    # 短离席（peak < T）会把 last_state 变成 leave 态但不开事件，
    # 若按 last_state 判断，后面真正够格的 away 段会被误当成"延续"，次数就漏计了。
    if peak > cfg.silent_leave_sec:
        if day.open_wait_sec > 0:
            day.open_wait_sec = max(day.open_wait_sec, peak)   # 同一次离开的延续
        else:
            day.open_wait_sec = peak                            # 新事件
            day.waiting_count += 1
        # 峰值要实时反映到 longest —— 否则当天最后一段离开还在进行中时，
        # 「最长一次」会显示成 0（它只在结算时才更新的话）
        day.longest_wait_sec = max(day.longest_wait_sec, day.open_wait_sec)
    day.last_state = seg.state
    return day


def _track_seen(day: DayStat, seg: Segment) -> None:
    """维护当天第一条 / 最后一条采样的时刻（wall clock）。

    ``last_seen`` 必须取 ``seg.last_t``（最后一条采样的实际时刻），
    **不能用 ``start_t + duration``** —— ``duration`` 不含 stale 缺口，
    那样算出的结束时刻会早于真实结束。
    实测（2026-10-26）：一段从 10:05 开始、真实持续到 11:22 的
    interactive 段，``duration`` 只有 600s，按 ``start_t + duration``
    会算成 10:15，跨度凭空少 67 分钟，覆盖率也就跟着虚高。
    """
    if seg.start_t > 0 and (day.first_seen is None or seg.start_t < day.first_seen):
        day.first_seen = seg.start_t
    if seg.last_t > 0 and (day.last_seen is None or seg.last_t > day.last_seen):
        day.last_seen = seg.last_t


# ==========================================================
# 展示侧口径
# ==========================================================


def onset_is_late(first_seen: float, day_start: float, threshold: float = 1800.0) -> bool:
    """当天首条采样是否明显晚于当日零点 —— 即「记录起点」不是「一天的开头」。

    用来区分两件长得一样、意思却完全相反的事：

    - 「她那天只陪了你 48 分钟」
    - 「她的记录 22:32 才开始，在那之前是空白」

    2026-11-01 真机：插件 10:57 才启动，``companion_sec`` 只有 1h45m，模型照读成
    「今天只陪了你 1 小时 45 分」，用户以为她上午不在。**空白 ≠ 不在**，
    所以首条采样晚于 00:30 时，要把「从几点开始记」一并说出来。

    ``threshold`` 取 30 分钟：用户凌晨 0:10 才开始用电脑，首见晚于零点却
    并没有真的漏掉什么，这时不该多嘴。
    """
    try:
        first = float(first_seen)
        start = float(day_start)
    except (TypeError, ValueError):
        return False
    if first <= 0 or start <= 0:
        return False
    return (first - start) > float(threshold)


def live_moment(segs: list, short_gap: float = 600.0) -> tuple[float, float]:
    """从当天时间轴倒着扫，算出「最近一次离开多久」和「这次已经坐了多久」。

    这两个数是「时机」的全部依据 —— 她说得再漂亮，若不知道对方刚回来还是
    已经枯坐三小时，就只能按固定频率随口开口，那正是「烦」的来源。

    ``segs`` 是当天分段列表（每项含 ``o`` 起始偏移、``len`` 时长、``s`` 状态），
    按时间先后排列。返回 ``(gap_sec, run_sec)``：

    - ``gap_sec``：最近一次**长离开**（≥ ``short_gap``）有多长；当前正处在
      长离开里则返回 0（还没回来，谈不上「刚回来」）。
    - ``run_sec``：从那次长离开结束到现在，一共坐了多久；当前在长离开里返回 0。

    **短离开不算中断。** 去倒杯水、接个电话的那几分钟不能重置「这次坐了多久」，
    否则一个从早坐到晚的人永远显示「刚坐下」—— 2026-11-02 用 11-01 真实数据
    验过：按「任何离开都重置」算，20:00 时连续在场只有 134 分钟，而人其实
    从 10:57 就在了。所以短于 ``short_gap``（默认 10 分钟）的离开并入在场时长。

    倒扫而不是正扫：只需要尾巴上这两段，不需要把整天算一遍。
    """
    gap = 0.0
    run = 0.0
    items = segs if isinstance(segs, list) else []
    try:
        cut = max(0.0, float(short_gap))
    except (TypeError, ValueError):
        cut = 600.0
    for seg in reversed(items):
        if not isinstance(seg, dict):
            continue
        state = str(seg.get("s") or "")
        try:
            length = float(seg.get("len") or 0)
        except (TypeError, ValueError):
            continue
        if length <= 0:
            continue
        if state in ("interactive", "fullscreen"):
            run += length
            continue
        if length >= cut:
            # 撞上一次真正的离开：它结束了「这一段坐着」
            if run > 0:
                gap = length
            break
        # 短离开：人还在，只是手离开了键盘
        run += length
    if run <= 0:
        return 0.0, 0.0
    return gap, run


def moment_label(gap_sec: float, run_sec: float, hour: int) -> str:
    """把 (离开多久, 连续在场多久, 现在几点) 归纳成一个人看得懂的时机标签。

    只输出**事实性的时机**，不输出情绪建议 —— 「刚回来」是事实，
    「她该高兴」是角色的事，不是数据的。模型看到标签，要不要开口、
    用什么语气，由它和人设决定。

    标签按「值不值得说」排序，越靠前越该被优先看见：

    ========  ==========================  ==================
    标签      判据                        大致可做的事
    ========  ==========================  ==================
    久别      gap ≥ 2 天                  她记得你上次是什么时候
    刚回来    gap ≥ 1 小时                打个招呼是自然的
    深夜      00:00–05:00                 陪着，但别说教
    长坐      连续在场 ≥ 3 小时            可以轻提，但别催
    平常      以上都不满足                闭嘴是默认选项
    （空串）  当前不在场                  没有时机可言
    ========  ==========================  ==================

    阈值取 1 小时而不是 15 分钟：去买杯水就回来，说「好久不见」是假的。
    2 天才叫「久别」：隔夜睡一觉是常态，第二天见面不该像生离死别。
    """
    try:
        gap = max(0.0, float(gap_sec))
        run = max(0.0, float(run_sec))
    except (TypeError, ValueError):
        return ""
    if run <= 0:
        return ""
    try:
        h = int(hour)
    except (TypeError, ValueError):
        h = -1
    if gap >= 2 * 86400:
        return "久别"
    if gap >= 3600:
        return "刚回来"
    if 0 <= h < 5:
        return "深夜"
    if run >= 3 * 3600:
        return "长坐"
    return "平常"


def moment_level(moment: str, gap_sec: float, run_sec: float, hour: int) -> str:
    """给时机标签补一层「程度」—— 「长坐」从 3 小时到 9 小时，不该说同一句话。

    只有三个标签分程度，其余返回空串（没什么可分的）：

    ========  ==================  ==========================================
    标签      程度                为什么这么分
    ========  ==================  ==========================================
    久别      两三天 / 一周多 /    隔三天和隔一个月，见面的分量完全不同
              半个多月 / 一个多月
    长坐      三到五小时 /        3 小时是「坐了一会儿」，8 小时是「从早上
              五到八小时 /        坐到天黑」，中间要能分辨
              八小时以上
    深夜      零点后 / 快天亮了    刚过零点还能算夜里，四五点是另一回事
    ========  ==================  ==========================================

    这只是**分档话术的提示**，不是情绪指令。真正精确的时长在 ``sit`` 字段里，
    模型可以选任意一种说法，也可以什么都不说。
    """
    try:
        gap = max(0.0, float(gap_sec))
        run = max(0.0, float(run_sec))
    except (TypeError, ValueError):
        return ""
    try:
        h = int(hour)
    except (TypeError, ValueError):
        h = -1

    if moment == "久别":
        days = gap / 86400.0
        if days >= 30:
            return "一个多月"
        if days >= 14:
            return "半个多月"
        if days >= 7:
            return "一周多"
        return "两三天"
    if moment == "长坐":
        hours = run / 3600.0
        if hours >= 8:
            return "八小时以上"
        if hours >= 5:
            return "五到八小时"
        return "三到五小时"
    if moment == "深夜":
        return "快天亮了" if h >= 3 else "零点后"
    return ""


# ==========================================================
# 存储键结构（写进 Store 的都是这里 to_dict 的结果）
# ==========================================================
#
# current_v1 —— 当前段，**必须落盘**：
#     Segment.to_dict() 的结果。重启后按 last_m/last_t 续接；
#     若 last_t 距今超过离开阈值，说明上一段已结束，先结算再开新段。
#     ⚠️ peak_w 是"这段等待"的唯一记录，丢了就等于没等过。
#
# daily_v1 —— 按天聚合：
#     {"YYYY-MM-DD": DayStat.to_dict(), ...}，保留 730 天。
#
# marks_v1 —— 里程碑：
#     {"first_day": "YYYY-MM-DD",
#      "longest_wait": {"sec": float, "date": "YYYY-MM-DD"},
#      "best_companion_day": {"sec": float, "date": "YYYY-MM-DD"}}
