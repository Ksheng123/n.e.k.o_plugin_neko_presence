"""Linux 现场体检：这台机器到底能给出哪些在场信号。

    python tools/check_linux_signals.py            # 采 3 次，每次间隔 1 秒
    python tools/check_linux_signals.py --n 8 --gap 5
    python tools/check_linux_signals.py --raw      # 附带 logind 原始属性

为什么需要它：Wayland 的空闲时长**取决于 compositor 是否往 logind 写
``IdleHint``**。niri / sway 这类轻量 compositor 可能根本不写 —— 那时插件会
如实退化成「没有记录」，但你在日志里只会看到 ``idle=? ``，很难判断是
「没配好」还是「这台机器就没有这条信号」。本脚本把这件事直接摊开：

    idle 一直在变     → 这条链路是活的，插件能正常记录
    idle 恒为 0 / ?   → 这个会话不自报空闲，插件会退到宿主快照或记「没有记录」

脚本只读两个标量（空闲秒数、锁定标志），不解锁任何输入相关的能力。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


def main(argv: list[str]) -> int:
    from core import platform_linux as pl

    want_raw = "--raw" in argv
    count = _arg(argv, "--n", 3)
    gap = _arg(argv, "--gap", 1.0)

    session = pl.detect_session()
    backend = pl.pick_backend(session)

    print("=== 环境 ===")
    for key in ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "DISPLAY", "XDG_CURRENT_DESKTOP", "XDG_SESSION_ID"):
        print(f"  {key:<20} = {_env(key)}")
    print(f"  判定会话类型         = {session}")
    print(f"  选中的后端           = {backend.name}")

    if want_raw:
        props, how = pl._logind_props()
        print(f"\n=== logind 原始属性（{how or '读取失败'}）===")
        if not props:
            print("  （空：loginctl / busctl 都不可用）")
        for key in ("IdleHint", "IdleSinceHintMonotonic", "LockedHint", "Type"):
            if key in props:
                print(f"  {key:<24} = {props[key]}")

    print(f"\n=== 连续 {count} 次采样（间隔 {gap}s）===")
    idles: list[float | None] = []
    for i in range(count):
        probed = backend.read()
        idles.append(probed.idle_sec)
        idle = "?" if probed.idle_sec is None else f"{probed.idle_sec:8.1f}s"
        locked = {True: "locked  ", False: "unlocked", None: "lock?   "}[probed.locked]
        detail = f"  <- {', '.join(probed.detail)}" if probed.detail else ""
        print(f"  [{i + 1}] idle={idle}  {locked}{detail}")
        if i + 1 < count:
            time.sleep(gap)

    print("\n=== 结论 ===")
    known = [i for i in idles if i is not None]
    if not known:
        print("  ✗ 这个会话拿不到空闲时长。")
        print("    插件会退到宿主快照（system_idle_seconds）；宿主也没有的话，")
        print("    这段时间在面板上显示为「没有记录」——这是诚实的降级，不是 bug。")
        print("    Wayland 下常见原因：compositor 不往 logind 写 IdleHint。")
        return 1

    moving = len(set(known)) > 1 or max(known) > 0
    if moving:
        print(f"  ✓ 拿到空闲时长（{min(known):.0f}s ~ {max(known):.0f}s），链路可用。")
    else:
        print("  ⚠ 空闲时长恒为 0：可能你一直在操作，也可能这条信号没被维护。")
        print("    离开键盘 2 分钟再跑一次；数值仍然不动，就说明信号是死的。")
    return 0


def _arg(argv: list[str], name: str, default):
    try:
        idx = argv.index(name)
        raw = argv[idx + 1]
    except (ValueError, IndexError):
        return default
    try:
        return type(default)(raw)
    except (TypeError, ValueError):
        return default


def _env(key: str) -> str:
    import os

    return os.environ.get(key) or "（未设置）"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))