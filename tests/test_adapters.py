"""平台适配层测试：语义归一、隐私门控、降级路径。

不依赖宿主：

    python tests/test_adapters.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from core.adapters import (  # noqa: E402
    SCREEN_FULLSCREEN,
    SCREEN_LOCKED,
    SCREEN_NORMAL,
    SCREEN_UNKNOWN,
    SOURCE_HOST,
    SOURCE_NONE,
    HostSdkAdapter,
    NullAdapter,
    Win32Adapter,
    build_adapter,
    screen_from_q,
)

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, note: str) -> None:
    RESULTS.append((ok, note))
    print(f"[{'OK  ' if ok else 'FAIL'}] {note}")


class FakeSnap:
    def __init__(self, privacy="visible", idle=42.0, category="work"):
        self.privacy_state = privacy
        self.system_idle_seconds = idle
        self.foreground_category = category


def main() -> int:
    print("--- q 码 → 语义屏幕态")
    for q, expect in (
        (1, SCREEN_LOCKED),
        (2, SCREEN_FULLSCREEN),
        (3, SCREEN_FULLSCREEN),
        (6, SCREEN_FULLSCREEN),
        (4, SCREEN_NORMAL),
        (5, SCREEN_NORMAL),
        (None, SCREEN_UNKNOWN),
    ):
        got = screen_from_q(q)
        check(got == expect, f"q={q} -> {got}（期望 {expect}）")

    print("\n--- 宿主快照适配器：visible")
    host = HostSdkAdapter(lambda: _done(FakeSnap()))
    sig = asyncio.run(host.sample())
    check(sig.source == SOURCE_HOST, f"source={sig.source}")
    check(sig.idle_sec == 42.0, f"idle 透传（实得 {sig.idle_sec}）")
    check(sig.screen == SCREEN_UNKNOWN, "宿主不给锁屏/全屏细分 → screen=unknown")
    check(sig.usable, "有 idle 就能判定，usable=True")

    print("\n--- 宿主快照适配器：privacy 非 visible")
    host = HostSdkAdapter(lambda: _done(FakeSnap(privacy="private", idle=42.0)))
    sig = asyncio.run(host.sample())
    check(sig.idle_sec is None, "隐私态下不读空闲时长")
    check(sig.category == "", "隐私态下不读前台分类")
    check(not sig.usable, "隐私态 → 不可用，业务层不记录")

    print("\n--- Win32 适配器：宿主明确 private")
    native = Win32Adapter(lambda: _done(FakeSnap(privacy="private", idle=42.0)))
    sig = asyncio.run(native.sample())
    check(sig.idle_sec is None, "private 时连原生 idle 也不上报")
    check(sig.screen == SCREEN_UNKNOWN, "private 时不上报屏幕态")
    check(not sig.usable, "private → 不可用")

    print("\n--- 宿主快照适配器：调用失败")
    async def boom():
        raise RuntimeError("host down")

    host = HostSdkAdapter(boom)
    sig = asyncio.run(host.sample())
    check(sig.source == SOURCE_NONE, f"失败降级为 source=none（实得 {sig.source}）")
    check(not sig.usable, "失败 → 不可用")

    print("\n--- 空适配器")
    sig = asyncio.run(NullAdapter().sample())
    check(sig.source == SOURCE_NONE, f"source={sig.source}")
    check(not sig.usable, "空适配器永远不可用")

    print("\n--- 平台选择")
    adapter = build_adapter(None)
    if sys.platform == "win32":
        check(adapter.source == "win32", f"Windows 选原生适配器（实得 {adapter.source}）")
        sig = asyncio.run(adapter.sample())
        if sig.usable:
            check(sig.idle_sec is not None, f"真机 idle 可读（{sig.idle_sec}s）")
            check(sig.screen in (SCREEN_NORMAL, SCREEN_LOCKED, SCREEN_FULLSCREEN),
                  f"真机屏幕态已归一（{sig.screen}）")
        else:
            print("[SKIP] 本机空闲信号不可用，跳过原生读数断言")
    else:
        check(adapter.source == SOURCE_NONE, "无宿主快照的非 Windows 平台 → 空适配器")

    failed = sum(1 for ok, _ in RESULTS if not ok)
    print(f"\n共 {len(RESULTS)} 项，失败 {failed} 项")
    return 0 if failed == 0 else 1


async def _done(value):
    return value


if __name__ == "__main__":
    raise SystemExit(main())
