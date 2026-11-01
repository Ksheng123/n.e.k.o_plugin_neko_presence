"""读 neko_presence 的 store.db，解 msgpack，统计探针数据。

系统 Python 没装 msgpack，宿主自带的 ormsgpack 是编译二进制不一定能加载，
所以这里自带一个只覆盖采样结构所需的极简 decoder（map/array/str/float/nil/int）。

用法::

    python tools/analyze_probe.py <store.db 路径> [--raw]

store.db 通常在：
    <数据根>/plugins/neko_presence/data/store.db
"""

from __future__ import annotations

import sqlite3
import struct
import sys
from datetime import datetime
from pathlib import Path

# Windows 上重定向到文件时默认用 cp936，中文会乱码；统一走 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 - 老版本 Python 没有 reconfigure
    pass


# ---------------------------------------------------------------- msgpack
def decode(buf: bytes, pos: int = 0):
    """解码一个 msgpack 值，返回 (value, next_pos)。"""
    if pos >= len(buf):
        raise ValueError("unexpected end of buffer")
    b = buf[pos]

    if b <= 0x7F:                      # positive fixint
        return b, pos + 1
    if b == 0xC0:                      # nil
        return None, pos + 1
    if b == 0xC2:
        return False, pos + 1
    if b == 0xC3:
        return True, pos + 1
    if b == 0xCA:                      # float32
        return struct.unpack(">f", buf[pos + 1 : pos + 5])[0], pos + 5
    if b == 0xCB:                      # float64
        return struct.unpack(">d", buf[pos + 1 : pos + 9])[0], pos + 9
    if b == 0xCC:
        return buf[pos + 1], pos + 2
    if b == 0xCD:
        return struct.unpack(">H", buf[pos + 1 : pos + 3])[0], pos + 3
    if b == 0xCE:
        return struct.unpack(">I", buf[pos + 1 : pos + 5])[0], pos + 5
    if b == 0xCF:
        return struct.unpack(">Q", buf[pos + 1 : pos + 9])[0], pos + 9

    if 0x80 <= b <= 0x8F:              # fixmap
        n, pos = b & 0x0F, pos + 1
        out: dict = {}
        for _ in range(n):
            key, pos = decode(buf, pos)
            out[key], pos = decode(buf, pos)
        return out, pos
    if 0x90 <= b <= 0x9F:              # fixarray
        n, pos = b & 0x0F, pos + 1
        arr = []
        for _ in range(n):
            item, pos = decode(buf, pos)
            arr.append(item)
        return arr, pos

    if 0xA0 <= b <= 0xBF:              # fixstr
        n = b & 0x1F
        return buf[pos + 1 : pos + 1 + n].decode("utf-8", "replace"), pos + 1 + n

    if b in (0xD9, 0xDA, 0xDB):        # str8/16/32
        width = {0xD9: 1, 0xDA: 2, 0xDB: 4}[b]
        fmt = {1: ">B", 2: ">H", 4: ">I"}[width]
        n = struct.unpack(fmt, buf[pos + 1 : pos + 1 + width])[0]
        start = pos + 1 + width
        return buf[start : start + n].decode("utf-8", "replace"), start + n

    if b in (0xDC, 0xDD):              # array16/32
        width = 2 if b == 0xDC else 4
        fmt = ">H" if width == 2 else ">I"
        n = struct.unpack(fmt, buf[pos + 1 : pos + 1 + width])[0]
        pos = pos + 1 + width
        arr = []
        for _ in range(n):
            item, pos = decode(buf, pos)
            arr.append(item)
        return arr, pos

    if b in (0xDE, 0xDF):              # map16/32
        width = 2 if b == 0xDE else 4
        fmt = ">H" if width == 2 else ">I"
        n = struct.unpack(fmt, buf[pos + 1 : pos + 1 + width])[0]
        pos = pos + 1 + width
        out = {}
        for _ in range(n):
            key, pos = decode(buf, pos)
            out[key], pos = decode(buf, pos)
        return out, pos

    raise ValueError(f"unsupported msgpack byte 0x{b:02X} at offset {pos}")


# ---------------------------------------------------------------- analyze
def idle_bucket(value) -> str:
    if not isinstance(value, (int, float)):
        return "?"
    if value < 60:
        return "在座"
    if value < 300:
        return "短暂离开"
    if value < 1800:
        return "较久离开"
    return "深度离开"


def segments(rows: list[dict]) -> list[dict]:
    """按 idle 桶把时间线切成连续段，用于一眼看出「什么时候走的、走了多久」。"""
    out: list[dict] = []
    current: dict | None = None
    for row in rows:
        bucket = idle_bucket(row.get("w"))
        if current is None or current["bucket"] != bucket:
            if current is not None:
                out.append(current)
            current = {"bucket": bucket, "start": row, "end": row, "n": 1}
        else:
            current["end"] = row
            current["n"] += 1
    if current is not None:
        out.append(current)
    return out


def load_rows(db: Path) -> list[tuple[str, bytes]]:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return list(conn.execute("SELECT key, value FROM kv_store"))
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    db = Path(argv[1])
    show_raw = "--raw" in argv
    if not db.is_file():
        print(f"not found: {db}")
        return 1

    chunks: list[tuple[str, list[dict]]] = []
    index: list[str] = []
    for key, blob in load_rows(db):
        try:
            value, _ = decode(blob)
        except ValueError as exc:
            print(f"[skip] {key}: {exc}")
            continue
        if key == "probe_index" and isinstance(value, list):
            index = [str(item) for item in value]
        elif isinstance(value, dict) and isinstance(value.get("items"), list):
            chunks.append((key, [row for row in value["items"] if isinstance(row, dict)]))

    total = sum(len(rows) for _, rows in chunks)
    with_w = sum(1 for _, rows in chunks for row in rows if "w" in row)
    print(f"db        : {db}")
    print(f"chunks    : {len(chunks)}  (index says {len(index)})")
    print(f"samples   : {total}   其中带旁路信号 w 的 {with_w} 条")

    fresh = [(k, rows) for k, rows in chunks if any("w" in row for row in rows)]
    if not fresh:
        print("\n还没有带 w 的新数据 —— 探针可能刚开始跑，等几分钟再看。")
        return 0

    rows_all = [row for _, rows in fresh for row in rows if "w" in row]
    rows_all.sort(key=lambda row: row.get("t") or 0)
    first, last = rows_all[0], rows_all[-1]
    fmt = "%Y-%m-%d %H:%M:%S"

    def ts(row: dict) -> str:
        try:
            return datetime.fromtimestamp(float(row.get("t"))).strftime(fmt)
        except (TypeError, ValueError):
            return "?"

    idles = [row["w"] for row in rows_all if isinstance(row.get("w"), (int, float))]
    print(f"\n新数据时间范围: {ts(first)} → {ts(last)}  ({len(rows_all)} 条)")
    if idles:
        print(f"win_idle 秒: min={min(idles):.1f} max={max(idles):.1f}")
        buckets = {"<60s(在座)": 0, "60-300s": 0, "300-1800s": 0, ">=1800s(离开)": 0}
        for value in idles:
            if value < 60:
                buckets["<60s(在座)"] += 1
            elif value < 300:
                buckets["60-300s"] += 1
            elif value < 1800:
                buckets["300-1800s"] += 1
            else:
                buckets[">=1800s(离开)"] += 1
        for label, count in buckets.items():
            print(f"  {label:>16} : {count}")

    privacy = {}
    for row in rows_all:
        privacy[str(row.get("p"))] = privacy.get(str(row.get("p")), 0) + 1
    print(f"privacy 分布: {privacy}")

    if "--segments" in argv:
        print("\n=== 时间线分段（按 idle 桶，相邻同桶合并）")
        print("起始                → 结束                 状态        条数  时长(分)  段末idle")
        for seg in segments(rows_all):
            try:
                duration = (float(seg["end"]["t"]) - float(seg["start"]["t"])) / 60.0
            except (TypeError, ValueError):
                duration = 0.0
            print(
                f"{ts(seg['start'])} → {ts(seg['end'])}  {seg['bucket']:<8} "
                f"{seg['n']:>4}  {duration:>7.1f}  {seg['end'].get('w')}"
            )

    if "--matrix" in argv:
        print("\n=== (q 全屏状态, f 前台进程) × idle 桶 交叉表")
        print("这是判断「看视频 vs 真离开」能不能分开的关键视图")
        table: dict[tuple, dict[str, int]] = {}
        for row in rows_all:
            key = (row.get("q"), row.get("f"))
            bucket = idle_bucket(row.get("w"))
            table.setdefault(key, {})
            table[key][bucket] = table[key].get(bucket, 0) + 1
        order = ["在座", "短暂离开", "较久离开", "深度离开"]
        for key in sorted(table, key=lambda k: -sum(table[k].values())):
            cells = "  ".join(f"{b}={table[key][b]}" for b in order if table[key].get(b))
            print(f"q={key[0]!s:<5} f={key[1]!r:<26} 合计={sum(table[key].values()):<4} {cells}")

    if show_raw:
        print("\n时间                 win_idle   sdk_idle  privacy       category")
        for row in rows_all:
            print(
                f"{ts(row)}  {str(row.get('w')):>8}  {str(row.get('i')):>8}  "
                f"{str(row.get('p')):>12}  {row.get('c')!r}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
