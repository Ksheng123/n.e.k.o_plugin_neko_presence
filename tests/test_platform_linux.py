"""Linux 适配层测试：属性解析、降级判据、隐私门控、上层零改动。

全部是纯逻辑，**不需要 Linux 也不需要 X11 / logind**，Windows 上同样能跑
（后端用假对象注入，:class:`LinuxAdapter` 本来就为此留了口子）：

    python tests/test_platform_linux.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from core import platform_linux as pl  # noqa: E402
from core.adapters import (  # noqa: E402
    SCREEN_LOCKED,
    SCREEN_NORMAL,
    SCREEN_UNKNOWN,
    LinuxAdapter,
    build_adapter,
)
from core.judge import (  # noqa: E402
    AWAY,
    INTERACTIVE,
    LOCKED,
    SHORT_BREAK,
    Config,
    Sample,
    classify,
)

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, note: str) -> None:
    RESULTS.append((ok, note))
    print(f"[{'OK  ' if ok else 'FAIL'}] {note}")


async def _done(value):
    return value


class FakeSnap:
    def __init__(self, privacy="visible", idle=42.0, category="work"):
        self.privacy_state = privacy
        self.system_idle_seconds = idle
        self.foreground_category = category


class FakeBackend:
    """假后端：直接给结论，不碰 X11 / D-Bus。"""

    name = "fake"

    def __init__(self, idle=None, locked=None):
        self._idle = idle
        self._locked = locked
        self.reads = 0

    def read(self) -> pl.Probed:
        self.reads += 1
        return pl.Probed(
            idle_sec=self._idle,
            locked=self._locked,
            session="wayland",
            backend=self.name,
        )


def sample_of(adapter, host=None):
    return asyncio.run(adapter.sample())


def main() -> int:
    print("--- loginctl 输出解析")
    loginctl = (
        "IdleHint=yes\n"
        "IdleSinceHintMonotonic=123456789\n"
        "LockedHint=no\n"
        "Type=wayland\n"
    )
    props = pl.parse_props(loginctl)
    check(props.get("IdleHint") == "yes", f"IdleHint 解析（实得 {props.get('IdleHint')!r}）")
    check(props.get("Type") == "wayland", f"Type 解析（实得 {props.get('Type')!r}）")

    print("\n--- busctl GetAll 输出解析")
    busctl = (
        'a{sv} 12 "ActiveHint": <b true>; "IdleHint": <b false>; '
        '"IdleSinceHintMonotonic": <t 987654321000000>; '
        '"LockedHint": <b true>; "Type": <s "wayland">;'
    )
    props = pl.parse_props(busctl)
    check(props.get("IdleHint") == "false", f"变体布尔解析（实得 {props.get('IdleHint')!r}）")
    check(
        props.get("IdleSinceHintMonotonic") == "987654321000000",
        f"变体 uint64 解析（实得 {props.get('IdleSinceHintMonotonic')!r}）",
    )
    check(props.get("Type") == '"wayland"', f"变体字符串保留引号（实得 {props.get('Type')!r}）")
    check(pl.parse_props("") == {}, "空输入 → 空字典")
    check(pl.parse_props("garbage") == {}, "垃圾输入 → 空字典，不抛")

    print("\n--- idle 计算的三条判据")
    check(
        pl.idle_from_props({}) is None,
        "IdleHint 缺失 → None（会话没有 idle 追踪，不能报 0）",
    )
    check(
        pl.idle_from_props({"IdleHint": "no"}) == 0.0,
        "IdleHint=no → 0.0（logind 的正面回答）",
    )
    check(
        pl.idle_from_props({"IdleHint": "yes"}) is None,
        "IdleHint=yes 但无 IdleSinceHintMonotonic → None（知道空闲、不知道多久）",
    )
    check(
        pl.idle_from_props({"IdleHint": "yes", "IdleSinceHintMonotonic": "0"}) is None,
        "IdleSinceHintMonotonic=0 → None（未初始化）",
    )
    # 1000 秒前开始空闲：now=1_000_000s，since=999_000_000_000µs
    got = pl.idle_from_props(
        {"IdleHint": "true", "IdleSinceHintMonotonic": "999000000000"}, now=1_000_000.0
    )
    check(got == 1000.0, f"monotonic 微秒 → 秒（实得 {got}）")
    got = pl.idle_from_props(
        {"IdleHint": "yes", "IdleSinceHintMonotonic": "2000000000000"}, now=1_000_000.0
    )
    check(got is None, "since 比 now 还晚 → None（时钟基准对不上，不报负数）")

    print("\n--- 会话类型判定顺序")
    check(
        pl.detect_session({"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0"})
        == pl.SESSION_WAYLAND,
        "Wayland 会话也导出 DISPLAY，必须先判 WAYLAND_DISPLAY",
    )
    check(
        pl.detect_session({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"})
        == pl.SESSION_WAYLAND,
        "XDG_SESSION_TYPE=wayland",
    )
    check(
        pl.detect_session({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}) == pl.SESSION_X11,
        "XDG_SESSION_TYPE=x11",
    )
    check(pl.detect_session({"DISPLAY": ":0"}) == pl.SESSION_X11, "只有 DISPLAY → x11")
    check(pl.detect_session({}) == pl.SESSION_HEADLESS, "什么都没有 → headless")

    print("\n--- 后端选择与强制覆盖")
    old = os.environ.get("NEKO_LINUX_BACKEND")
    try:
        os.environ["NEKO_LINUX_BACKEND"] = "none"
        check(pl.pick_backend(pl.SESSION_WAYLAND).name == pl.BACKEND_NONE, "强制 none 生效")
        os.environ["NEKO_LINUX_BACKEND"] = "logind"
        check(pl.pick_backend(pl.SESSION_X11).name == pl.BACKEND_LOGIND, "强制 logind 能盖过会话类型")
        os.environ["NEKO_LINUX_BACKEND"] = "x11"
        check(pl.pick_backend(pl.SESSION_WAYLAND).name == pl.BACKEND_X11, "强制 x11 能盖过会话类型")
        del os.environ["NEKO_LINUX_BACKEND"]
        check(pl.pick_backend(pl.SESSION_X11).name == pl.BACKEND_X11, "auto: x11 → X11Backend")
        check(pl.pick_backend(pl.SESSION_WAYLAND).name == pl.BACKEND_LOGIND, "auto: wayland → LogindBackend")
        check(pl.pick_backend(pl.SESSION_HEADLESS).name == pl.BACKEND_NONE, "auto: headless → 无后端")
    finally:
        if old is None:
            os.environ.pop("NEKO_LINUX_BACKEND", None)
        else:
            os.environ["NEKO_LINUX_BACKEND"] = old

    print("\n--- LinuxAdapter：锁屏是确认级判据")
    adapter = LinuxAdapter(None, FakeBackend(idle=None, locked=True))
    sig = sample_of(adapter)
    check(sig.screen == SCREEN_LOCKED, f"locked=True → screen=locked（实得 {sig.screen}）")
    check(sig.usable, "没有 idle 但确定锁屏 → 依然可用（judge 能判 locked）")
    check(sig.source == "linux_fake", f"source 带后端后缀（实得 {sig.source}）")
    check(sig.foreground == "" and sig.q is None, "Linux 不产 q / foreground")

    print("\n--- LinuxAdapter：正常取值")
    adapter = LinuxAdapter(None, FakeBackend(idle=42.0, locked=False))
    sig = sample_of(adapter)
    check(sig.idle_sec == 42.0, f"idle 透传（实得 {sig.idle_sec}）")
    check(sig.screen == SCREEN_NORMAL, f"不锁 → normal（实得 {sig.screen}）")

    print("\n--- LinuxAdapter：双源取较大值")
    host = lambda: _done(FakeSnap(idle=600.0))  # noqa: E731
    adapter = LinuxAdapter(host, FakeBackend(idle=5.0, locked=False))
    sig = sample_of(adapter)
    check(sig.idle_sec == 600.0, f"logind 说 5s、宿主说 600s → 取 600（实得 {sig.idle_sec}）")
    check(sig.sdk_idle == 600.0, "宿主原值留在 sdk_idle 里供对照")

    print("\n--- LinuxAdapter：后端全空时的降级链")
    adapter = LinuxAdapter(None, FakeBackend())
    sig = sample_of(adapter)
    check(sig.screen == SCREEN_UNKNOWN, "无锁屏信息也无 idle → unknown")
    check(not sig.usable, "unknown → 不可用，业务层记「没有记录」")

    host = lambda: _done(FakeSnap(idle=1200.0))  # noqa: E731
    adapter = LinuxAdapter(host, FakeBackend())
    sig = sample_of(adapter)
    check(sig.idle_sec == 1200.0, f"退到宿主快照 idle（实得 {sig.idle_sec}）")
    check(sig.screen == SCREEN_UNKNOWN, "宿主不给锁屏细分 → screen 仍是 unknown")
    check(sig.usable, "有 idle 就能判定")

    print("\n--- LinuxAdapter：隐私门控（先问再读）")
    backend = FakeBackend(idle=1.0, locked=False)
    adapter = LinuxAdapter(lambda: _done(FakeSnap(privacy="private")), backend)
    sig = sample_of(adapter)
    check(backend.reads == 0, "privacy=private 时**根本没读**本地信号")
    check(sig.idle_sec is None and not sig.usable, "private → 不上报任何字段")

    print("\n--- 合并验收：上层（judge）零改动")
    cfg = Config()
    cases = (
        (pl.Probed(idle_sec=None, locked=True), SCREEN_LOCKED, LOCKED),
        (pl.Probed(idle_sec=10.0, locked=False), SCREEN_NORMAL, INTERACTIVE),
        (pl.Probed(idle_sec=300.0, locked=False), SCREEN_NORMAL, SHORT_BREAK),
        (pl.Probed(idle_sec=3600.0, locked=False), SCREEN_NORMAL, AWAY),
    )
    for probed, want_screen, want_state in cases:
        sig = sample_of(LinuxAdapter(None, FakeBackend(idle=probed.idle_sec, locked=probed.locked)))
        state = classify(Sample(t=0.0, m=0.0, w=sig.idle_sec, screen=sig.screen), cfg)
        check(
            sig.screen == want_screen and state == want_state,
            f"backend({probed.idle_sec}s, locked={probed.locked}) → {state}（期望 {want_state}）",
        )

    print("\n--- 工厂：平台分发")
    adapter = build_adapter(None)
    if sys.platform.startswith("linux"):
        check(adapter.source.startswith("linux"), f"Linux → LinuxAdapter（实得 {adapter.source}）")
    elif os.name == "nt":
        check(adapter.source == "win32", f"Windows 仍走 Win32Adapter（实得 {adapter.source}）")
    else:
        check(adapter.source == "none", f"其他平台 → 空适配器（实得 {adapter.source}）")

    print("\n--- describe()：日志单行")
    line = pl.describe(pl.Probed(idle_sec=91.0, locked=False, session="wayland",
                                 backend="logind", detail=("via loginctl self",)))
    check("idle=91s" in line and "unlocked" in line, f"{line}")
    line = pl.describe(pl.Probed(session="x11", backend="x11"))
    check("idle=?" in line and "lock?" in line, f"未知字段用 ? 而不是 0/false：{line}")

    failed = sum(1 for ok, _ in RESULTS if not ok)
    print(f"\n共 {len(RESULTS)} 项，失败 {failed} 项")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())