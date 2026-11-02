"""区间管理与日聚合测试。

构造一段模拟时间线跑完：在操作 → 离开 → 回来 → 全屏看片，

    python tests/test_state.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from core.judge import (  # noqa: E402
    AWAY,
    AWAY_FULLSCREEN,
    INTERACTIVE,
    LOCKED,
    SHORT_BREAK,
    Config,
    Sample,
)
from core.state import (  # noqa: E402
    DayStat,
    Segment,
    apply_segment,
    live_moment,
    moment_label,
    onset_is_late,
    roll_segment,
    step,
)

CFG = Config()
RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, note: str) -> None:
    RESULTS.append((ok, note))
    print(f"[{'OK  ' if ok else 'FAIL'}] {note}")


def near(a: float, b: float, tol: float = 0.6) -> bool:
    return abs(a - b) <= tol


def run_sequence(samples: list[Sample]):
    """跑完一串采样，返回 (最终 current, 所有结算出的段)。"""
    cur = Segment()
    closed: list[Segment] = []
    for sample in samples:
        result = step(cur, sample, CFG)
        cur = result.current
        if result.closed is not None:
            closed.append(result.closed)
    return cur, closed


# ---------------------------------------------------------------- 场景一
# 08:00 起在操作 10 分钟（21 条采样，w 很小），然后离开 40 分钟（w 涨到 2400+），
# 再回来。验证：陪伴用累加、等待用峰值、段正确切换。
def scenario_simple() -> None:
    samples: list[Sample] = []
    base = 1000.0
    # 在操作：21 条，间隔 30 秒 → 覆盖 600 秒
    for i in range(21):
        m = base + i * 30
        samples.append(Sample(t=m, m=m, w=float(i % 5) * 2, q=5))
    # 离开：41 条，w 从 60 涨到 2460（间隔 30 秒）
    for i in range(41):
        m = base + 600 + i * 30
        samples.append(Sample(t=m, m=m, w=60.0 + i * 60.0, q=5))

    cur, closed = run_sequence(samples)
    day = DayStat(date="2026-10-25")
    for seg in closed:
        apply_segment(day, seg, CFG)

    # w 从 60 涨到 2460 必然穿过 short_break 区间，所以是 2 次切换：
    # interactive → short_break → away。这是真实的状态渐变，不是多余事件。
    check(len(closed) == 2, f"状态切换结算出 2 个段（实得 {len(closed)}）")
    check(closed[0].state == INTERACTIVE, f"第 1 段是 interactive（实得 {closed[0].state}）")
    check(near(closed[0].duration, 600), f"陪伴段累加 ≈600 秒（实得 {closed[0].duration}）")
    check(closed[1].state == SHORT_BREAK, f"第 2 段是 short_break（实得 {closed[1].state}）")
    # 450 = short_break 期间 14 条同状态采样(14×30) + 切换那条的 delta(30)
    # 切换那条归旧段，是本模块的保守口径（旧状态确实持续到那一条为止）
    check(near(closed[1].duration, 450), f"短暂离席段 ≈450 秒（实得 {closed[1].duration}）")
    check(near(day.companion_sec, 600), f"日聚合 companion_sec ≈600（实得 {day.companion_sec}）")

    check(cur.state == AWAY, f"当前段是 away（实得 {cur.state}）")
    check(near(cur.peak_w, 2460), f"peak_w 取到峰值 2460（实得 {cur.peak_w}）")

    apply_segment(day, cur, CFG)
    # 这段还没回到陪伴，事件未结算 → 用 waiting_total_sec（含 open_wait_sec）
    check(near(day.waiting_total_sec, 2460), f"离开时长用峰值 ≈2460（实得 {day.waiting_total_sec}）")
    check(day.waiting_count == 1, f"等待次数 =1（实得 {day.waiting_count}）")
    check(near(day.short_break_sec, 450), f"短暂离席单列 ≈450（实得 {day.short_break_sec}）")
    check(day.longest_wait_sec >= 2460, "最长等待已更新")


# ---------------------------------------------------------------- 场景二
# 短暂离席 5 分钟（w=300）：算 short_break，不计入等待
def scenario_short_break() -> None:
    samples = [Sample(t=2000.0, m=2000.0, w=5.0, q=5)]
    for i in range(11):
        m = 2030.0 + i * 30
        samples.append(Sample(t=m, m=m, w=300.0, q=5))
    cur, closed = run_sequence(samples)
    day = DayStat(date="2026-10-25")
    for seg in closed:
        apply_segment(day, seg, CFG)
    check(cur.state == SHORT_BREAK, f"5 分钟离席判为 short_break（实得 {cur.state}）")
    apply_segment(day, cur, CFG)
    check(day.waiting_sec == 0, f"short_break 不计入等待（实得 {day.waiting_sec}）")
    check(day.short_break_sec > 0, "short_break 单列记录了时长")


# ---------------------------------------------------------------- 场景三
# 全屏看片 4 小时：前 3 小时算陪伴，超出 1 小时算等待
def scenario_fullscreen_tail() -> None:
    seg = Segment(
        state=AWAY_FULLSCREEN,
        start_t=3000.0,
        start_m=3000.0,
        duration=4 * 3600.0,
        peak_w=4 * 3600.0,
        samples=480,
    )
    day = DayStat(date="2026-10-25")
    apply_segment(day, seg, CFG)
    check(
        near(day.waiting_total_sec, 3600),
        f"全屏长尾：超出部分 ≈3600 秒算离开（实得 {day.waiting_total_sec}）",
    )
    check(near(day.companion_sec, 3 * 3600), f"前 3 小时仍算陪伴（实得 {day.companion_sec}）")


# ---------------------------------------------------------------- 场景四
# 采样间隔突然跳到 1000 秒（合盖/重启）：不计入时长，标记 stale
def scenario_stale() -> None:
    cur = Segment()
    r1 = step(cur, Sample(t=5000.0, m=5000.0, w=1.0, q=5), CFG)
    cur = r1.current
    r2 = step(cur, Sample(t=5030.0, m=5030.0, w=31.0, q=5), CFG)
    check(near(r2.step_sec, 30), f"正常间隔计入 30 秒（实得 {r2.step_sec}）")
    cur = r2.current
    # 间隔 1000 秒
    r3 = step(cur, Sample(t=6030.0, m=6030.0, w=1031.0, q=5), CFG)
    check(r3.stale is True, "大间隔被标记 stale")
    check(r3.step_sec == 0, f"大间隔不计入时长（实得 {r3.step_sec}）")


# ---------------------------------------------------------------- 场景五
# 锁屏：算等待，并单列 locked_sec
def scenario_locked() -> None:
    seg = Segment(
        state=LOCKED,
        start_t=7000.0,
        start_m=7000.0,
        duration=1800.0,
        peak_w=1800.0,
        samples=60,
    )
    day = DayStat(date="2026-10-25")
    apply_segment(day, seg, CFG)
    check(near(day.locked_sec, 1800), f"锁屏时长单列 ≈1800（实得 {day.locked_sec}）")
    check(near(day.waiting_total_sec, 1800), f"锁屏也算离开 ≈1800（实得 {day.waiting_total_sec}）")


# ---------------------------------------------------------------- 场景六
# 覆盖率：无记录≠不在
def scenario_coverage() -> None:
    """覆盖率 = 有采样时长 / 真实跨度（账本A）。

    分母必须是 ``span_sec``（first_seen → last_seen）。早期版本写成
    ``attributed / covered``，分子分母都是 duration 之和，**恒等于 1.00**，
    等于完全没做检查（2026-10-27 发现：10-26 实际覆盖率只有 0.76 却显示 1.00）。

    注意 ``last_t`` 必须设：``_track_seen`` 取的是最后一条采样的实际时刻，
    不是 ``start_t + duration``（duration 不含 stale 缺口）。
    """
    day = DayStat(date="2026-10-25")
    apply_segment(
        day,
        Segment(state=INTERACTIVE, start_t=100.0, last_t=700.0, duration=600.0, samples=20),
        CFG,
    )
    check(near(day.span_sec, 600), f"跨度 =600（实得 {day.span_sec}）")
    check(near(day.covered_ratio, 1.0), f"采样填满跨度时覆盖率 =1（实得 {day.covered_ratio:.2f}）")

    # 中间断 3300 秒（电脑待机）：跨度涨到 4500，但采样只有 1200
    apply_segment(
        day,
        Segment(state=INTERACTIVE, start_t=4000.0, last_t=4600.0, duration=600.0, samples=20),
        CFG,
    )
    check(near(day.span_sec, 4500), f"跨度涨到 4500（实得 {day.span_sec}）")
    check(near(day.covered_sec, 1200), f"有采样仍 1200（实得 {day.covered_sec}）")
    check(
        near(day.covered_ratio, 1200 / 4500, tol=0.01),
        f"有盲区时覆盖率 ≈0.267（实得 {day.covered_ratio:.3f}）",
    )

    empty = DayStat(date="2026-10-25")
    check(empty.covered_ratio == 0.0, "完全没数据时覆盖率 =0（不冒充'你不在'）")


def scenario_no_double_count() -> None:
    """回归门：各状态时长之和不得超过有采样时长。

    2026-10-25 真机日检发现：锁屏段同时写进 `locked_sec`（状态时长）和
    `waiting_sec`（peak_w 口径），而归因总额把两者一起加，导致
    陪伴+安静+短暂离席+锁屏 = 12.15h > 实际采样 10.70h。
    修法：归因改用 `away_sec`（duration 口径），`waiting_sec` 只作产品指标。
    """
    day = DayStat(date="2026-10-25")
    apply_segment(
        day,
        Segment(state=LOCKED, start_t=100.0, last_t=1900.0, duration=1800.0, peak_w=1800.0, samples=60),
        CFG,
    )
    apply_segment(
        day,
        Segment(state=AWAY, start_t=2000.0, last_t=2600.0, duration=600.0, peak_w=600.0, samples=20),
        CFG,
    )
    check(near(day.covered_sec, 2400), f"有采样 2400 秒（实得 {day.covered_sec}）")
    check(near(day.attributed_sec, 2400), f"归因总额 2400 秒（实得 {day.attributed_sec}）")
    # 跨度 2500（100 → 2600），有采样 2400 → 中间漏了 100 秒
    check(near(day.span_sec, 2500), f"跨度 2500（实得 {day.span_sec}）")
    check(
        near(day.covered_ratio, 2400 / 2500, tol=0.005),
        f"覆盖率 0.96 而非 1.00（实得 {day.covered_ratio:.3f}）—— 分母是跨度不是自己",
    )
    # 锁屏 1800s 接 away 600s 是**同一次离开**，峰值取 1800 而不是 1800+600。
    # 旧实现会累加成 2400 —— 那正是「段切得越碎、重复计算越多」的 bug。
    check(
        near(day.waiting_total_sec, 1800),
        f"同一次离开只取峰值 1800（实得 {day.waiting_total_sec}）—— 不是 1800+600",
    )
    check(day.waiting_count == 1, f"合并后只算 1 次离开（实得 {day.waiting_count}）")


def scenario_roll_segment() -> None:
    """切段兜底：同一个状态太久就提前结算，口径必须一分不差。

    日聚合只在段结算时更新。用户连续几小时不切换状态时，段就一直不结束，
    ``daily_v1`` 停在上次结算那一刻（2026-10-31 真机：内存里 4 小时，
    daily 只有 238 秒）。所以每 600 秒切一刀；这里是「切了之后账还对不对」
    的回归门。
    """

    def sample_at(offset: float, w: float) -> Sample:
        return Sample(t=1000.0 + offset, m=1000.0 + offset, w=w, q=5)

    def run_with_roll(samples: list[Sample], limit: float = 600.0):
        cur = Segment()
        day = DayStat(date="2026-10-31")
        cuts = 0
        for s in samples:
            result = step(cur, s, CFG)
            cur = result.current
            if result.closed is not None:
                apply_segment(day, result.closed, CFG)
            fresh = roll_segment(cur, limit)
            if fresh is not None:
                apply_segment(day, cur, CFG)
                cur = fresh
                cuts += 1
        return day, cur, cuts

    # ---- 不切的情况 ----
    seg = Segment(
        state=INTERACTIVE, start_t=1000.0, start_m=1000.0,
        last_t=1500.0, last_m=1500.0, duration=500.0, peak_w=3.0, samples=17,
    )
    check(roll_segment(seg, 600.0) is None, "500 秒未到 600 阈值 → 不切")
    check(roll_segment(seg, 0.0) is None, "roll_segment_sec=0 → 关闭这条兜底")

    # ---- 切了之后续段长什么样 ----
    seg = Segment(
        state=INTERACTIVE, start_t=1000.0, start_m=1000.0,
        last_t=1600.0, last_m=1600.0, duration=600.0, peak_w=3.0, samples=21,
    )
    fresh = roll_segment(seg, 600.0)
    check(fresh is not None, "600 秒到阈值 → 切")
    if fresh is not None:
        check(
            near(fresh.start_t, 1600) and near(fresh.last_t, 1600),
            "续段起点 = 原段终点（首尾相接，时间轴不留缝）",
        )
        check(
            near(fresh.start_m, 1600) and near(fresh.last_m, 1600),
            "续段单调时钟沿用 last_m（归零的话每条采样都会被判断档）",
        )
        check(fresh.duration == 0.0, "续段 duration 从 0 起")
        check(fresh.samples == 1, f"续段 samples=1（实得 {fresh.samples}）")
        check(near(fresh.peak_w, 3.0), "续段 peak_w 延续原段峰值（离开事件才不会断）")

    # ---- 全屏长尾段拒绝切：否则每段都重扣一次长尾 ----
    fs = Segment(
        state=AWAY_FULLSCREEN, start_t=0.0, start_m=0.0, last_t=20000.0,
        last_m=20000.0, duration=20000.0, peak_w=20000.0, samples=666,
    )
    check(roll_segment(fs, 600.0) is None, "away_fullscreen 不切（切成多段会重复扣 fullscreen_tail）")

    # ---- 切完段，下一条采样正常计入，不被判成断档 ----
    seg = Segment(
        state=INTERACTIVE, start_t=0.0, start_m=5000.0, last_t=3600.0,
        last_m=8600.0, duration=3600.0, peak_w=1.0, samples=120,
    )
    fresh = roll_segment(seg, 600.0)
    if fresh is not None:
        nxt = step(fresh, Sample(t=3630.0, m=8630.0, w=1.0, q=5), CFG)
        check(near(nxt.step_sec, 30, tol=0.5), f"切段后下一条采样计入 ≈30s（实得 {nxt.step_sec}）")
        check(nxt.closed is None, "切段后下一条采样不触发断档结算")

    # ---- 连续 4 小时 interactive，切 24 刀，陪伴总量不能变 ----
    total = 4 * 3600
    # 第一条采样只负责立起点、不计时长（step 的首条分支 step_sec=0），
    # 所以总量是 total - 30，跟切不切段无关
    expected = total - 30
    day, cur, cuts = run_with_roll([sample_at((i + 1) * 30.0, 1.0) for i in range(total // 30)])
    apply_segment(day, cur, CFG)
    check(cuts >= 20, f"4 小时切了 {cuts} 刀（应该每 600 秒一刀）")
    check(
        near(day.companion_sec, expected, tol=1.0),
        f"切段后陪伴仍 ≈{expected} 秒（实得 {day.companion_sec}）—— 不重复不丢失",
    )
    check(
        near(day.covered_sec, expected, tol=1.0),
        f"有采样仍 ≈{expected} 秒（实得 {day.covered_sec}）",
    )
    check(
        near(day.attributed_sec, day.covered_sec, tol=1.0),
        "归因总额 = 有采样时长（切段没有漏记某个分支）",
    )

    # ---- 一次离开被切很多段：仍只算 1 次，峰值不重复累加 ----
    # w 一路涨到 1800：interactive → short_break(120~900) → away(>900)
    day, cur, cuts = run_with_roll([sample_at((i + 1) * 30.0, float((i + 1) * 30)) for i in range(60)])
    apply_segment(day, cur, CFG)
    check(day.waiting_count == 1, f"同一次离开切了多段仍算 1 次（实得 {day.waiting_count}）")
    check(
        near(day.waiting_total_sec, 1800, tol=31),
        f"离开时长取峰值 ≈1800（实得 {day.waiting_total_sec}）—— 不是逐段相加",
    )


def scenario_unknown() -> None:
    """不可观测的采样：切断当前段，缺口不算陪伴、不算离开、不算覆盖。"""
    base = 1000.0
    cur, closed = run_sequence(
        [
            Sample(t=base, m=base, w=5.0, q=5, screen="normal"),
            Sample(t=base + 30, m=base + 30, w=5.0, q=5, screen="normal"),
            # 适配器拿不到任何信号 → UNKNOWN
            Sample(t=base + 60, m=base + 60, w=None, q=None, screen="unknown"),
            # 信号恢复：从第 90 秒重新起段，60~90 的缺口不属于任何段
            Sample(t=base + 90, m=base + 90, w=5.0, q=5, screen="normal"),
            Sample(t=base + 120, m=base + 120, w=5.0, q=5, screen="normal"),
        ]
    )
    check(len(closed) == 1, f"unknown 把已有段切出去（实得 {len(closed)} 段）")
    check(
        closed[0].last_t == base + 30,
        f"被切断的段停在最后一条有效采样（实得 {closed[0].last_t - base}）",
    )
    check(near(closed[0].duration, 30.0), f"切断前只累计 30 秒（实得 {closed[0].duration}）")
    check(
        cur.state == INTERACTIVE and cur.start_t == base + 90,
        "信号恢复后从第 90 秒重新起段",
    )
    check(cur.samples == 2, f"恢复后新段只含 2 条有效采样（实得 {cur.samples}）")

    day = DayStat(date="2026-11-01")
    for seg in closed:
        apply_segment(day, seg, CFG)
    apply_segment(day, cur, CFG)
    check(near(day.companion_sec, 60.0), f"缺口不算陪伴，只记 60 秒（实得 {day.companion_sec}）")
    check(near(day.covered_sec, 60.0), f"缺口不算覆盖，只记 60 秒（实得 {day.covered_sec}）")
    check(near(day.span_sec, 120.0), f"跨度仍到 120 秒（实得 {day.span_sec}）")
    check(near(day.covered_ratio, 0.5, tol=0.01), f"覆盖率诚实显示 50%（实得 {day.covered_ratio:.2f}）")
    check(day.waiting_total_sec == 0, "unknown 不会被编成一次离开")


def scenario_onset_caveat() -> None:
    """「记录起点晚于零点」的判据。

    这一条不是为了算对数字，而是为了**不让模型把空白说成不在**。
    2026-11-01 真机：插件 10:57 启动，companion_sec 1h45m，
    YUI 照读成「今天只陪了你 1 小时 45 分」。用户以为她上午不在。
    """
    d0 = 1_793_476_800.0  # 某天 00:00，只当基准用（不依赖时区）

    check(not onset_is_late(d0 + 600, d0), "首见 00:10 不算晚（用户自己就是那时开机）")
    check(not onset_is_late(d0 + 1800, d0), "首见 00:30 恰好不说（阈值取严格大于）")
    check(onset_is_late(d0 + 1801, d0), "首见 00:30:01 算晚")
    check(onset_is_late(d0 + 41_160, d0), "首见 11:26 算晚（11-01 真机就是这种）")
    check(onset_is_late(d0 + 81_120, d0), "首见 22:32 算晚")
    check(
        onset_is_late(d0 + 41_160, d0, threshold=40_000),
        "阈值可调：调到 40000 秒后同一时刻算晚",
    )
    check(not onset_is_late(0, d0), "没有首见时间时不猜")
    check(not onset_is_late(d0 + 41_160, 0), "拿不到零点时不猜")
    check(not onset_is_late(None, d0), "None 不抛异常")


def scenario_moment() -> None:
    """「此刻是什么时机」—— 倒扫时间轴算 gap / run，再归纳成标签。

    这两个数决定她会不会在不该说话的时候开口。2026-11-02 群里的原话是
    「主动搭话是有的，就是频率和内容还得调整」——频率问题的本质是时机问题。
    """
    on = "interactive"
    off = "away"

    # ---- live_moment：倒扫 ----
    check(live_moment([]) == (0.0, 0.0), "空时间轴 → (0, 0)")
    check(
        live_moment([{"o": 0, "len": 7200, "s": on}]) == (0.0, 7200.0),
        "一整天都在 → gap 0、run 全长",
    )
    check(
        live_moment([{"o": 0, "len": 3600, "s": on}, {"o": 3600, "len": 600, "s": off}])
        == (0.0, 0.0),
        "末尾是长离开（10 分钟整）→ 当前不在场，gap/run 都是 0",
    )
    check(
        live_moment(
            [
                {"o": 0, "len": 1800, "s": on},
                {"o": 1800, "len": 5400, "s": off},
                {"o": 7200, "len": 900, "s": on},
            ]
        )
        == (5400.0, 900.0),
        "on→off→on：gap 取那次离开、run 只算最后这段",
    )
    check(
        live_moment(
            [
                {"o": 0, "len": 600, "s": on},
                {"o": 600, "len": 300, "s": off},
                {"o": 900, "len": 600, "s": on},
                {"o": 1500, "len": 300, "s": off},
                {"o": 1800, "len": 1200, "s": on},
            ]
        )
        == (0.0, 3000.0),
        "短离开（各 5 分钟）不打断，全部并入「这次坐了多久」",
    )
    check(
        live_moment(
            [
                {"o": 0, "len": 1800, "s": on},
                {"o": 1800, "len": 900, "s": off},
                {"o": 2700, "len": 600, "s": on},
            ]
        )
        == (900.0, 600.0),
        "长离开（15 分钟）才算打断，run 只从它之后算",
    )
    check(
        live_moment(
            [{"o": 0, "len": 3600, "s": on}, {"o": 3600, "len": 300, "s": off}],
            short_gap=120.0,
        )
        == (0.0, 0.0),
        "阈值调小到 2 分钟后，末尾 5 分钟的离开也算离开",
    )

    # ---- moment_label：阈值与优先级 ----
    check(moment_label(0, 0, 15) == "", "不在场 → 空串（没有时机可言）")
    check(moment_label(3 * 86400, 3600, 15) == "久别", "三天没来 → 久别")
    check(moment_label(2 * 86400, 3600, 15) == "久别", "整整两天 → 久别（含等号）")
    check(moment_label(2 * 86400 - 1, 3600, 15) == "刚回来", "差一秒不到两天 → 刚回来")
    check(moment_label(3600, 3600, 15) == "刚回来", "正好一小时 → 刚回来（含等号）")
    check(moment_label(3599, 3600, 15) == "平常", "59 分 59 秒 → 还不算刚回来")
    check(moment_label(600, 3600, 2) == "深夜", "凌晨两点 → 深夜")
    check(moment_label(600, 3600, 5) == "平常", "五点整 → 不是深夜")
    check(moment_label(600, 3 * 3600, 15) == "长坐", "连续三小时 → 长坐")
    check(moment_label(600, 3 * 3600 - 1, 15) == "平常", "差一秒不满三小时 → 平常")
    check(moment_label(3 * 86400, 3600, 2) == "久别", "久别优先于深夜")
    check(moment_label("bad", 3600, 15) == "", "脏输入不抛异常")
    check(moment_label(0, 3600, "bad") == "平常", "小时数非法时退化成平常")


def main() -> int:
    for fn in (
        scenario_simple,
        scenario_short_break,
        scenario_fullscreen_tail,
        scenario_stale,
        scenario_locked,
        scenario_coverage,
        scenario_no_double_count,
        scenario_roll_segment,
        scenario_unknown,
        scenario_onset_caveat,
        scenario_moment,
    ):
        print(f"\n--- {fn.__name__}")
        fn()

    failed = sum(1 for ok, _ in RESULTS if not ok)
    print(f"\n共 {len(RESULTS)} 项，失败 {failed} 项")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
