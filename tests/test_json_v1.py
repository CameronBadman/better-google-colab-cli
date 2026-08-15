import json

import pytest
from typer.testing import CliRunner

from better_colab import (
    BetterColabClient,
    BetterColabError,
    ErrorDetail,
    ExecutionState,
    HealthResult,
)
from better_colab.cli import app
from better_colab.protocol import (
    DEFAULT_EXECUTION_LIMIT,
    MAX_COLLECTION_LIMIT,
    MAX_RESPONSE_BYTES,
    ExitCode,
    render_error_bytes,
    render_success_bytes,
)


runner = CliRunner()


def _json_output(result) -> dict:
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    return json.loads(lines[0])


def test_public_facade_is_typed_and_terminal_independent():
    client = BetterColabClient()

    result = client.capabilities(command="execution.start")

    assert result.commands[0].name == "execution start"
    assert result.commands[0].arguments
    assert ExecutionState.UNKNOWN.value == "unknown"
    assert "typer" not in type(result).__module__


def test_capabilities_are_bounded_and_cursor_paged():
    client = BetterColabClient()

    first = client.capabilities()
    repeated = client.capabilities()
    second = client.capabilities(cursor=first.next_cursor)

    assert len(first.commands) == DEFAULT_EXECUTION_LIMIT
    assert first.next_cursor is not None
    assert first == repeated
    assert first.limits.output_page_min_bytes == 512
    assert first.limits.output_page_max_bytes == MAX_RESPONSE_BYTES // 2
    assert {item.name for item in first.commands}.isdisjoint(
        item.name for item in second.commands
    )
    assert all("drive" not in item.name for item in first.commands + second.commands)


def test_capabilities_reject_invalid_cursor_and_limit():
    client = BetterColabClient()

    with pytest.raises(BetterColabError) as cursor_error:
        client.capabilities(cursor="not-a-cursor")
    assert cursor_error.value.error.code == "INVALID_CURSOR"
    assert cursor_error.value.exit_code == ExitCode.USAGE

    with pytest.raises(BetterColabError) as limit_error:
        client.capabilities(limit=MAX_COLLECTION_LIMIT + 1)
    assert limit_error.value.error.code == "INVALID_LIMIT"
    assert limit_error.value.exit_code == ExitCode.USAGE


def test_unknown_capability_is_a_stable_not_found_error():
    result = runner.invoke(app, ["capabilities", "does-not-exist", "--format", "json"])

    assert result.exit_code == ExitCode.NOT_FOUND
    assert result.stderr == ""
    assert _json_output(result) == {
        "schema_version": 1,
        "ok": False,
        "error": {
            "code": "COMMAND_NOT_FOUND",
            "message": "Unknown command: does-not-exist",
            "retryable": False,
            "suggested_action": "run_capabilities",
        },
    }


def test_capabilities_cli_emits_one_compact_schema_v1_object():
    result = runner.invoke(app, ["capabilities", "execution.start", "--format=json"])

    payload = _json_output(result)
    assert result.exit_code == ExitCode.OK
    assert result.stderr == ""
    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    assert payload["result"]["commands"][0]["name"] == "execution start"
    assert "\n" not in result.stdout.rstrip("\n")
    assert len(result.stdout.encode("utf-8")) <= MAX_RESPONSE_BYTES


def test_response_cap_returns_bounded_machine_error():
    data, exit_code = render_success_bytes({"blob": "x" * MAX_RESPONSE_BYTES})
    payload = json.loads(data)

    assert exit_code == ExitCode.UNAVAILABLE
    assert len(data) <= MAX_RESPONSE_BYTES
    assert payload["ok"] is False
    assert payload["error"]["code"] == "RESPONSE_TOO_LARGE"


def test_error_envelopes_are_also_hard_capped():
    error = ErrorDetail(
        code="REMOTE_ERROR",
        message="x" * MAX_RESPONSE_BYTES,
        retryable=True,
        suggested_action="retry",
    )

    data, exit_code = render_error_bytes(error, ExitCode.UNAVAILABLE)

    assert len(data) <= MAX_RESPONSE_BYTES
    assert exit_code == ExitCode.UNAVAILABLE
    assert json.loads(data)["error"]["code"] == "RESPONSE_TOO_LARGE"


def test_health_serialization_keeps_all_seven_fields_even_when_empty():
    health = HealthResult(
        controller_alive=False,
        backend_alive=False,
        kernel_connected=False,
        kernel_execution_ready=False,
        kernel_probe_at=None,
        kernel_probe_latency_ms=None,
        kernel_probe_error=None,
    )

    assert set(health.to_wire()) == {
        "controller_alive",
        "backend_alive",
        "kernel_connected",
        "kernel_execution_ready",
        "kernel_probe_at",
        "kernel_probe_latency_ms",
        "kernel_probe_error",
    }


def test_doctor_is_side_effect_free_and_schema_versioned(mocker, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    setup_logging = mocker.patch("colab_cli.cli.setup_logging")

    result = runner.invoke(app, ["doctor", "--format", "json"])
    payload = _json_output(result)

    assert result.exit_code == ExitCode.OK
    assert payload["ok"] is True
    assert payload["result"]["controller_alive"] is False
    assert payload["result"]["schema_versions"] == [1]
    setup_logging.assert_not_called()


def test_error_detail_requires_the_stable_recovery_contract():
    error = ErrorDetail(
        code="SOURCE_HASH_MISMATCH",
        message="Cell source changed",
        retryable=False,
        suggested_action="reinspect_cell",
    )

    assert error.to_wire() == {
        "code": "SOURCE_HASH_MISMATCH",
        "message": "Cell source changed",
        "retryable": False,
        "suggested_action": "reinspect_cell",
    }
