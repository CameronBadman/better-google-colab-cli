# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from pathlib import Path
import tomllib
from unittest.mock import patch

from typer.testing import CliRunner

from colab_cli.cli import app as compatibility_app


ROOT = Path(__file__).parents[1]
runner = CliRunner()


def _load_toml(path: Path) -> dict:
    with path.open("rb") as file:
        return tomllib.load(file)


def test_core_distribution_installs_only_better_colab():
    metadata = _load_toml(ROOT / "pyproject.toml")

    assert metadata["project"]["name"] == "better-google-colab-cli"
    assert metadata["project"]["scripts"] == {
        "better-colab": "better_colab.cli:main"
    }
    assert metadata["project"]["urls"] == {
        "Homepage": "https://github.com/CameronBadman/better-google-colab-cli",
        "Repository": "https://github.com/CameronBadman/better-google-colab-cli",
        "Issues": "https://github.com/CameronBadman/better-google-colab-cli/issues",
        "Upstream": "https://github.com/googlecolab/google-colab-cli",
    }
    assert metadata["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/better_colab",
        "src/colab_cli",
    ]


def test_compat_distribution_installs_only_colab():
    metadata = _load_toml(ROOT / "compat" / "pyproject.toml")

    assert metadata["project"]["name"] == "better-google-colab-cli-compat"
    assert metadata["project"]["scripts"] == {"colab": "colab_cli.cli:main"}
    assert set(metadata["project"]["dynamic"]) == {"dependencies", "version"}
    assert (ROOT / "compat" / "hatch_build.py").is_file()


def test_better_surface_excludes_drive_and_legacy_surface_retains_it():
    from better_colab.cli import app as better_app

    better_help = runner.invoke(better_app, ["--help"])
    compatibility_help = runner.invoke(compatibility_app, ["--help"])

    assert better_help.exit_code == 0
    assert "Better Colab CLI" in better_help.output
    assert "drivemount" not in better_help.output
    assert compatibility_help.exit_code == 0
    assert "drivemount" in compatibility_help.output


def test_better_surface_never_performs_implicit_update_checks(mock_common_state):
    from better_colab.cli import app as better_app

    with patch("colab_cli.auto_update.run_background_check") as check:
        result = runner.invoke(better_app, ["sessions"])

    assert result.exit_code == 0
    check.assert_not_called()


def test_fork_is_prominently_unofficial_and_keeps_attribution():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    licence = (ROOT / "LICENSE").read_text(encoding="utf-8")

    assert "Unofficial" in readme.splitlines()[0]
    assert "not affiliated with or endorsed by Google" in readme
    assert "Apache License" in licence
    assert "Google LLC" in (ROOT / "src" / "colab_cli" / "cli.py").read_text(
        encoding="utf-8"
    )


def test_repository_uses_neutral_ci_and_release_workflows():
    assert not (ROOT / "cloudbuild.yaml").exists()
    assert (ROOT / ".github" / "workflows" / "ci.yml").is_file()
    assert (ROOT / ".github" / "workflows" / "release.yml").is_file()

