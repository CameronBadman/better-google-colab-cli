#!/usr/bin/env python3
"""Synchronize, check, and reproducibly archive the Better Colab plugin."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "better-colab"
MANIFEST = PLUGIN / ".codex-plugin" / "plugin.json"
COPIES = {
    ROOT / "LICENSE": PLUGIN / "LICENSE",
    ROOT / ".agents" / "skills" / "better-colab" / "SKILL.md": (
        PLUGIN / "skills" / "better-colab" / "SKILL.md"
    ),
    ROOT / ".agents" / "skills" / "better-colab" / "agents" / "openai.yaml": (
        PLUGIN / "skills" / "better-colab" / "agents" / "openai.yaml"
    ),
}
ARCHIVE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def expected_files() -> set[Path]:
    return {MANIFEST, *COPIES.values()}


def plugin_files() -> set[Path]:
    return {path for path in PLUGIN.rglob("*") if path.is_file()}


def synchronize() -> None:
    for source, destination in COPIES.items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())


def check() -> list[str]:
    errors: list[str] = []
    expected = expected_files()
    actual = plugin_files()
    for path in sorted(expected - actual):
        errors.append(f"missing {path.relative_to(ROOT)}")
    for path in sorted(actual - expected):
        errors.append(f"unexpected {path.relative_to(ROOT)}")
    for source, destination in COPIES.items():
        if destination.is_file() and destination.read_bytes() != source.read_bytes():
            errors.append(
                f"{destination.relative_to(ROOT)} differs from "
                f"{source.relative_to(ROOT)}"
            )
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        errors.append("plugin manifest is missing or invalid JSON")
    else:
        if manifest.get("name") != "better-colab":
            errors.append("plugin manifest name must be better-colab")
        if manifest.get("skills") != "./skills/":
            errors.append("plugin manifest must discover only ./skills/")
        for forbidden in ("apps", "hooks", "mcpServers"):
            if forbidden in manifest:
                errors.append(f"plugin manifest must not declare {forbidden}")
    return errors


def require_clean_package() -> None:
    errors = check()
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        raise SystemExit(1)


def build_archive(out_dir: Path) -> Path:
    require_clean_package()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    version = manifest["version"]
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = (out_dir / f"better-colab-plugin-{version}.zip").resolve()
    with zipfile.ZipFile(
        archive,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as package:
        for path in sorted(expected_files()):
            relative = path.relative_to(PLUGIN).as_posix()
            info = zipfile.ZipInfo(
                f"better-colab/{relative}",
                date_time=ARCHIVE_TIMESTAMP,
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            package.writestr(info, path.read_bytes(), compresslevel=9)
    return archive


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("sync", help="Copy canonical skill and licence bytes")
    subcommands.add_parser("check", help="Fail if plugin package bytes drift")
    build = subcommands.add_parser("build", help="Build a deterministic ZIP archive")
    build.add_argument("--out-dir", type=Path, default=ROOT / "dist")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "sync":
        synchronize()
        require_clean_package()
        print(PLUGIN)
    elif args.command == "check":
        require_clean_package()
        print("Plugin package is synchronized.")
    else:
        print(build_archive(args.out_dir))


if __name__ == "__main__":
    main()
