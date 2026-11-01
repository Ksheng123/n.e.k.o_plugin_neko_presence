"""手工构建 .neko-plugin 包（宿主冻结版没有 neko_plugin_cli，官方 CLI 不可用）。

参考实包结构（sys_monitor / forever_companion 解包所得）::

    manifest.toml
    metadata.toml
    payload/dependencies.toml
    payload/plugins/<id>/plugin.toml
    payload/plugins/<id>/__init__.py
    payload/profiles/default.toml

注意：当前 CLI 会额外生成 payload/plugins/<id>/plugin.meta.json（预派生 handler 元数据），
但市场在售的 sys_monitor 包里没有它，安装后照样正常运行 —— 说明它不是导入的硬性要求。

用法::

    python tools/build_package.py [--out dist/neko_presence.neko-plugin]
"""

from __future__ import annotations

import argparse
import hashlib
import tomllib
import zipfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

# 只打进运行必需的文件；DESIGN.md / tools/ / tests / __pycache__ 一律排除。
# V0.3 起代码分 core/（纯逻辑）与 ui/（面板 TSX）两个子目录，须整体打包。
INCLUDE_FILES = ("plugin.toml", "__init__.py", "README.md")
INCLUDE_DIRS = ("core", "ui")
EXCLUDE_PARTS = ("__pycache__",)


def _collect_payload(entries: dict[str, bytes]) -> None:
    """把 plugin.toml / __init__.py / README.md / core/** / ui/** 收进 payload。

    写进 zip 的路径一律用正斜杠（zip 规范），目录递归但排除 __pycache__。
    """
    plugin_id = None
    meta = tomllib.loads((PLUGIN_ROOT / "plugin.toml").read_text(encoding="utf-8"))["plugin"]
    plugin_id = str(meta["id"])

    def _add(file: Path) -> None:
        rel = file.relative_to(PLUGIN_ROOT).as_posix()
        entries[f"payload/plugins/{plugin_id}/{rel}"] = file.read_bytes()

    for name in INCLUDE_FILES:
        path = PLUGIN_ROOT / name
        if not path.is_file():
            raise FileNotFoundError(path)
        _add(path)
    for d in INCLUDE_DIRS:
        for file in sorted((PLUGIN_ROOT / d).rglob("*")):
            if file.is_file() and not any(p in EXCLUDE_PARTS for p in file.parts):
                _add(file)


def _payload_hash(entries: dict[str, bytes]) -> str:
    """按 (相对路径 + 内容) 顺序喂 sha256，路径升序。

    与官方 compute_archive_payload_hash 未逐位对齐（宿主导入若校验会报错），
    但在没有 CLI 的前提下这是唯一可自洽复现的写法。
    """
    digest = hashlib.sha256()
    for name in sorted(entries):
        digest.update(name.encode("utf-8"))
        digest.update(entries[name])
    return digest.hexdigest()


def build(out: Path) -> Path:
    manifest_data = tomllib.loads((PLUGIN_ROOT / "plugin.toml").read_text(encoding="utf-8"))
    meta = manifest_data["plugin"]
    plugin_id = str(meta["id"])
    version = str(meta["version"])
    display = str(meta["name"])
    description = str(meta.get("description") or meta.get("short_description") or "")

    entries: dict[str, bytes] = {}
    _collect_payload(entries)

    deps = (
        'schema_version = "1.0"\n\n'
        f"[plugins.{plugin_id}]\n"
        "python_requirements = []\n"
        "host_python_requirements = []\n"
        "plugin_dependencies = []\n"
        "advanced_plugin_dependencies = []\n"
        f'vendor_path = "plugins/{plugin_id}/vendor"\n'
        "vendor_present = false\n"
    ).encode("utf-8")
    entries["payload/dependencies.toml"] = deps

    profile = (
        'name = "default"\n'
        f'enabled_plugins = ["{plugin_id}"]\n\n'
        f"[plugin.{plugin_id}]\n"
        "enabled = true\n"
        "auto_start = true\n"
    ).encode("utf-8")
    entries["payload/profiles/default.toml"] = profile

    manifest = (
        'schema_version = "1.0"\n'
        'package_type = "plugin"\n\n'
        f'id = "{plugin_id}"\n'
        f'package_name = "{display}"\n'
        f'version = "{version}"\n'
        f'package_description = "{description.strip().splitlines()[0]}"\n'
    ).encode("utf-8")

    metadata = (
        "[payload]\n"
        'hash_algorithm = "sha256"\n'
        f'hash = "{_payload_hash(entries)}"\n\n'
        "[source]\n"
        'kind = "local"\n'
        f'paths = ["{plugin_id}"]\n'
    ).encode("utf-8")

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.toml", manifest)
        archive.writestr("metadata.toml", metadata)
        for name in sorted(entries):
            archive.writestr(name, entries[name])
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=PLUGIN_ROOT.parent / "dist" / "neko_presence.neko-plugin")
    args = parser.parse_args()
    target = build(args.out.resolve())
    print(f"built: {target}")
    print(f"bytes: {target.stat().st_size}")
    with zipfile.ZipFile(target) as archive:
        for name in archive.namelist():
            print(f"  {name}  [{len(archive.read(name))}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
