"""她的一天 (neko_presence) —— V0.7.0：在场判定 + 跨端适配 + 按月翻页年历 + 初识日探测

同时跑两条链路：

1. **探针**（V0.1 保留）：原始采样写 chunk，供调参期逐条对照
   "这条为什么判成 away"。66 小时自动淘汰，不会无限增长。
2. **状态机**（V0.2 新增）：判定在场状态 → 段结算 → 日聚合。

本文件只做编排，不含业务判断：

    core/signals.py  取信号（键鼠空闲 / 锁屏全屏 / 前台进程）
    core/judge.py    单点判定（纯函数，六态）
    core/state.py    段管理与日聚合（陪伴累加、等待取峰值）
    core/hostmem.py  向宿主记忆借一个日期（只读时间戳，定年历左边界）

采样字段（探针 chunk）
----------------------
    t  wall clock 秒         对齐真实时间；也是「最后见到你」的锚点
    m  单调时钟秒            识别合盖/休眠造成的跳变
    p  privacy_state         宿主快照，本机恒 unavailable
    i  system_idle_seconds   宿主快照，本机恒 None
    c  foreground_category   宿主快照，本机恒空
    w  win_idle              GetLastInputInfo，**当前主判据**
    q  notification_state    1=锁屏 2=全屏 3=全屏游戏 4=演示 5=正常 6=刚进全屏
    f  foreground_exe        前台进程名（仅文件名）

存储键
------
    probe_index / probe:<ts>-<seq>   探针原始采样（66 小时）
    current_v1                       当前段（**必须落盘**，peak_w 是等待的唯一记录）
    daily_v1                         按天聚合，保留 retention_days
    marks_v1                         里程碑
    since_v1                         初识日（向宿主记忆借的那一个日期，只存日期不存内容）
    settings                         用户覆盖层（含 paused 暂停开关）
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
    timer_interval,
    tr,
    ui,
)

from .core.adapters import build_adapter
from .core.hostmem import date_of as _date_of
from .core.hostmem import months_ago
from .core.hostmem import probe_since
from .core.judge import Config as JudgeConfig
from .core.judge import Sample
from .core.state import DayStat, Segment, apply_segment, onset_is_late, roll_segment, step

_INDEX_KEY = "probe_index"
_CHUNK_PREFIX = "probe:"
_STORE_CURRENT = "current_v1"
_STORE_DAILY = "daily_v1"
_STORE_MARKS = "marks_v1"
_STORE_SETTINGS = "settings"
# 分段时间轴：daily_v1 只有聚合，画不出「几点在、几点走」。
# 按天单独存一个 key，每次只写当天（几十段、3KB 左右），不做全量重写。
_STORE_SEG_INDEX = "seg_index"
_SEG_PREFIX = "seg:"

#: 与 plugin.toml 的 version 保持一致（启动日志里打出来，方便确认装载的是哪一版）
_VERSION = "0.7.0"

#: 年历左翻的边界（初识日）探测结果。见 :meth:`_resolve_since` 与 core/hostmem.py。
#: 注意：**不再硬编码日期**。写死一个日期意味着每个用户都会看到别人的纪念日。
#: settings.since_date 仍可覆盖，且用户设定优先于探测结果。
_STORE_SINCE = "since_v1"

#: 探测结果最多往回追多少个月。老用户记忆可能横跨数年，
#: 界面按整月翻页，不设上限就是几百页的空月历。
_SINCE_FLOOR_MONTHS = 36

# 采样写入失败的提示节流（秒）：避免存储坏掉时每拍一条日志
_ERR_LOG_GAP_SEC = 300.0
# 同一个状态最长累计多少秒就提前结算一次（见 _roll_current）。
# 600 秒 = 20 条采样，最坏情况下 daily_v1 只滞后 10 分钟；
# 一天最多切 144 段，90 天保留期下也就一万多条，存储压力可忽略
_ROLL_SEC_DEFAULT = 600
# 面板/API 取数入口一律对 Agent 路由隐藏
_AGENT_HIDDEN = {"agent_hidden": True}


def _section(raw: Any) -> dict:
    """把宿主配置片段归一成 dict；非 dict 一律当空段。"""
    return dict(raw) if isinstance(raw, dict) else {}


def _as_float(raw: Any) -> float | None:
    """宽松转 float：坏值 / NaN 一律 None。"""
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if val == val else None


def _as_int(raw: Any, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _fmt_dur(sec: Any) -> str:
    """把秒数说成人话，**与面板 ``dur()`` 完全同一套规则**。

    面板（ui/panel.tsx）:
        <60 秒 → "N 秒"；<60 分 → "N 分钟"（向下取整）；
        否则 → "H 小时 M 分"（M=0 时省略）。

    这个函数存在的唯一理由：聊天里 YUI 说的数字必须和面板逐字一致。
    两边各写一套四舍五入，迟早又对不上（2026-11-01 真机踩到）。
    """
    # 用「四舍五入到秒」而不是 Python 的 round()：后者是银行家舍入，
    # 和面板 JS 的 Math.round 在 .5 秒上会差 1 秒 —— 聊天与面板又会对不上
    s = max(0, int(max(0.0, _as_float(sec) or 0.0) + 0.5))
    if s < 60:
        return f"{s} 秒"
    mins = s // 60
    if mins < 60:
        return f"{mins} 分钟"
    hours, mm = divmod(mins, 60)
    return f"{hours} 小时 {mm} 分" if mm else f"{hours} 小时"


@neko_plugin
class NekoPresencePlugin(NekoPluginBase):
    """她的一天：记录在场与离开，不做提醒、不做建议。"""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)

        # ---- [probe] 探针配置 ----
        self._cfg: dict = {}
        # ---- [presence] 状态机配置 ----
        self._pcfg: dict = {}

        # ---- 探针运行态 ----
        self._buf: list[dict] = []
        self._index: list[str] = []
        self._seq = 0
        self._last_sample_ts = 0.0
        self._sample_count = 0
        self._err_logged = 0.0
        self._raw_dumped = False

        # ---- 状态机运行态 ----
        self._current = Segment()
        self._daily: dict[str, DayStat] = {}
        self._marks: dict = {}
        self._settings: dict = {}
        #: 初识日（年历左翻下界）。启动时由 _resolve_since 定下，空串表示还没算
        self._since = ""
        #: 分段时间轴：{"YYYY-MM-DD": [{"s":状态,"t":起点,"d":时长,"p":峰值}, ...]}
        self._segments: dict[str, list] = {}
        self._seg_index: list[str] = []
        #: 上一拍的状态，用于只在切换时打日志（不刷屏）
        self._last_state = ""
        self._judge_cfg = JudgeConfig()

        # ---- 平台适配层 ----
        # Windows 走原生 ctypes；其他平台退回宿主快照；两者都没有就诚实空转。
        # 业务层只拿语义 Signal，不再直接碰 OS API（见 core/adapters.py）。
        host_snapshot = None
        try:
            from plugin.sdk.plugin import get_os_activity_snapshot

            host_snapshot = lambda: get_os_activity_snapshot(self.plugin_id)  # noqa: B023
        except Exception:  # noqa: BLE001 - 旧 SDK 没有这个接口就只用原生
            host_snapshot = None
        self._host_snapshot = host_snapshot
        self._adapter = build_adapter(host_snapshot)
        #: 「没有可观测信号」日志节流（秒）
        self._no_signal_logged = 0.0

    # ==========================================================
    # 生命周期
    # ==========================================================

    @lifecycle(id="startup")
    async def startup(self, **_: Any):
        await self._refresh_config()
        await self._load_settings()
        await self._load_index()
        await self._load_daily()
        await self._resolve_since()
        await self._load_marks()
        await self._load_segments()
        deduped = self._dedupe_segments()
        if deduped:
            for day, items in self._segments.items():
                await self.store.set(f"{_SEG_PREFIX}{day}", items)
            self.logger.info("segments deduped on {} day(s)", deduped)
        # 分段结构是 V0.3 才有的，旧数据只有聚合没有分段 → 时间轴会空。
        # 探针 chunk 里存着完整历史，重放一次就能补上轮廓。
        # 只在分段明显不足时跑（正常跑一两天后 segments 会自然增长，不再触发）
        if len(self._segments) < 2:
            filled = await self._backfill_segments()
            if filled.get("filled"):
                self.logger.info("segments backfilled on startup: {}", filled)
        await self._restore_current()
        self.logger.info(
            "neko_presence v{} started: probe={} interval={}s interact={}s paused={} "
            "T={}s fullscreen_tail={}h days={} chunks={} segs={} src={}",
            _VERSION,
            self._probe_enabled(),
            self._interval_sec(),
            int(self._judge_cfg.interactive_sec),
            self._paused(),
            int(self._judge_cfg.silent_leave_sec),
            self._judge_cfg.fullscreen_tail_sec / 3600.0,
            len(self._daily),
            len(self._index),
            len(self._segments),
            self._adapter.source,
        )
        return Ok({"status": "ok", "days": len(self._daily), "chunks": len(self._index)})

    @lifecycle(id="shutdown")
    async def shutdown(self, **_: Any):
        # 退出前把两块都落盘：采样缓冲 + 当前段（peak_w 丢了就等于没等过）
        await self._flush()
        await self._save_current()
        return Ok({"status": "ok", "samples": self._sample_count})

    @lifecycle(id="config_change")
    async def config_change(self, **_: Any):
        await self._refresh_config()
        return Ok({"status": "ok", "interval_sec": self._interval_sec()})

    # ==========================================================
    # 配置
    # ==========================================================

    async def _refresh_config(self) -> None:
        """读取 plugin.toml。失败时保留上一份，不因为一次读失败停摆。"""
        try:
            raw = await self.config.dump(timeout=5.0)
        except Exception as exc:  # noqa: BLE001 - 配置读失败不该拖垮插件
            self.logger.warning("config dump failed: {}", type(exc).__name__)
            return
        root = _section(raw)
        self._cfg = _section(root.get("probe"))
        self._pcfg = _section(root.get("presence"))

        self._judge_cfg = JudgeConfig(
            interactive_sec=float(_as_int(self._pcfg.get("interactive_sec"), 120) or 120),
            silent_leave_sec=float(_as_int(self._pcfg.get("silent_leave_sec"), 900) or 900),
            fullscreen_tail_sec=float(
                _as_int(self._pcfg.get("fullscreen_tail_hours"), 3) or 3
            )
            * 3600.0,
        )

    def _probe_enabled(self) -> bool:
        return self._cfg.get("enabled", True) is not False

    def _presence_enabled(self) -> bool:
        return self._pcfg.get("enabled", True) is not False

    def _paused(self) -> bool:
        """用户显式暂停（Store 覆盖层优先于 toml 默认）。

        宿主的 `privacy_state` 在本机恒为 unavailable，等于没有隐私保护，
        所以必须靠自己这个开关（DESIGN.md §6.5）。
        """
        if self._settings.get("paused") is True:
            return True
        return self._pcfg.get("paused") is True

    def _interval_sec(self) -> int:
        return max(10, _as_int(self._cfg.get("interval_sec"), 30))

    def _flush_every(self) -> int:
        return max(1, _as_int(self._cfg.get("flush_every"), 20))

    def _max_chunks(self) -> int:
        return max(1, _as_int(self._cfg.get("max_chunks"), 400))

    def _retention_days(self) -> int:
        return max(7, _as_int(self._pcfg.get("retention_days"), 730))

    def _tz(self):
        name = str(self._pcfg.get("timezone") or "auto").strip()
        if not name or name == "auto":
            return None  # None = 系统本地
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - 时区名非法就退回本地
            return None

    def _day_key(self, ts: float) -> str:
        try:
            return datetime.fromtimestamp(float(ts), self._tz()).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            return datetime.now().strftime("%Y-%m-%d")

    def _clock(self, ts: Any) -> str:
        """把 wall clock 秒说成 ``HH:MM``；无效值返回空串（与面板 ``clock()`` 一致）。"""
        try:
            return datetime.fromtimestamp(float(ts), self._tz()).strftime("%H:%M")
        except (TypeError, ValueError, OSError):
            return ""

    def _day_start(self, ts: float) -> float:
        """当天 0 点的 wall clock 秒。

        时间轴下发的是「当天第几秒」而不是绝对时间戳 —— 前端只做
        「秒 / 86400」的百分比换算，不在 TSX 里碰时区（那边没有 ZoneInfo）。
        """
        try:
            dt = datetime.fromtimestamp(float(ts), self._tz())
            return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        except (TypeError, ValueError, OSError):
            return 0.0

    # ==========================================================
    # 采样 + 推进
    # ==========================================================

    @timer_interval(id="presence_probe", seconds=10, auto_start=True)
    async def probe_tick(self, **_: Any):
        """每 10 秒被唤醒一次，内部按 [probe].interval_sec 二次节流。

        timer 的 seconds 必须是字面量、读不了配置，而采样间隔恰恰是最需要
        随手调的旋钮，所以在这里二次节流。
        """
        if not self._probe_enabled():
            return Ok({"skipped": "disabled"})
        if self._paused():
            return Ok({"skipped": "paused"})

        now = time.time()
        if now - self._last_sample_ts < self._interval_sec():
            return Ok({"skipped": "interval"})
        self._last_sample_ts = now

        taken = await self._take_sample()
        if taken is None:
            return Ok({"skipped": "no_signal"})
        record, sample = taken
        self._sample_count += 1

        if self._sample_count == 1:
            self.logger.info(
                "first sample: privacy={} sdk_idle={} win_idle={} q={} f={!r}",
                record.get("p"),
                record.get("i"),
                record.get("w"),
                record.get("q"),
                record.get("f"),
            )

        # ---- 链路 1：探针原始采样 ----
        self._buf.append(record)
        if len(self._buf) >= self._flush_every():
            await self._flush()

        # ---- 链路 2：状态机 ----
        if self._presence_enabled():
            result = step(self._current, sample, self._judge_cfg)
            self._current = result.current
            if result.state != self._last_state:
                self.logger.info(
                    "state -> {} (w={} q={} f={!r})",
                    result.state,
                    record.get("w"),
                    record.get("q"),
                    record.get("f"),
                )
                self._last_state = result.state
            if result.closed is not None:
                await self._absorb(result.closed)
            # 同一个状态连续太久也要切一刀：daily_v1 只在段结算时更新，
            # 段不结束它就永远停在旧值（2026-10-31 真机：连续 4 小时
            # interactive，daily 里只有 238 秒）。切段只是把已累计的时长
            # 落进 daily，不改变任何口径（见 _roll_current）。
            rolled = self._roll_current()
            if rolled is not None:
                await self._absorb(rolled, reason="rolled")
            await self._save_current()

        return Ok(
            {
                "sampled": True,
                "buffered": len(self._buf),
                "state": self._last_state,
            }
        )

    async def _take_sample(self) -> tuple[dict, Sample] | None:
        """通过平台适配层取一条语义采样。

        适配器负责「在哪个平台、用哪套 API」；本方法只负责把语义 ``Signal``
        原样记进探针、转成状态机需要的 ``Sample``。业务层看不到 ctypes，
        也看不到 ``SHQueryUserNotificationState`` 这类 Windows 细节。

        返回 ``None`` 表示**完全没有可观测信号**（例如非 Windows 且宿主快照
        也不可用，或隐私状态不是 visible）。此时不写采样、不开段 ——
        「不知道」不能变成「陪着你」。
        """
        sig = await self._adapter.sample()

        if not sig.usable:
            now = time.time()
            if now - self._no_signal_logged > _ERR_LOG_GAP_SEC:
                self._no_signal_logged = now
                self.logger.warning(
                    "no observable signal: src={} privacy={} idle={} screen={}",
                    sig.source,
                    sig.privacy,
                    sig.idle_sec,
                    sig.screen,
                )
            return None

        if not self._raw_dumped:
            self._raw_dumped = True
            self.logger.info(
                "first sample: src={} privacy={} sdk_idle={} win_idle={} screen={} q={} f={!r}",
                sig.source,
                sig.privacy,
                sig.sdk_idle,
                sig.idle_sec,
                sig.screen,
                sig.q,
                sig.foreground,
            )

        record = {
            "t": round(sig.t, 1),
            "m": round(sig.m, 1),
            "p": sig.privacy,
            "i": sig.sdk_idle,
            "c": sig.category,
            "w": sig.idle_sec,
            "q": sig.q,
            "f": sig.foreground,
            # V0.3.1 新增语义字段：回放/排障时不必再猜 q 码属于哪套 OS
            "sc": sig.screen,
            "src": sig.source,
        }
        sample = Sample(
            t=sig.t,
            m=sig.m,
            w=sig.idle_sec,
            q=sig.q,
            f=sig.foreground,
            screen=sig.screen,
        )
        return record, sample

    async def _absorb(self, seg: Segment, reason: str = "closed") -> None:
        """把结算出的段记进当日聚合与里程碑。

        ``reason`` 只用于日志：``closed`` = 状态切换或断档切断（真结算），
        ``rolled`` = 段太长提前切（见 :meth:`_roll_current`），两者口径完全一致。
        """
        key = self._day_key(seg.start_t)
        stat = self._daily.get(key) or DayStat(date=key)
        apply_segment(stat, seg, self._judge_cfg)
        self._daily[key] = stat
        self._trim_daily()
        self._update_marks(stat, seg)
        await self._save_daily()
        await self._save_marks()
        await self._append_segment(key, seg)
        self.logger.info(
            "segment {}: {} dur={:.0f}s peak_w={:.0f}s day={}",
            reason,
            seg.state,
            seg.duration,
            seg.peak_w,
            key,
        )

    def _roll_sec(self) -> int:
        """同一个状态最长累计多少秒就切一刀（``0`` = 关掉这条兜底）。"""
        raw = _as_int(self._pcfg.get("roll_segment_sec"), _ROLL_SEC_DEFAULT)
        return max(0, min(86400, raw))

    def _roll_current(self) -> Segment | None:
        """当前段够长就提前结算，返回被切下来的旧段（没切返回 ``None``）。

        判断规则全在 :func:`core.state.roll_segment`（纯函数，可单测），
        这里只负责把 ``self._current`` 换成续段。
        """
        fresh = roll_segment(self._current, float(self._roll_sec()))
        if fresh is None:
            return None
        closed = self._current
        self._current = fresh
        return closed

    def _trim_daily(self) -> None:
        keep = self._retention_days()
        if len(self._daily) <= keep:
            return
        for key in sorted(self._daily)[: len(self._daily) - keep]:
            self._daily.pop(key, None)

    # ---------- 分段时间轴 ----------

    def _segments_retention_days(self) -> int:
        """分段时间轴保留天数，比 ``daily_v1``（730 天）短得多。

        分段是几 KB/天，聚合只有几百字节。90 天足够画月历和趋势，
        更早的日子只留聚合数字 —— 面板要画的是"最近"，不是"历史全量"。
        """
        return max(1, min(365, _as_int(self._pcfg.get("segments_retention_days"), 90)))

    async def _load_segments(self) -> None:
        res = await self.store.get(_STORE_SEG_INDEX)
        if isinstance(res, Err):
            return
        raw = res.value
        if not isinstance(raw, list):
            return
        self._seg_index = [str(x) for x in raw if x]
        for day in self._seg_index:
            r = await self.store.get(f"{_SEG_PREFIX}{day}")
            if isinstance(r, Ok) and isinstance(r.value, list):
                self._segments[day] = list(r.value)

    def _dedupe_segments(self) -> int:
        """修掉同起点的重复段（保留终点更晚的那条），返回改动天数。

        回填写过一条、实时又写了同起点的一条时，时间轴上同一段会画两遍。
        启动加载后跑一次，顺手把已经产生的重复也修掉。
        """
        fixed_days = 0
        for day, items in list(self._segments.items()):
            if not isinstance(items, list) or len(items) < 2:
                continue
            best: dict = {}
            for it in items:
                if not isinstance(it, dict):
                    continue
                key = int(it.get("t") or 0)
                prev = best.get(key)
                if prev is None or int(it.get("e") or 0) > int(prev.get("e") or 0):
                    best[key] = it
            if len(best) != len(items):
                self._segments[day] = sorted(best.values(), key=lambda x: int(x.get("t") or 0))
                fixed_days += 1
        return fixed_days

    async def _append_segment(self, day_key: str, seg: Segment) -> None:
        """把这一段的轮廓记进当天的时间轴。

        字段名压到 1 个字符是刻意的：90 天 × 每天几十段，长期存储就靠这个省。
        ``peak_w`` 也一起存 —— 将来想换时长口径重算历史，不用重新采集。
        """
        items = self._segments.setdefault(day_key, [])
        item = {
            "s": seg.state,
            # t/e 是段的真实起止（wall clock），时间轴按它们定位；
            # d 只是「有采样的时长」，缺口不计入 d —— 两者必须分开存，
            # 否则一段持续 77 分钟但只采到 10 分钟的段会被画成 10 分钟宽
            "t": round(seg.start_t),
            "e": round(seg.last_t or seg.start_t),
            "d": round(seg.duration),
            "p": round(seg.peak_w),
        }
        # 兜底去重：回填可能已经写过同起点同状态的段，实时结算时应替换而不是
        # 追加，否则时间轴上同一段画两遍（一侧截止到回填时刻、一侧继续延长）
        if (
            items
            and str(items[-1].get("s")) == item["s"]
            and int(items[-1].get("t") or 0) == int(item["t"])
        ):
            items[-1] = item
        else:
            items.append(item)
        res = await self.store.set(f"{_SEG_PREFIX}{day_key}", items)
        if isinstance(res, Err):
            now = time.time()
            if now - self._err_logged > _ERR_LOG_GAP_SEC:
                self._err_logged = now
                self.logger.warning("persist segments failed: {}", res.error)
            return
        if day_key not in self._seg_index:
            self._seg_index.append(day_key)
            await self._trim_segments()

    async def _trim_segments(self) -> None:
        keep = self._segments_retention_days()
        self._seg_index = sorted(set(self._seg_index))[-keep:]
        alive = set(self._seg_index)
        for day in [k for k in self._segments if k not in alive]:
            self._segments.pop(day, None)
            try:
                await self.store.delete(f"{_SEG_PREFIX}{day}")
            except Exception:  # noqa: BLE001 - 淘汰失败不影响新段
                pass
        await self.store.set(_STORE_SEG_INDEX, self._seg_index)

    async def _backfill_segments(self) -> dict:
        """用探针原始采样重放，补齐缺失日期的分段时间轴。

        为什么需要：``seg:<date>`` 是 V0.3 才有的结构，此前的日期没有分段，
        面板上时间轴会是空的。而 probe chunk 保留 66 小时，里面有完整历史。

        - **只填完全没有分段数据的日期**，已有的真实结算数据一律不覆盖
          （真实数据带 `live` 语义和 gap 边界，重放的只是近似）
        - 回填结果**不参与 daily 聚合**：聚合早就结算过了，这里只补时间轴轮廓
        - 不自动跑：重放几千条采样虽快，但没必要每次启动都做
        """
        if not self._index:
            return {"filled": 0, "reason": "no_chunks"}

        rows: list[dict] = []
        for key in self._index:
            res = await self.store.get(key)
            if isinstance(res, Ok) and isinstance(res.value, dict):
                items = res.value.get("items")
                if isinstance(items, list):
                    rows.extend(r for r in items if isinstance(r, dict))
        if not rows:
            return {"filled": 0, "reason": "no_samples"}
        rows.sort(key=lambda r: float(r.get("t") or 0))

        buckets: dict[str, list] = {}
        cur = Segment()
        for row in rows:
            try:
                sample = Sample(
                    t=float(row["t"]),
                    m=float(row.get("m") or row["t"]),
                    w=row.get("w"),
                    q=row.get("q"),
                    f=str(row.get("f") or ""),
                )
            except (KeyError, TypeError, ValueError):
                continue
            result = step(cur, sample, self._judge_cfg)
            cur = result.current
            if result.closed is not None:
                self._bucket_add(buckets, result.closed)
        # 最后那个还没结算的 `cur` **故意不写** —— 它就是此刻正在进行的段，
        # 实时结算时会自己写一条。若这里也写，时间轴上同一段会出现两条
        # （回填的截止到回填时刻，实时的继续延长）。2026-10-29 真机踩到：
        # 21:54:16 起点的 interactive 段同时存在 22:34:31 和 22:45:15 两个终点。

        filled = 0
        skipped = 0
        for day, items in buckets.items():
            if self._segments.get(day):
                skipped += 1          # 已有真实分段，别覆盖
                continue
            self._segments[day] = items
            res = await self.store.set(f"{_SEG_PREFIX}{day}", items)
            if isinstance(res, Err):
                self._segments.pop(day, None)
                continue
            if day not in self._seg_index:
                self._seg_index.append(day)
            filled += 1
        if filled:
            self._seg_index = sorted(set(self._seg_index))
            await self.store.set(_STORE_SEG_INDEX, self._seg_index)
        return {
            "filled": filled,
            "skipped": skipped,
            "days": sorted(buckets),
            "samples": len(rows),
        }

    def _bucket_add(self, buckets: dict[str, list], seg: Segment) -> None:
        key = self._day_key(seg.start_t)
        buckets.setdefault(key, []).append(
            {
                "s": seg.state,
                "t": round(seg.start_t),
                "e": round(seg.last_t or seg.start_t),
                "d": round(seg.duration),
                "p": round(seg.peak_w),
            }
        )

    def _update_marks(self, stat: DayStat, seg: Segment) -> None:
        if not self._marks:
            self._marks = {"first_day": stat.date}
        # 用 longest_wait_sec 而不是 waiting_sec 作前置条件：
        # waiting_sec 只在离开事件"结算"时增加，而 longest 是实时更新的，
        # 当天最后一段离开还在进行中时 waiting_sec 可能仍是 0
        if stat.longest_wait_sec > 0 and (
            stat.longest_wait_sec
            > float(_section(self._marks.get("longest_wait")).get("sec") or 0.0)
        ):
            self._marks["longest_wait"] = {
                "sec": round(stat.longest_wait_sec, 1),
                "date": stat.date,
            }
        if stat.companion_sec > float(
            _section(self._marks.get("best_companion_day")).get("sec") or 0.0
        ):
            self._marks["best_companion_day"] = {
                "sec": round(stat.companion_sec, 1),
                "date": stat.date,
            }

    # ==========================================================
    # 落盘
    # ==========================================================

    async def _load_settings(self) -> None:
        res = await self.store.get(_STORE_SETTINGS)
        if isinstance(res, Ok) and isinstance(res.value, dict):
            self._settings = dict(res.value)

    async def _save_settings(self) -> None:
        res = await self.store.set(_STORE_SETTINGS, dict(self._settings))
        if isinstance(res, Err):
            self.logger.warning("persist settings failed: {}", res.error)

    # ==========================================================
    # 初识日
    # ==========================================================

    async def _resolve_since(self) -> None:
        """定下年历左翻的下界：你和她的第一天。

        证据优先级（细节见 ``core/hostmem.py``）：

            宿主记忆里最早的对话  >  记忆迁移种子  >  宿主初始化日
            >  本插件第一条记录  >  今天

        有两条设计约束：

        1. **只读一次**，结果落 store。记忆库有五千多行，5 秒轮询里扫库是浪费；
           而且宿主正写着的时候会 ``database is locked``，读失败不该影响采样。
        2. **边界只往前认，不往回跳**。历史上认定过 6 月，就不该因为今天读不到
           记忆（用户清了记忆、换了机器、库被锁）而变成 10 月 —— 那是抹掉历史。
        3. 探测到现在约 3ms，所以放在 startup 里同步做，不值得开线程。
        """
        cached = ""
        res = await self.store.get(_STORE_SINCE)
        if isinstance(res, Ok) and isinstance(res.value, str):
            cached = _date_of(res.value) or ""

        probed, evidence = probe_since()
        own = min(self._daily) if self._daily else ""

        marks = [d for d in (probed, cached, own) if d]
        # 都没证据就老实从今天开始数；不拿一个写死的日期假装「我们认识很久了」
        since = min(marks) if marks else datetime.now().strftime("%Y-%m-%d")
        # 回追上限：老用户记忆可能横跨数年，整月翻页会变成几百页空月历
        since = max(since, months_ago(datetime.now(), _SINCE_FLOOR_MONTHS))

        self._since = since
        if since != cached:
            res = await self.store.set(_STORE_SINCE, since)
            if isinstance(res, Err):
                self.logger.warning("persist since failed: {}", res.error)

        self.logger.info(
            "since resolved: {} (src={} talk={} persona={} host={} cached={} own={} char={})",
            since,
            evidence.get("src") or "none",
            evidence.get("talk") or "-",
            evidence.get("persona") or "-",
            evidence.get("host") or "-",
            cached or "-",
            own or "-",
            evidence.get("char") or "-",
        )

    async def _load_index(self) -> None:
        res = await self.store.get(_INDEX_KEY)
        if isinstance(res, Ok) and isinstance(res.value, list):
            self._index = [str(key) for key in res.value if str(key)]

    async def _save_index(self) -> None:
        res = await self.store.set(_INDEX_KEY, list(self._index))
        if isinstance(res, Err):
            self.logger.warning("persist probe_index failed: {}", res.error)

    async def _load_daily(self) -> None:
        res = await self.store.get(_STORE_DAILY)
        if isinstance(res, Ok) and isinstance(res.value, dict):
            self._daily = {
                str(key): DayStat.from_dict(value)
                for key, value in res.value.items()
                if isinstance(value, dict)
            }

    async def _save_daily(self) -> None:
        payload = {key: stat.to_dict() for key, stat in self._daily.items()}
        res = await self.store.set(_STORE_DAILY, payload)
        if isinstance(res, Err):
            self.logger.warning("persist daily failed: {}", res.error)

    async def _load_marks(self) -> None:
        res = await self.store.get(_STORE_MARKS)
        if isinstance(res, Ok) and isinstance(res.value, dict):
            self._marks = dict(res.value)

    async def _save_marks(self) -> None:
        res = await self.store.set(_STORE_MARKS, dict(self._marks))
        if isinstance(res, Err):
            self.logger.warning("persist marks failed: {}", res.error)

    async def _restore_current(self) -> None:
        """恢复上次的当前段。若已过期（插件停了很久），先结算再开新段。"""
        res = await self.store.get(_STORE_CURRENT)
        if not isinstance(res, Ok) or not isinstance(res.value, dict):
            return
        seg = Segment.from_dict(res.value)
        if seg.samples == 0:
            return
        gap = time.time() - seg.last_t if seg.last_t else float("inf")
        if gap > self._judge_cfg.silent_leave_sec:
            # 中间插件没运行：这一段的结尾是"无记录"，不是"你一直在"
            await self._absorb(seg)
            self._current = Segment()
            self.logger.info(
                "stale segment closed on restore: {} gap={:.0f}s",
                seg.state,
                gap,
            )
            return
        self._current = seg
        self._last_state = seg.state

    async def _save_current(self) -> None:
        res = await self.store.set(_STORE_CURRENT, self._current.to_dict())
        if isinstance(res, Err):
            self.logger.warning("persist current failed: {}", res.error)

    async def _flush(self) -> None:
        """把探针缓冲写成一块。写失败塞回缓冲头部，下一拍重试。"""
        if not self._buf:
            return
        chunk = self._buf
        self._buf = []
        self._seq += 1
        key = f"{_CHUNK_PREFIX}{int(time.time())}-{self._seq}"

        res = await self.store.set(key, {"v": 1, "items": chunk})
        if isinstance(res, Err):
            self._buf = chunk + self._buf
            now = time.time()
            if now - self._err_logged > _ERR_LOG_GAP_SEC:
                self._err_logged = now
                self.logger.warning("persist probe chunk failed: {}", res.error)
            return

        self._index.append(key)
        keep = self._max_chunks()
        if len(self._index) > keep:
            overflow = self._index[: len(self._index) - keep]
            self._index = self._index[len(self._index) - keep :]
            for old_key in overflow:
                try:
                    await self.store.delete(old_key)
                except Exception:  # noqa: BLE001 - 淘汰失败不影响新块
                    pass
        await self._save_index()

    # ==========================================================
    # 面板数据源
    # ==========================================================

    @ui.context(id="dashboard")
    async def dashboard(self) -> dict:
        """面板数据源（宿主轮询入口）。

        宿主的 dashboard 轮询**只在面板聚焦时跑**，切走后会停 —— 所以面板还
        自己按 5 秒间隔调 ``presence_snapshot`` 拉同一个快照（见 ui/panel.tsx）。
        两者共用 :meth:`_snapshot`，保证口径一致。
        """
        return self._snapshot()

    def _day_view(self, stat: DayStat, day_key: str) -> DayStat:
        """返回一份「含当前未结算段」的日聚合副本（只读，不写盘）。

        ⚠️ ``daily_v1`` 只在**段结算（状态切换）**时才更新。用户连续几小时停在
        同一个状态（一直在操作、一直没离开），聚合就永远停在"上次结算那一刻"，
        面板会显示成几小时前的旧数字 —— 2026-10-31 真机踩到：18:08 开机后连续
        用了 4 小时没离开，面板一直显示"陪了你 3 分钟"。

        所以展示侧要拷一份、把当前段试算进去。``self._daily`` 与 Store 都不动，
        真正持久化仍由 ``_absorb()`` 在段结算时完成。
        """
        view = DayStat.from_dict(stat.to_dict())
        cur = self._current
        if cur.samples and self._day_key(cur.start_t) == day_key:
            apply_segment(view, cur, self._judge_cfg)
        return view

    def _snapshot(self) -> dict:
        """面板快照：dashboard context 与 presence_snapshot 入口共用。

        口径纪律（见 ``core/state.py`` 顶部「三本账」与 DESIGN §7.1）：

        - 「陪伴多久」用 duration 账
        - 「离开多久」用 ``peak_w`` 账（含无采样缺口）
        - **两者不可相加** —— 锁屏那段既进 ``locked_sec`` 又进 ``waiting``
        - 覆盖率 = ``covered_sec / span_sec``，< 1 表示有盲区，
          面板必须标「没有记录」，不能画成「离开」

        只读内存，不碰 Store —— 面板 5 秒一次的轮询不该打到磁盘上。
        """
        today_key = self._day_key(time.time())
        stat = self._daily.get(today_key) or DayStat(date=today_key)
        cur = self._current
        view = self._day_view(stat, today_key)

        today = view.to_dict()
        today["span_sec"] = round(view.span_sec, 1)
        today["coverage"] = round(view.covered_ratio, 3)
        today["waiting_total_sec"] = round(view.waiting_total_sec, 1)

        # 时间轴下发「当天第几秒」，前端只做 x/86400 的百分比换算
        day_start = self._day_start(time.time())

        def _offset(start_ts: float) -> int:
            return max(0, min(86400, int(round(float(start_ts) - day_start))))

        timeline = []
        for item in self._segments.get(today_key) or []:
            seg = dict(item)
            start_off = _offset(seg.get("t") or 0)
            end_off = _offset(seg.get("e") or seg.get("t") or 0)
            seg["o"] = start_off
            seg["len"] = max(0, end_off - start_off)
            timeline.append(seg)

        # 当前段还没结算，不在 _segments 里；补进来，否则面板看不到「此刻」
        if cur.samples and self._day_key(cur.start_t) == today_key:
            start_off = _offset(cur.start_t)
            end_off = _offset(cur.last_t or cur.start_t)
            timeline.append(
                {
                    "s": cur.state,
                    "t": round(cur.start_t),
                    "o": start_off,
                    "len": max(0, end_off - start_off),
                    "d": round(cur.duration),
                    "p": round(cur.peak_w),
                    "live": True,
                }
            )

        # 「最近几天」必须与顶部同一口径：顶部走 _day_view()（含未结算段），
        # 列表若直接读 self._daily，今天那一行就只算到**上次结算**为止 ——
        # 2026-11-01 真机踩到：顶部 12 分钟、列表 10 分钟，差的正是 roll 那一刀之后的部分。
        # 另：daily_v1 在首次 roll / 状态切换前**根本没有今天这个 key**，
        # 列表会整行缺失（同一天上午的另一个现象），所以缺席时补一行。
        day_keys = sorted(self._daily)
        if (
            today_key not in day_keys
            and cur.samples
            and self._day_key(cur.start_t) == today_key
        ):
            day_keys.append(today_key)
            day_keys.sort()

        recent = []
        for day_key in day_keys[-7:]:
            # 今天用 view（含当前未结算段），历史日用已结算的日聚合
            shown = view if day_key == today_key else self._daily[day_key]
            recent.append(
                {
                    "date": day_key,
                    "companion_sec": round(shown.companion_sec, 1),
                    "waiting_sec": round(shown.waiting_total_sec, 1),
                    "coverage": round(shown.covered_ratio, 2),
                }
            )

        # 年历热力图：自然年（1-01 至今）的全部日聚合，口径与 recent 完全一致 ——
        # 今天用 view 试算，历史日用已结算的日聚合。只下发 date / companion_sec 两个字段，
        # 且**只发有记录的日子**（daily_v1 没有 key 的那天就是「还没开始记录」，前端压淡）。
        # 短键 d/s：满打满算 365 天 ≈ 9KB，本地 IPC 每 5 秒一次没有压力。
        # 不下发 coverage —— 月历格子里显示不下覆盖率，前端也用不到。
        _yyyy = today_key[:4]
        calendar = []
        for day_key in day_keys:
            if not day_key.startswith(_yyyy):
                continue
            shown = view if day_key == today_key else self._daily[day_key]
            calendar.append(
                {
                    "d": day_key,
                    "s": round(shown.companion_sec),
                }
            )

        # 年历左翻的下界：初识日（启动时探测，见 _resolve_since），而不是本插件
        # 第一条记录。早于记录期的那几个月显示成空月历 —— 那是「她还没开始数」，
        # 不是「没陪过」。用户显式设了 since_date 就以他的为准，不再拿记录去压低它。
        override = _date_of(self._settings.get("since_date"))
        if override:
            since = override
        else:
            since = self._since or today_key
            if day_keys and day_keys[0] < since:
                since = day_keys[0]

        return {
            "version": _VERSION,
            "paused": self._paused(),
            "since": since,
            "today": today,
            "timeline": timeline,
            "current": {
                "state": cur.state,
                "duration": round(cur.duration, 1),
                "peak_w": round(cur.peak_w, 1),
                "samples": cur.samples,
            },
            "recent": recent,
            "calendar": calendar,
            "config": {
                "interactive_sec": self._judge_cfg.interactive_sec,
                "silent_leave_sec": self._judge_cfg.silent_leave_sec,
                "fullscreen_tail_sec": self._judge_cfg.fullscreen_tail_sec,
            },
            "marks": dict(self._marks),
        }

    #: 面板定时轮询入口。`@ui.action` 是 `api.call` 可达的前提（陪伴 get_journal 同款）。
    #: **不设 `refresh_context=True`** —— 那会让宿主每次调用都重刷 dashboard context，
    #: 变成「轮询 → 刷新 context → 又轮询」的自我循环。
    @ui.action(label=tr("actions.presence_snapshot.label", default="刷新"))
    @plugin_entry(
        id="presence_snapshot",
        name="面板快照",
        description="面板定时轮询用：返回与 dashboard 相同的数据，不依赖宿主刷新面板。",
        metadata=_AGENT_HIDDEN,
        input_schema={"type": "object", "properties": {}},
    )
    async def presence_snapshot(self, **_: Any):
        return Ok(self._snapshot())

    # ==========================================================
    # 入口（全部 agent_hidden，仅供面板/命令面板调用）
    # ==========================================================

    @plugin_entry(
        id="presence_status",
        name="今日摘要",
        description="查看今天她陪了你多久、你安静了多久、几点第一次见到你，以及数据覆盖率。",
        metadata=_AGENT_HIDDEN,
        input_schema={
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD，留空为今天"}},
        },
    )
    async def presence_status(self, **kwargs: Any):
        day = str(kwargs.get("date") or "").strip() or self._day_key(time.time())
        stat = self._daily.get(day)
        payload: dict = {
            "date": day,
            "paused": self._paused(),
            "current_state": self._current.state,
            "current_duration": round(self._current.duration, 1),
            "current_peak_w": round(self._current.peak_w, 1),
            "marks": dict(self._marks),
            "days_recorded": len(self._daily),
        }
        if stat is None:
            payload["day"] = None
            payload["note"] = "no_record_for_this_day"
            return Ok(payload)
        # 今天要算上「当前还没结算的段」，否则显示的是上次结算时的旧值
        view = self._day_view(stat, day)
        payload["day"] = view.to_dict()
        # 三个派生量单独给，避免面板自己算错口径：
        #   covered_ratio     采样覆盖率（分母是真实跨度，不是采样时长）
        #   span_sec          当天首尾跨度
        #   waiting_total_sec 含未结算事件的离开累计（to_dict 里只有已结算部分）
        payload["covered_ratio"] = round(view.covered_ratio, 3)
        payload["span_sec"] = round(view.span_sec, 1)
        payload["waiting_total_sec"] = round(view.waiting_total_sec, 1)
        return Ok(payload)

    # ==========================================================
    # 给角色（LLM）的唯一一个查询工具
    # ==========================================================

    #: 这是本插件**唯一**对模型可见的入口（DESIGN §8.1：至多 1 个查询工具）。
    #: 其他入口全部 `agent_hidden`，避免污染上下文。
    #:
    #: 为什么必须有它：2026-11-01 真机，用户问 YUI「陪了我多久」，YUI 手里
    #: 没有任何本插件的数据，只能靠别的插件状态去猜，报出「2 小时 41 分」，
    #: 而面板同时显示「1 小时 45 分」——2h41 恰好是当天的**有采样时长**
    #: （covered_sec），不是陪伴时长。工具返回 `summary` 已经按面板口径写好，
    #: 模型照读即可，不再有二次换算的空间。
    @llm_tool(
        name="presence_today",
        description=(
            "查询「她的一天」记录的真实陪伴与安静数据。"
            "当用户问『今天陪了我多久』『我今天安静过几次』『你什么时候见到我』"
            "『我昨天几点走的』时调用。"
            "返回的 summary 已按面板口径写好，直接用它回答，不要自行换算或猜测。"
            "口径：companion_sec 才是『陪着你』的时长；covered_sec 只是有采样的时长；"
            "waiting 是另一本账，三者不能相加。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "YYYY-MM-DD；留空表示今天。",
                }
            },
        },
        timeout=5.0,
    )
    async def presence_today(self, *, date: Any = "", **_):
        today_key = self._day_key(time.time())
        raw_date = str(date or "").strip().lower()
        # 模型偶尔会传「今天 / 昨天」而不是日期；两种写法都接住，
        # 免得它因为格式不对而自己编一个数字
        if raw_date in ("", "今天", "today"):
            day = today_key
        elif raw_date in ("昨天", "yesterday"):
            day = self._day_key(time.time() - 86400)
        else:
            day = raw_date
        is_today = day == today_key

        stat = self._daily.get(day)
        if stat is None and is_today:
            # 今天可能还一条段都没结算过：用空壳 + 当前段试算，
            # 与面板的 _snapshot 走同一条路
            stat = DayStat(date=day)
        if stat is None:
            return {
                "summary": f"{day} 那天没有记录（插件没运行、关机或睡眠），不能推断你当时在不在。",
                "date": day,
                "recorded": False,
            }

        view = self._day_view(stat, day)
        companion = float(view.companion_sec)
        count = int(view.waiting_count)
        longest = float(view.longest_wait_sec)
        span = float(view.span_sec)
        coverage = float(view.covered_ratio)

        lead = "今天到现在" if is_today else f"{day} 那天"

        first = self._clock(view.first_seen)
        last = self._clock(view.last_seen)

        # 「记录从几点开始」≠「她几点才来」。2026-11-01 真机：插件 10:57 才启动，
        # companion_sec 只有 1h45m，YUI 照读成「今天只陪了你 1 小时 45 分」——
        # 用户以为她上午不在，实际是那之前根本没在记。
        caveat = ""
        if first and onset_is_late(view.first_seen, self._day_start(float(view.first_seen))):
            caveat = (
                f"{'今天' if is_today else '那天'}的记录是从 {first} 开始的，"
                "在那之前没有记录 —— 那是空白，不是她不在。"
            )

        if span <= 0 and companion <= 0 and count <= 0:
            spoken = f"{lead}还没有记录。"
        else:
            head = f"{lead}，她陪了你 {_fmt_dur(companion)}"
            if caveat:
                head += f"（从 {first} 开始记的）"
            parts = [head]
            if count > 0:
                parts.append(f"你安静过 {count} 次，最长 {_fmt_dur(longest)}")
            else:
                parts.append("你还没有安静过")
            spoken = "；".join(parts) + "。"

        if first:
            who = "今天" if is_today else "那天"
            if caveat and last and last != first:
                # 起点「从 10:57 开始记」上面已经说过，这里只补最后一次，
                # 否则同一个时刻会在两句话里各出现一遍
                spoken += f"她{who}最后一次见到你是 {last}。"
            else:
                spoken += f"她{who} {first} 见到你"
                if last and last != first:
                    spoken += f"，最后一次见到你是 {last}"
                spoken += "。"

        # 与面板 coverageNote() 同一条分界线：覆盖率百分比四舍五入后 < 95 才提示
        pct = int(coverage * 100 + 0.5)
        if span > 0 and pct < 95:
            blind = span * (1.0 - min(1.0, max(0.0, coverage)))
            spoken += f"（有 {_fmt_dur(blind)} 没有记录，那段时间不计入）"

        return {
            "summary": spoken,
            "date": day,
            "recorded": True,
            "companion_sec": round(companion, 1),
            "companion": _fmt_dur(companion),
            "waiting_count": count,
            "longest_wait_sec": round(longest, 1),
            "longest_wait": _fmt_dur(longest),
            "span_sec": round(span, 1),
            "span": _fmt_dur(span),
            "coverage": round(coverage, 3),
            "first_seen_clock": first,
            "last_seen_clock": last,
            "window": f"{first} – {last}" if first and last and last != first else (first or ""),
            "caveat": caveat,
            "paused": self._paused(),
            "guidance": (
                "回答『陪了我多久』只用 companion_sec；covered_sec 是采样覆盖时长，"
                "waiting 是另一本账，三者不能相加。"
                "caveat 非空时必须把『记录从几点开始』一并说出，"
                "禁止把 companion_sec 说成『今天只陪了这么久』——"
                "窗口之前是没在记录，不是她不在。"
            ),
        }

    # `@ui.action` 是面板 `api.call` 可达的前提（同 presence_snapshot 先例）。
    # `refresh_context=True`：暂停是用户主动操作，执行后宿主立刻重刷 dashboard，
    # 面板的 props.state 和右上角「已暂停」标记马上跟上，不用等下一轮 5 秒轮询。
    @ui.action(
        label=tr("actions.presence_pause.label", default="暂停/恢复记录"),
        refresh_context=True,
    )
    @plugin_entry(
        id="presence_pause",
        name="暂停/恢复记录",
        description="暂停后一条采样都不做（连键鼠空闲也不读）。这是本插件自己的隐私开关。",
        metadata=_AGENT_HIDDEN,
        input_schema={
            "type": "object",
            "properties": {"paused": {"type": "boolean"}},
            "required": ["paused"],
        },
    )
    async def presence_pause(self, **kwargs: Any):
        value = kwargs.get("paused")
        if not isinstance(value, bool):
            return Err(SdkError("presence_pause_requires_boolean"))
        self._settings["paused"] = value
        await self._save_settings()
        self.logger.info("presence recording {}", "paused" if value else "resumed")
        return Ok({"paused": value})

    @plugin_entry(
        id="probe_status",
        name="探针状态",
        description="查看采样是否在跑、已采多少条、缓冲与已落盘块数量。",
        metadata=_AGENT_HIDDEN,
        input_schema={"type": "object", "properties": {}},
    )
    async def probe_status(self, **_: Any):
        return Ok(
            {
                "enabled": self._probe_enabled(),
                "paused": self._paused(),
                "interval_sec": self._interval_sec(),
                "flush_every": self._flush_every(),
                "max_chunks": self._max_chunks(),
                "buffered": len(self._buf),
                "chunks": len(self._index),
                "samples_this_run": self._sample_count,
                "last_sample_ts": self._last_sample_ts,
                "current_state": self._current.state,
            }
        )

    @plugin_entry(
        id="probe_flush",
        name="立即写入探针数据",
        description="把内存缓冲立刻落盘，不用等攒满一块。",
        metadata=_AGENT_HIDDEN,
        input_schema={"type": "object", "properties": {}},
    )
    async def probe_flush(self, **_: Any):
        await self._flush()
        await self._save_current()
        return Ok({"buffered": len(self._buf), "chunks": len(self._index)})

    @plugin_entry(
        id="probe_export",
        name="导出探针数据",
        description="导出最近若干块的原始采样，用于对照判定结果。",
        metadata=_AGENT_HIDDEN,
        input_schema={
            "type": "object",
            "properties": {
                "max_chunks": {"type": "integer", "minimum": 1, "maximum": 200},
            },
        },
    )
    async def probe_export(self, **kwargs: Any):
        limit = max(1, min(200, _as_int(kwargs.get("max_chunks"), 12)))
        keys = self._index[-limit:] if limit <= len(self._index) else list(self._index)

        items: list[dict] = []
        missing: list[str] = []
        for key in keys:
            res = await self.store.get(key)
            if isinstance(res, Ok) and isinstance(res.value, dict):
                rows = res.value.get("items")
                if isinstance(rows, list):
                    items.extend(row for row in rows if isinstance(row, dict))
            else:
                missing.append(key)

        return Ok(
            {
                "chunks": len(keys),
                "total_chunks": len(self._index),
                "samples": len(items),
                "missing": missing,
                "buffered": len(self._buf),
                "items": items,
            }
        )

    @plugin_entry(
        id="probe_clear",
        name="清空全部记录",
        description="删除探针原始采样、当前段与日聚合（不可恢复）。必须显式传 confirm=true。",
        metadata=_AGENT_HIDDEN,
        input_schema={
            "type": "object",
            "properties": {"confirm": {"type": "boolean"}},
            "required": ["confirm"],
        },
    )
    async def probe_clear(self, **kwargs: Any):
        if kwargs.get("confirm") is not True:
            return Err(SdkError("probe_clear_confirmation_required"))
        removed = 0
        for key in list(self._index):
            try:
                await self.store.delete(key)
                removed += 1
            except Exception:  # noqa: BLE001 - 清理尽力而为
                pass
        self._index = []
        self._buf = []
        self._current = Segment()
        self._daily = {}
        self._marks = {}
        self._last_state = ""
        # 分段时间轴也要清 —— 否则面板上时间轴还在、上面的数字已归零
        for day in list(self._segments):
            try:
                await self.store.delete(f"{_SEG_PREFIX}{day}")
            except Exception:  # noqa: BLE001 - 清理尽力而为
                pass
        self._segments = {}
        self._seg_index = []
        await self._save_index()
        await self._save_current()
        await self._save_daily()
        await self._save_marks()
        await self.store.set(_STORE_SEG_INDEX, [])
        return Ok({"cleared": True, "chunks": removed})

    @plugin_entry(
        id="segments_backfill",
        name="补齐时间轴",
        description=(
            "用探针原始采样重放，补齐缺失日期的分段时间轴。"
            "只填完全没有分段的日期，已有的真实结算数据不覆盖。"
        ),
        metadata=_AGENT_HIDDEN,
        input_schema={"type": "object", "properties": {}},
    )
    async def segments_backfill(self, **_: Any):
        result = await self._backfill_segments()
        self.logger.info("segments backfill: {}", result)
        return Ok(result)
