"""一次性：查插件市场是否已有同定位插件 / slug 是否被占。用完即删。"""
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

BASE = "https://market.project-neko.cn/api/v1/plugins"


def fetch_all():
    items = []
    page = 1
    while True:
        with urllib.request.urlopen(f"{BASE}?page={page}&page_size=50", timeout=25) as r:
            data = json.load(r)
        batch = data.get("items") or []
        items += batch
        if not batch or page >= int(data.get("total_pages") or 1):
            break
        page += 1
    return items


items = fetch_all()
print(f"market plugins: {len(items)}\n")

slugs = {str(it.get("slug")): it for it in items}
print("slug 'neko_presence' taken?", "neko_presence" in slugs)
print()

KW = [
    "presence", "在场", "陪伴", "一天", "日程", "时间轴", "记录", "生活",
    "安静", "离开", "在场感", "日记", "回忆", "作息",
]
hits = []
for it in items:
    blob = " ".join(
        str(it.get(k) or "")
        for k in ("name", "slug", "short_description", "description")
    ).lower()
    if any(k.lower() in blob for k in KW):
        hits.append(it)

print(f"--- 关键词命中 {len(hits)} 个 ---")
for it in hits:
    ver = it.get("latest_version") or {}
    v = ver.get("version") if isinstance(ver, dict) else ver
    print(f"[{it.get('id')}] {it.get('slug')} | {it.get('name')} | v{v}")
    print(f"     {(it.get('short_description') or it.get('description') or '')[:120]}")
