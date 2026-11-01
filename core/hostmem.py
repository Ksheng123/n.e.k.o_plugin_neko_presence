"""从宿主记忆里借一个日期 —— 只读时间戳，不读内容。

为什么是「借」而不是「查」
------------------------
宿主 SDK **没有开放记忆接口**。核对过宿主里 5 个已安装插件能 import 的符号，
只有 ``Ok / Err / Result / SdkError / plugin_entry / quick_action / tr / ui /
llm_tool / get_os_activity_snapshot`` —— 没有任何一个能拿记忆或角色信息。
所以要「知道她记得多久」只能按目录结构定位宿主根目录，再直接读它的文件。

三条铁律
--------
1. **只读**。SQLite 一律 ``mode=ro`` 打开；不建表、不写文件、不改数据。
2. **只取时间**。只查 ``MIN(timestamp)``，绝不 SELECT ``message`` 之类的正文；
   persona 也只读 ``__meta__.facts[].migrated_at``，不碰事实内容。
3. **读不到就 None**。不猜、不编，也不把异常抛给上层 —— 记忆库可能正被宿主
   写入（``database is locked``）、可能是旧版本没有这张表、也可能根本不存在。

为什么不用别的插件的数据
------------------------
``forever_companion`` 里的 ``first_seen`` 是**它自己的初装时间**（本机实测
2026-10-29，而真实初识是 2026-06-19），拿它当起点会把四个月记忆抹掉；而且
它的 kv_store 是 msgpack 二进制，读它就得跟着它的版本变。本插件在
README §2.1 里承诺「只被读、不反向依赖任何插件」，这条同样适用于此。
所以这里只读**宿主自己的**文件：记忆库 + 插件锁。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

#: 宿主根目录的判定标志：两者同时存在才算数，避免认错成别的目录
_MEM_DIR = "memory"
_LOCK_NAME = "plugins.lock.json"
_CHARS_PATH = Path("config") / "characters.json"

#: 记忆库文件名与表名（宿主内部实现，只读、只这一处）
_TALK_DB = "time_indexed.db"
_TALK_TABLE = "time_indexed_original"

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def date_of(raw: object) -> str | None:
    """把各种时间表示压成 ``YYYY-MM-DD``；不像日期就返回 None。

    只做粗校验（年月日范围），脏数据宁可不认，也不要把 2026-13-45 这种东西
    送到界面上 —— 前端拿它当月历的左边界会直接算错。
    """
    m = _DATE_RE.search(str(raw or ""))
    if not m:
        return None
    year, month, day = (int(g) for g in m.groups())
    if not (2000 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def host_root(start: Path | str | None = None, levels: int = 6) -> Path | None:
    """从当前文件往上找宿主根目录。

    装好之后代码在 ``<root>/.neko-plugin-installations/plugins/neko_presence/``，
    往上三级就是根；但开发态可能从源码目录直接跑，所以多留两级余地。
    判定标准是 ``memory/`` 与 ``plugins.lock.json`` 同时存在 —— 只认结构，不认路径名。
    """
    here = Path(start or __file__).resolve()
    node = here if here.is_dir() else here.parent
    for _ in range(max(1, levels)):
        if (node / _MEM_DIR).is_dir() and (node / _LOCK_NAME).is_file():
            return node
        if node.parent == node:
            break
        node = node.parent
    return None


def current_character(root: Path) -> str | None:
    """读 ``config/characters.json`` 里的「当前猫娘」。

    字段可能只是名字，也可能是带 ``昵称`` 的对象，两种都认。
    """
    path = root / _CHARS_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("当前猫娘")
    if isinstance(raw, str):
        return raw.strip() or None
    if isinstance(raw, dict):
        for key in ("昵称", "name", "档案名"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def memory_dirs(root: Path) -> list[Path]:
    """列出要采信的记忆目录。

    有「当前猫娘」就只认她 —— 这页年历说的是「她」，不能混进别的角色的记忆。
    拿不到当前角色时退回全部，最后取最早的那个日期（宁可保守，不凭空延后）。
    """
    base = root / _MEM_DIR
    try:
        dirs = sorted(p for p in base.iterdir() if p.is_dir())
    except OSError:
        return []
    if not dirs:
        return []
    name = current_character(root)
    if name:
        hit = [d for d in dirs if d.name == name]
        if hit:
            return hit
    return dirs


def first_talk_date(mem: Path) -> str | None:
    """记忆库里最早一条对话的日期。这是三条证据里最硬的一条。"""
    db = mem / _TALK_DB
    if not db.is_file():
        return None
    try:
        uri = f"{db.as_uri()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=1.0)
    except (sqlite3.Error, ValueError):
        return None
    try:
        row = con.execute(f"SELECT MIN(timestamp) FROM {_TALK_TABLE}").fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return date_of(row[0]) if row and row[0] else None


def persona_date(mem: Path) -> str | None:
    """``persona.json`` 里 ``__meta__.facts[].migrated_at`` 的最早日期。

    注意这是**迁移种子**的时间，不是「初识」。全新安装时两者同日；从旧版本
    升级过来的用户，它会偏晚 —— 所以它只作为次选，单独用会砍掉真实历史。
    """
    path = mem / "persona.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    meta = data.get("__meta__") if isinstance(data, dict) else None
    facts = meta.get("facts") if isinstance(meta, dict) else None
    if not isinstance(facts, list):
        return None
    dates = []
    for fact in facts:
        if isinstance(fact, dict):
            got = date_of(fact.get("migrated_at"))
            if got:
                dates.append(got)
    return min(dates) if dates else None


def host_init_date(root: Path) -> str | None:
    """宿主首次初始化的日期（``plugins.lock.json`` 的 ``created_at``，UTC 转本地）。"""
    path = root / _LOCK_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = data.get("created_at") if isinstance(data, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        stamp = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return date_of(raw)
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone()  # 转成本地时间再取日期，否则会差一天
    return date_of(stamp.isoformat())


def months_ago(now: datetime, months: int) -> str:
    """``now`` 往前推若干自然月，返回 ``YYYY-MM-DD``。

    这是探测结果的**回追上限**：老用户的记忆可能横跨数年，界面按整月翻页，
    不设上限就是几百页空月历。

    自己算月序，不引 dateutil：年月摊平成 ``year*12+month`` 再减，跨年自然就对；
    日子越界（3-31 往前推一个月）按当月最后一天收口。
    """
    total = (now.year * 12 + now.month - 1) - max(0, int(months))
    year, month = divmod(total, 12)
    month += 1
    if month == 12:
        last = 31
    else:
        last = (datetime(year, month + 1, 1) - timedelta(days=1)).day
    return f"{year:04d}-{month:02d}-{min(now.day, last):02d}"


def probe_since(start: Path | str | None = None) -> tuple[str | None, dict]:
    """探测「你和她从哪天开始」，返回 ``(日期, 证据)``。

    优先级（取最早的记忆类证据，没有记忆才退到宿主初始化时间）：

        first_talk_date  >  persona_date  >  host_init_date

    证据字典只用于写日志，方便出问题时一眼看出是哪个来源定的边界。
    """
    evidence: dict[str, object] = {
        "root": None,
        "char": None,
        "talk": None,
        "persona": None,
        "host": None,
        "src": None,
    }
    root = host_root(start)
    if root is None:
        return None, evidence

    evidence["root"] = str(root)
    dirs = memory_dirs(root)
    evidence["char"] = ",".join(d.name for d in dirs) or None

    talks, personas = [], []
    for mem in dirs:
        got = first_talk_date(mem)
        if got:
            talks.append(got)
        got = persona_date(mem)
        if got:
            personas.append(got)

    evidence["talk"] = min(talks) if talks else None
    evidence["persona"] = min(personas) if personas else None
    evidence["host"] = host_init_date(root)

    memory_marks = [d for d in (evidence["talk"], evidence["persona"]) if d]
    if memory_marks:
        evidence["src"] = "memory"
        return min(memory_marks), evidence  # type: ignore[arg-type]
    if evidence["host"]:
        evidence["src"] = "host"
        return str(evidence["host"]), evidence
    return None, evidence
