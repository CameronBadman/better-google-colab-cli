# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile


ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "better-colab"
MANIFEST = PLUGIN / ".codex-plugin" / "plugin.json"
PACKAGER = ROOT / "tools" / "package_plugin.py"
DESIGN = ROOT / "docs" / "15_codex_plugin.md"
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def _plugin_files() -> set[str]:
    return {
        path.relative_to(PLUGIN).as_posix()
        for path in PLUGIN.rglob("*")
        if path.is_file()
    }


def test_plugin_manifest_is_skill_only_and_release_versioned():
    manifest = json.loads(MANIFEST.read_text())

    assert manifest["name"] == "better-colab"
    assert manifest["version"] == "0.1.0"
    assert SEMVER.fullmatch(manifest["version"])
    assert manifest["license"] == "Apache-2.0"
    assert manifest["skills"] == "./skills/"
    assert manifest["author"]["name"] == "Cameron Badman"
    assert manifest["repository"] == (
        "https://github.com/CameronBadman/better-google-colab-cli"
    )
    assert "unofficial" in manifest["description"].lower()
    assert manifest["interface"]["displayName"] == "Better Colab"
    assert manifest["interface"]["category"] == "Developer Tools"
    assert manifest["interface"]["capabilities"] == ["Shell"]
    assert "$better-colab" in manifest["interface"]["defaultPrompt"][0]
    assert "mcpServers" not in manifest
    assert "apps" not in manifest
    assert "hooks" not in manifest


def test_plugin_contains_only_manifest_license_and_canonical_skill():
    assert _plugin_files() == {
        ".codex-plugin/plugin.json",
        "LICENSE",
        "skills/better-colab/SKILL.md",
        "skills/better-colab/agents/openai.yaml",
    }
    assert (PLUGIN / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes()
    assert (
        PLUGIN / "skills" / "better-colab" / "SKILL.md"
    ).read_bytes() == (
        ROOT / ".agents" / "skills" / "better-colab" / "SKILL.md"
    ).read_bytes()
    assert (
        PLUGIN / "skills" / "better-colab" / "agents" / "openai.yaml"
    ).read_bytes() == (
        ROOT / ".agents" / "skills" / "better-colab" / "agents" / "openai.yaml"
    ).read_bytes()


def test_plugin_packager_checks_and_builds_reproducible_archive(tmp_path):
    subprocess.run(
        [sys.executable, str(PACKAGER), "check"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    first = subprocess.run(
        [sys.executable, str(PACKAGER), "build", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    archive = Path(first.stdout.strip())
    first_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    second = subprocess.run(
        [sys.executable, str(PACKAGER), "build", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert Path(second.stdout.strip()) == archive
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == first_hash

    with zipfile.ZipFile(archive) as package:
        assert set(package.namelist()) == {
            "better-colab/.codex-plugin/plugin.json",
            "better-colab/LICENSE",
            "better-colab/skills/better-colab/SKILL.md",
            "better-colab/skills/better-colab/agents/openai.yaml",
        }
        archived_manifest = json.loads(
            package.read("better-colab/.codex-plugin/plugin.json")
        )
    assert archived_manifest["version"] == "0.1.0"


def test_ci_checks_plugin_copy_and_release_tag_version():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text()

    assert "tools/package_plugin.py check" in ci
    assert "tools/package_plugin.py build" in release
    assert "plugins/better-colab/.codex-plugin/plugin.json" in release
    assert "github.ref_name" in release
    assert "better-colab-plugin-*.zip" in release
    assert not (ROOT / ".agents" / "plugins" / "marketplace.json").exists()


def test_plugin_design_records_archive_and_isolated_install_validation():
    content = DESIGN.read_text()

    assert "last_updated: 2026-07-18" in content
    assert "plugins/better-colab/.codex-plugin/plugin.json" in content
    assert "tools/package_plugin.py" in content
    assert "integration/repro_plugin_install/test.sh" in content
    assert "temporary marketplace fixture" in content
    assert "No marketplace entry" in content
