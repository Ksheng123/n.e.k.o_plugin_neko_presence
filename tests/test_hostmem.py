"""宿主记忆探测测试：只读、只取时间、读不到就 None。

不依赖宿主、不依赖 SDK，用一个假的宿主目录跑：

    python tests/test_hostmem.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from core.hostmem import (  # noqa: E402
    current_character,
    date_of,
    first_talk_date,
    host_init_date,
    host_root,
    memory_dirs,
    months_ago,
    persona_date,
    probe_since,
)

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


def build_host(root: Path, *, talk: list[str] | None = None, migrated: str | None = None,
               lock: str | None = "2026-06-19T14:00:44.392654Z", char: str = "YUI") -> None:
    """搭一个最小可用的假宿主：memory/<char>/ + config/characters.json + plugins.lock.json。"""
    mem = root / "memory" / char
    mem.mkdir(parents=True, exist_ok=True)
    (root / "memory").mkdir(parents=True, exist_ok=True)

    if talk is not None:
        con = sqlite3.connect(mem / "time_indexed.db")
        con.execute(
            "CREATE TABLE time_indexed_original "
            "(id INTEGER PRIMARY KEY, session_id TEXT, message TEXT, timestamp TEXT)"
        )
        for i, ts in enumerate(talk):
            con.execute(
                "INSERT INTO time_indexed_original (session_id, message, timestamp) VALUES (?,?,?)",
                ("s1", "这句不该被读到", ts),
            )
        con.commit()
        con.close()

    if migrated is not None:
        (mem / "persona.json").write_text(
            json.dumps(
                {"neko": {}, "__meta__": {"facts": [{"id": "x", "migrated_at": migrated}]}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    if lock is not None:
        (root / "plugins.lock.json").write_text(
            json.dumps({"schema_version": 2, "created_at": lock}), encoding="utf-8"
        )

    cfg = root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "characters.json").write_text(
        json.dumps({"当前猫娘": char, "猫娘": {char: {"昵称": char}}}, ensure_ascii=False),
        encoding="utf-8",
    )


def main() -> int:
    # ---- date_of：脏数据必须被拒 ----
    check(date_of("2026-06-19 20:47:59.043884") == "2026-06-19", "date_of 兼容 SQLite 时间戳")
    check(date_of("2026-06-19T20:46:31.882933") == "2026-06-19", "date_of 兼容 ISO")
    check(date_of("2026-13-45") is None, "date_of 拒绝非法月份")
    check(date_of("") is None and date_of(None) is None, "date_of 空值返回 None")
    check(date_of("不是日期") is None, "date_of 非日期返回 None")

    # ---- months_ago：回追上限的日期算法 ----
    from datetime import datetime  # 局部导入，避免和上面的字符串常量混在一起

    check(months_ago(datetime(2026, 11, 1), 36) == "2023-11-01", "months_ago 跨年正确")
    check(months_ago(datetime(2026, 1, 15), 1) == "2025-12-15", "months_ago 减到上一年")
    check(months_ago(datetime(2026, 3, 31), 1) == "2026-02-28", "months_ago 越界按当月最后一天")
    check(months_ago(datetime(2024, 3, 31), 1) == "2024-02-29", "months_ago 认得闰年")
    check(months_ago(datetime(2026, 11, 1), 0) == "2026-11-01", "months_ago 零个月原样返回")
    check(months_ago(datetime(2026, 11, 1), -5) == "2026-11-01", "months_ago 负数不吃掉日期")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "N.E.K.O"
        build_host(root, talk=["2026-06-19 20:47:59.043884", "2026-11-01 10:00:00"], migrated="2026-06-19T20:46:31")

        # ---- 目录发现 ----
        plugin_dir = root / ".neko-plugin-installations" / "plugins" / "neko_presence"
        plugin_dir.mkdir(parents=True)
        check(host_root(plugin_dir / "__init__.py") == root, "host_root 从安装目录向上认到根")
        check(host_root(plugin_dir) == root, "host_root 也接受目录")
        # memory/YUI/x.db 往上三级才到根，只给两级就该认不出来（不瞎猜）
        check(host_root(root / "memory" / "YUI" / "x.db", levels=2) is None, "host_root 层级不够时返回 None")

        # ---- 单点读取 ----
        mem = root / "memory" / "YUI"
        check(first_talk_date(mem) == "2026-06-19", "first_talk_date 取最早一条对话")
        check(persona_date(mem) == "2026-06-19", "persona_date 取迁移种子")
        check(host_init_date(root) == "2026-06-19", "host_init_date 转成本地日期")
        check(current_character(root) == "YUI", "current_character 读到当前猫娘")
        check([d.name for d in memory_dirs(root)] == ["YUI"], "memory_dirs 只认当前猫娘")

        # ---- probe：记忆优先于宿主初始化 ----
        got, ev = probe_since(plugin_dir / "__init__.py")
        check(got == "2026-06-19", "probe_since 取到初识日", str(got))
        check(ev["src"] == "memory", "probe_since 来源标记为 memory", str(ev["src"]))
        check(ev["char"] == "YUI", "证据里带角色名")

        # ---- 只读验证：读一遍之后库还能正常打开，且内容没被改 ----
        con = sqlite3.connect(mem / "time_indexed.db")
        n = con.execute("SELECT COUNT(*) FROM time_indexed_original").fetchone()[0]
        con.close()
        check(n == 2, "探测不增删数据", f"rows={n}")

        # ---- 记忆比宿主锁更早时，取记忆 ----
        root2 = Path(tmp) / "N.E.K.O2"
        build_host(root2, talk=["2024-03-02 09:00:00"], migrated=None, lock="2026-06-19T14:00:44Z")
        got, ev = probe_since(root2)
        check(got == "2024-03-02", "记忆早于宿主初始化时取记忆", str(got))

        # ---- 只有宿主锁，没有记忆 ----
        root3 = Path(tmp) / "N.E.K.O3"
        build_host(root3, talk=None, migrated=None, lock="2025-01-05T02:00:00Z")
        got, ev = probe_since(root3)
        check(got == "2025-01-05", "无记忆时退回宿主初始化日", str(got))
        check(ev["src"] == "host", "来源标记为 host", str(ev["src"]))

        # ---- 什么都没有：必须是 None，不能编日期 ----
        root4 = Path(tmp) / "empty"
        root4.mkdir()
        got, ev = probe_since(root4)
        check(got is None, "没有宿主结构时返回 None", str(got))
        check(ev["root"] is None, "证据里标出没找到根目录")

        # ---- 记忆库损坏 / 正在被写：不能抛异常 ----
        root5 = Path(tmp) / "N.E.K.O5"
        build_host(root5, talk=None, migrated=None, lock="2026-06-19T14:00:44Z")
        bad = root5 / "memory" / "YUI" / "time_indexed.db"
        bad.write_bytes(b"not a sqlite file at all")
        check(first_talk_date(root5 / "memory" / "YUI") is None, "坏库返回 None 而不是抛异常")
        got, _ = probe_since(root5)
        check(got == "2026-06-19", "坏库不影响其它证据", str(got))

        # ---- 多角色：当前猫娘缺失时全部采信，取最早 ----
        root6 = Path(tmp) / "N.E.K.O6"
        build_host(root6, talk=["2026-05-01 10:00:00"], migrated=None, char="YUI")
        other = root6 / "memory" / "MIKU"
        other.mkdir(parents=True)
        con = sqlite3.connect(other / "time_indexed.db")
        con.execute(
            "CREATE TABLE time_indexed_original "
            "(id INTEGER PRIMARY KEY, session_id TEXT, message TEXT, timestamp TEXT)"
        )
        con.execute(
            "INSERT INTO time_indexed_original (session_id, message, timestamp) VALUES (?,?,?)",
            ("s", "x", "2025-12-01 08:00:00"),
        )
        con.commit()
        con.close()
        check(len(memory_dirs(root6)) == 1, "多角色时只认当前猫娘")
        (root6 / "config" / "characters.json").write_text("{}", encoding="utf-8")
        check(len(memory_dirs(root6)) == 2, "角色信息缺失时全部采信")
        got, _ = probe_since(root6)
        check(got == "2025-12-01", "取全部角色里最早的日期", str(got))

        # ---- 记忆晚于宿主锁时不应被锁带早 ----
        root7 = Path(tmp) / "N.E.K.O7"
        build_host(root7, talk=["2026-10-25 10:00:00"], migrated=None, lock="2020-01-01T00:00:00Z")
        got, _ = probe_since(root7)
        check(got == "2026-10-25", "有记忆时不采信更早的宿主锁", str(got))

    failed = sum(1 for ok, _ in RESULTS if not ok)
    print(f"\n共 {len(RESULTS)} 项，失败 {failed} 项")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
