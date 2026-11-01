"""把探针的历史 chunk 重放给 V0.2 状态机，验证判定与记账。

用途：不用等新数据，就能回答"如果 V0.2 今天一直在跑，它会怎么记这一天"。
重放是只读的，不碰 Store。

    python tools/replay.py <store.db 路径>
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from core.judge import Config, Sample  # noqa: E402
from core.state import DayStat, Segment, apply_segment, step  # noqa: E402
from tools.analyze_probe import decode, load_rows  # noqa: E402


def hhmm(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "?"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    db = Path(argv[1])
    cfg = Config()

    # 阈值可覆盖，用于对比「改阈值前后差多少」（Config 是 frozen，只能重建）
    for i, arg in enumerate(argv):
        if arg == "--interactive" and i + 1 < len(argv):
            cfg = Config(
                interactive_sec=float(argv[i + 1]),
                silent_leave_sec=cfg.silent_leave_sec,
                fullscreen_tail_sec=cfg.fullscreen_tail_sec,
            )
        if arg == "--leave" and i + 1 < len(argv):
            cfg = Config(
                interactive_sec=cfg.interactive_sec,
                silent_leave_sec=float(argv[i + 1]),
                fullscreen_tail_sec=cfg.fullscreen_tail_sec,
            )

    rows: list[dict] = []
    for key, blob in load_rows(db):
        if not key.startswith("probe:"):
            continue
        try:
            value, _ = decode(blob)
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        for row in value.get("items") or []:
            if isinstance(row, dict) and isinstance(row.get("t"), (int, float)):
                rows.append(row)

    rows.sort(key=lambda r: float(r["t"]))
    print(f"重放 {len(rows)} 条采样（{hhmm(rows[0]['t'])} → {hhmm(rows[-1]['t'])}）")
    print(f"判定阈值：T={cfg.silent_leave_sec:.0f}s  互动={cfg.interactive_sec:.0f}s  "
          f"全屏长尾={cfg.fullscreen_tail_sec / 3600:.1f}h\n")

    cur = Segment()
    segments: list[Segment] = []
    daily: dict[str, DayStat] = {}
    state_count: dict[str, int] = {}
    stale = 0

    for row in rows:
        sample = Sample(
            t=float(row["t"]),
            m=float(row.get("m") or row["t"]),
            w=row.get("w"),
            q=row.get("q"),
            f=str(row.get("f") or ""),
            # V0.3.1 起探针多记了语义屏幕态；旧 chunk 没有这个字段，
            # 留空让 judge 回退到 q 码，两种数据都能重放
            screen=str(row.get("sc") or ""),
        )
        result = step(cur, sample, cfg)
        cur = result.current
        state_count[result.state] = state_count.get(result.state, 0) + 1
        if result.stale:
            stale += 1
        if result.closed is not None:
            segments.append(result.closed)
            key = datetime.fromtimestamp(result.closed.start_t).strftime("%Y-%m-%d")
            stat = daily.get(key) or DayStat(date=key)
            apply_segment(stat, result.closed, cfg)
            daily[key] = stat

    if cur.samples:
        segments.append(cur)
        key = datetime.fromtimestamp(cur.start_t).strftime("%Y-%m-%d")
        stat = daily.get(key) or DayStat(date=key)
        apply_segment(stat, cur, cfg)
        daily[key] = stat

    print("=== 状态分布（采样条数）")
    for state, count in sorted(state_count.items(), key=lambda kv: -kv[1]):
        print(f"  {state:<16} {count}")
    if stale:
        print(f"  （其中 {stale} 条因采样间隔过大未计入时长，跨重启边界会产生）")

    print("\n=== 每日聚合")
    for key in sorted(daily):
        stat = daily[key]
        d = stat.to_dict()
        print(
            f"  {key}  陪伴 {d['companion_sec'] / 3600:.2f}h | 离开 {stat.waiting_total_sec / 60:.0f}min"
            f"（{d['waiting_count']} 次，最长 {d['longest_wait_sec'] / 60:.0f}min）"
            f" | 短暂离席 {d['short_break_sec'] / 60:.0f}min"
            f" | 静默 {d['away_sec'] / 60:.1f}min"
            f" | 锁屏 {d['locked_sec'] / 60:.0f}min"
            f" | 全屏 {d['fullscreen_sec'] / 60:.0f}min"
            f" | 覆盖率 {stat.covered_ratio:.2f}"
        )
        if d["first_seen"] and d["last_seen"]:
            print(
                f"           第一次见到你 {hhmm(d['first_seen'])}"
                f"，最后一次 {hhmm(d['last_seen'])}"
            )

    print("\n=== 段列表（>2 分钟的段）")
    for seg in segments:
        if seg.duration < 120 and seg.state in ("interactive",):
            continue
        print(
            f"  {hhmm(seg.start_t)} → +{seg.duration / 60:5.1f}min  "
            f"{seg.state:<16} peak_w={seg.peak_w:7.0f}s  n={seg.samples}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
