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

from pathlib import Path


ROOT = Path(__file__).parents[1]
SKILL = ROOT / ".agents" / "skills" / "better-colab" / "SKILL.md"
OPENAI_YAML = SKILL.parent / "agents" / "openai.yaml"


def test_portable_skill_is_compact_and_has_required_metadata():
    content = SKILL.read_text()

    assert len(content.encode()) < 4096
    assert content.startswith("---\n")
    frontmatter = content.split("---\n", 2)[1]
    assert "name: better-colab\n" in frontmatter
    assert "description:" in frontmatter


def test_portable_skill_teaches_the_agent_first_contract():
    content = SKILL.read_text()

    required_fragments = (
        "better-colab doctor --format json",
        "better-colab capabilities",
        "better-colab session ensure",
        "better-colab execution start",
        "--idempotency-key",
        "--detach",
        "better-colab execution wait",
        "next_cursor",
        "better-colab execution output",
        "--expected-source-sha256",
        "better-colab notebook update",
        "--expected-sha256",
        "better-colab session stop",
        "exit 124",
        "unknown",
        "sha256:",
    )
    for fragment in required_fragments:
        assert fragment in content


def test_portable_skill_is_vendor_neutral_drive_free_and_self_contained():
    content = SKILL.read_text().lower()

    assert "drivemount" not in content
    assert "google.colab.drive" not in content
    assert "mcp" not in content
    assert "codex" not in content
    assert "claude" not in content
    assert not (SKILL.parent / "scripts").exists()
    assert not (SKILL.parent / "references").exists()


def test_portable_skill_has_agent_catalog_metadata():
    content = OPENAI_YAML.read_text()

    assert 'display_name: "Better Colab"' in content
    assert 'short_description: "Operate durable Google Colab sessions safely"' in content
    assert 'default_prompt: "Use $better-colab ' in content


def test_stale_bundled_skill_is_removed():
    assert not (ROOT / "skills" / "colab-operator" / "SKILL.md").exists()
    assert "skills/colab-operator/SKILL.md" not in (
        ROOT / "pyproject.toml"
    ).read_text()
