import json
from pathlib import Path
import subprocess
import sys

from better_colab import ExecutionResult
from better_colab.entrypoint import _parse_fast_status, _run_fast_status


ROOT = Path(__file__).parents[1]
EXECUTION_ID = "00000000-0000-4000-8000-000000000777"


class FakeClient:
    request = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execution_status(self, execution_id, *, include):
        type(self).request = {
            "kwargs": self.kwargs,
            "execution_id": execution_id,
            "include": include,
        }
        return ExecutionResult(
            execution_id=execution_id,
            session="benchmark",
            state="interrupted",
            source_sha256="a" * 64,
            output_complete=True,
            created_at="2026-07-18T00:00:00Z",
            updated_at="2026-07-18T00:00:01Z",
        )


def test_core_script_targets_lazy_entrypoint():
    pyproject = (ROOT / "pyproject.toml").read_text()

    assert 'better-colab = "better_colab.entrypoint:main"' in pyproject


def test_entrypoint_import_does_not_load_full_cli():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import better_colab.entrypoint; "
                "assert 'better_colab.cli' not in sys.modules; "
                "assert 'colab_cli.cli' not in sys.modules"
            ),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_public_facade_defers_client_until_export_is_accessed():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import better_colab; "
                "assert 'better_colab.client' not in sys.modules; "
                "assert better_colab.BetterColabClient; "
                "assert 'better_colab.client' in sys.modules"
            ),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_parse_fast_json_status_with_global_profile_flags():
    request = _parse_fast_status(
        [
            "--auth=adc",
            "--config",
            "/tmp/sessions.json",
            "--client-oauth-config=/tmp/oauth.json",
            "execution",
            "status",
            EXECUTION_ID,
            "--include",
            "transitions",
            "--include=traceback",
            "--format=json",
        ]
    )

    assert request == {
        "execution_id": EXECUTION_ID,
        "include": ["transitions", "traceback"],
        "config_path": "/tmp/sessions.json",
        "auth_provider": "adc",
        "oauth_config_path": "/tmp/oauth.json",
    }


def test_fast_path_rejects_text_unknown_and_non_status_commands():
    assert (
        _parse_fast_status(
            ["execution", "status", EXECUTION_ID, "--format=text"]
        )
        is None
    )
    assert (
        _parse_fast_status(
            ["execution", "status", EXECUTION_ID, "--unknown", "--format=json"]
        )
        is None
    )
    assert (
        _parse_fast_status(
            ["execution", "wait", EXECUTION_ID, "--format=json"]
        )
        is None
    )


def test_fast_status_emits_one_schema_v1_object(capsys):
    request = {
        "execution_id": EXECUTION_ID,
        "include": ["traceback"],
        "config_path": "/tmp/sessions.json",
        "auth_provider": "oauth2",
        "oauth_config_path": "/tmp/oauth.json",
    }

    exit_code = _run_fast_status(request, client_type=FakeClient)

    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    payload = json.loads(captured.out)
    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    assert payload["result"]["state"] == "interrupted"
    assert exit_code == 0
    assert FakeClient.request == {
        "kwargs": {
            "config_path": "/tmp/sessions.json",
            "auth_provider": "oauth2",
            "oauth_config_path": "/tmp/oauth.json",
        },
        "execution_id": EXECUTION_ID,
        "include": ["traceback"],
    }
