"""日检：读 daily_v1 / current_v1 / marks_v1，输出四个绿灯指标。

    python tools/check_day.py <store.db 路径>

四个指标：①天条目数 ②覆盖率 ③状态分布 ④当前段是否健康
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

from tools.analyze_probe import decode, load_rows  # noqa: E402

_WANT = {"daily_v1", "current_v1", "marks_v1", "settings", "probe_index"}


def hhmm(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "?"


def span_of(d: dict) -> float:
    """当天真实时间跨度（第一条采样 → 最后一条采样），覆盖率的**分母**。

    与 `covered_sec`（分子，有采样的时长之和）相除才有意义。
    早期脚本用 `attributed / covered` 当覆盖率，分子分母都是 duration 之和，
    恒等于 1.00，等于没做检查（2026-10-27 发现）。
    """
    first, last = d.get("first_seen"), d.get("last_seen")
    if not isinstance(first, (int, float)) or not isinstance(last, (int, float)):
        return 0.0
    return max(0.0, float(last) - float(first))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    db = Path(argv[1])

    data: dict = {}
    for key, blob in load_rows(db):
        if key in _WANT:
            try:
                value, _ = decode(blob)
            except ValueError:
                continue
            data[key] = value

    daily = data.get("daily_v1") or {}
    print(f"===== daily_v1：{len(daily)} 天")
    for day in sorted(daily):
        d = daily[day]
        comp = float(d.get("companion_sec") or 0)
        # waiting_sec 只含"已结算"的离开事件，当天最后一段离开还在进行中时
        # 其峰值存在 open_wait_sec 里，必须一起加，否则会凭空消失
        wait = float(d.get("waiting_sec") or 0) + float(d.get("open_wait_sec") or 0)
        short = float(d.get("short_break_sec") or 0)
        lock = float(d.get("locked_sec") or 0)
        away = float(d.get("away_sec") or 0)
        full = float(d.get("fullscreen_sec") or 0)
        cov = float(d.get("covered_sec") or 0)
        span = span_of(d)
        # 账本B（duration 口径）：各状态互斥，和应 ≈ covered_sec
        attributed = comp + away + short + lock
        # 账本A：覆盖率分母必须是真实跨度，不是 cov 自己
        ratio = cov / span if span else 0.0
        print(
            f"  {day}  陪伴 {comp / 3600:.2f}h | 离开 {wait / 60:.0f}min"
            f"（{d.get('waiting_count') or 0} 次，最长 {float(d.get('longest_wait_sec') or 0) / 60:.0f}min）[峰值口径]"
            f" | 短暂离席 {short / 60:.0f}min | 静默 {away / 60:.1f}min | 锁屏 {lock / 60:.0f}min"
            f" | 全屏 {full / 60:.0f}min"
        )
        print(
            f"           跨度 {span / 3600:.2f}h  有采样 {cov / 3600:.2f}h  覆盖率 {ratio:.2f}"
            f"  第一次 {hhmm(d.get('first_seen'))}  最后一次 {hhmm(d.get('last_seen'))}"
        )
        if cov > 0 and abs(attributed - cov) > 2:
            # away_sec 是 2026-10-26 才加入的字段，此前的旧条目没有它，
            # 归因天然差一截 —— 标提示而不是错误，别让人以为数据坏了
            tag = "[--]" if not d.get("away_sec") else "[!!]"
            note = "旧条目无 away_sec 字段，属正常" if tag == "[--]" else "有状态分支没记账"
            print(
                f"           {tag} 归因账本 {attributed / 3600:.2f}h 与有采样 {cov / 3600:.2f}h 不符"
                f"（差 {(attributed - cov) / 60:+.0f}min，{note}）"
            )

    cur = data.get("current_v1") or {}
    print("\n===== current_v1（当前段）")
    print(
        f"  state={cur.get('state')}  duration={float(cur.get('duration') or 0):.0f}s"
        f"  peak_w={float(cur.get('peak_w') or 0):.0f}s  samples={cur.get('samples')}"
        f"  last_t={hhmm(cur.get('last_t'))}"
    )

    marks = data.get("marks_v1") or {}
    print(f"\n===== marks_v1\n  {marks if marks else '(空)'}")
    settings = data.get("settings") or {}
    print(f"===== settings\n  {settings if settings else '(空)'}")

    index = data.get("probe_index") or []
    print(f"===== probe_index：{len(index)} 块" if isinstance(index, list) else "")

    print("\n===== 绿灯检查")
    ok = True
    if len(daily) >= 1:
        print(f"  [OK] daily_v1 有 {len(daily)} 天数据")
    else:
        print("  [!!] daily_v1 是空的")
        ok = False
    for day in sorted(daily):
        d = daily[day]
        cov = float(d.get("covered_sec") or 0)
        span = span_of(d)
        # 真错误：归因账本与采样时长不符 → 有状态分支没记账
        attributed = (
            float(d.get("companion_sec") or 0)
            + float(d.get("away_sec") or 0)
            + float(d.get("short_break_sec") or 0)
            + float(d.get("locked_sec") or 0)
        )
        if cov > 0 and abs(attributed - cov) > 2:
            tag = "[--]" if not d.get("away_sec") else "[!!]"
            print(
                f"  {tag} {day} 归因账本 {attributed / 3600:.2f}h ≠ 有采样 {cov / 3600:.2f}h"
                f"（差 {(attributed - cov) / 60:+.0f}min）"
            )
            if tag == "[!!]":
                ok = False
        if cov <= 0:
            print(f"  [!!] {day} 没有任何采样")
            ok = False
            continue
        # 覆盖率是**信息项**不是判错项：用户关机睡觉本来就该留下盲区，
        # 那正是「无记录 ≠ 不在」要表达的。只在缺口过半时提醒一句。
        ratio = cov / span if span else 0.0
        if span and ratio < 0.5:
            print(
                f"  [--] {day} 覆盖率 {ratio:.2f}（跨度 {span / 3600:.2f}h，"
                f"有采样 {cov / 3600:.2f}h）—— 有大段无记录，属正常但面板要标出来"
            )
    if (cur.get("samples") or 0) > 0:
        print("  [OK] current_v1 有活跃段")
    else:
        print("  [--] current_v1 为空（可能刚启动）")
    print("\n结论：" + ("全部正常" if ok else "有异常，需要查日志"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
