"""状态机分支测试。

不依赖宿主、不依赖 pytest 也能跑：

    python tests/test_judge.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows 上重定向到文件时默认 cp936，中文会乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from core.judge import (  # noqa: E402
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
    counts_as_waiting,
)

CFG = Config()


def s(w, q=5, f="") -> Sample:
    return Sample(t=0.0, m=0.0, w=w, q=q, f=f)


def ss(w, screen, q=None) -> Sample:
    """语义屏幕态采样（适配层输出的形态，不带 q 也能判）。"""
    return Sample(t=0.0, m=0.0, w=w, q=q, screen=screen)


CASES = [
    # (说明, sample, 期望状态)
    ("刚动过键鼠 → 在操作", s(10), INTERACTIVE),
    ("离席 5 分钟（未到 T=900）→ 短暂离席", s(300), SHORT_BREAK),
    ("静默 1000 秒（超 T=900）→ 静默离开", s(1000), AWAY),
    ("刚好等于 T=900 → 仍算短暂离席（闭区间）", s(900), SHORT_BREAK),
    ("刚好超过 T 一秒 → 静默离开", s(901), AWAY),
    ("全屏看片 10 分钟 → 全屏活动（算陪伴）", s(600, q=2), FULLSCREEN),
    ("全屏看片 4 小时零输入 → 全屏长尾离开", s(4 * 3600, q=2), AWAY_FULLSCREEN),
    ("全屏刚好 3 小时 → 仍是全屏活动", s(3 * 3600, q=2), FULLSCREEN),
    ("全屏 D3D 状态同样识别", s(600, q=3), FULLSCREEN),
    ("刚进全屏的 QUIET_TIME 也归全屏", s(600, q=6), FULLSCREEN),
    ("锁屏且已静默 → 锁屏离开", s(600, q=1), LOCKED),
    ("解锁瞬间 q 滞后（w 已归零）→ 在操作", s(5, q=1), INTERACTIVE),
    # ---- 适配层语义屏幕态（q 缺失也能判）----
    ("语义：锁屏 + 无 idle → 仍是确认离开", ss(None, "locked"), LOCKED),
    ("语义：全屏 + 无 idle → 只能算全屏，不推断离开", ss(None, "fullscreen"), FULLSCREEN),
    ("语义：全屏 + idle 超长尾 → 画面挂着", ss(4 * 3600, "fullscreen"), AWAY_FULLSCREEN),
    ("语义：普通 + idle 300 → 短暂离席", ss(300, "normal"), SHORT_BREAK),
    # ---- 不可观测：不编造 ----
    ("w 不可用且没有强语义 → UNKNOWN（没有记录）", s(None, q=5), UNKNOWN),
    ("w 不可用 + 无 q → UNKNOWN", s(None, q=None), UNKNOWN),
    ("语义 unknown + 无 idle → UNKNOWN", ss(None, "unknown"), UNKNOWN),
]


def main() -> int:
    failed = 0
    for note, sample, expect in CASES:
        got = classify(sample, CFG)
        flag = "OK  " if got == expect else "FAIL"
        if got != expect:
            failed += 1
        print(f"[{flag}] {note:<44} w={sample.w} q={sample.q} -> {got} (期望 {expect})")

    print("\n--- 语义谓词 ---")
    checks = [
        ("interactive 算陪伴", counts_as_companion(INTERACTIVE), True),
        ("fullscreen 算陪伴", counts_as_companion(FULLSCREEN), True),
        ("locked 不算陪伴", counts_as_companion(LOCKED), False),
        ("locked 算等待", counts_as_waiting(LOCKED), True),
        ("away 算等待", counts_as_waiting(AWAY), True),
        ("fullscreen 不算等待", counts_as_waiting(FULLSCREEN), False),
        ("short_break 既不算陪伴也不算等待", counts_as_waiting(SHORT_BREAK), False),
        ("unknown 不算陪伴", counts_as_companion(UNKNOWN), False),
        ("unknown 不算等待", counts_as_waiting(UNKNOWN), False),
    ]
    for note, got, expect in checks:
        if got != expect:
            failed += 1
        print(f"[{'OK  ' if got == expect else 'FAIL'}] {note}: {got}")

    print(f"\n{'全部通过' if failed == 0 else f'失败 {failed} 项'}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
