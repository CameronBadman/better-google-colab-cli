
import pytest

from better_colab import BetterColabClient, BetterColabError, ExecutionState
from better_colab.models import (
    ExecutionListResult,
    ExecutionResult,
    ExecutionWaitResult,
)
from better_colab.storage import ProfileSpec, StatePaths


@pytest.fixture
def client(tmp_path):
    return BetterColabClient(
        config_path=tmp_path / "sessions.json",
        auth_provider="oauth2",
        oauth_config_path=tmp_path / "oauth.json",
        paths=StatePaths(
            state_dir=tmp_path / "state",
            runtime_dir=tmp_path / "run",
        ),
    )


def _execution_wire(state="queued"):
    return {
        "execution_id": "00000000-0000-4000-8000-000000000301",
        "session": "training",
        "state": state,
        "source_sha256": "a" * 64,
        "output_complete": True,
        "dispatch_confirmed": state != "queued",
        "reply_received": state in {"finished", "error"},
        "idle_received": state in {"finished", "error"},
        "created_at": "2026-07-17T00:00:00Z",
        "updated_at": "2026-07-17T00:00:01Z",
    }


def test_typed_start_generates_public_uuid_before_dispatch(mocker, client):
    controller = mocker.patch(
        "better_colab.client.ControllerClient",
    ).return_value
    controller.start_execution.return_value = _execution_wire()

    result = client.start_execution(
        session="training",
        source="print('hello')",
        provenance={"kind": "stdin"},
        detach=True,
        idempotency_key="retry-key",
    )

    assert isinstance(result, ExecutionResult)
    assert result.state is ExecutionState.QUEUED
    kwargs = controller.start_execution.call_args.kwargs
    assert kwargs["profile"] == client.profile
    assert kwargs["execution_id"]
    assert kwargs["session"] == "training"
    assert kwargs["source"] == "print('hello')"
    assert kwargs["idempotency_key"] == "retry-key"


def test_typed_start_hash_guard_fails_before_controller_dispatch(mocker, client):
    controller = mocker.patch(
        "better_colab.client.ControllerClient",
    ).return_value

    with pytest.raises(BetterColabError) as mismatch:
        client.start_execution(
            session="training",
            source="print('hello')",
            provenance={"kind": "stdin"},
            expected_source_sha256="0" * 64,
            detach=True,
        )

    assert mismatch.value.error.code == "SOURCE_HASH_MISMATCH"
    assert mismatch.value.exit_code == 5
    controller.start_execution.assert_not_called()


def test_typed_attached_start_waits_indefinitely_by_default(mocker, client):
    controller = mocker.patch(
        "better_colab.client.ControllerClient",
    ).return_value
    controller.start_execution.return_value = _execution_wire()
    controller.wait_execution.return_value = {
        **_execution_wire("finished"),
        "wait_timed_out": False,
        "output": {
            "execution_id": "00000000-0000-4000-8000-000000000301",
            "events": [],
            "has_more": False,
            "output_complete": True,
        },
    }

    result = client.start_execution(
        session="training",
        source="pass",
        provenance={"kind": "stdin"},
    )

    assert isinstance(result, ExecutionWaitResult)
    assert result.state is ExecutionState.FINISHED
    assert controller.wait_execution.call_args.kwargs["timeout"] is None


def test_status_list_wait_output_and_cancel_are_typed(mocker, client):
    controller = mocker.patch(
        "better_colab.client.ControllerClient",
    ).return_value
    controller.execution_status.return_value = _execution_wire("error")
    controller.wait_execution.return_value = {
        **_execution_wire("error"),
        "wait_timed_out": False,
        "output": {
            "execution_id": "00000000-0000-4000-8000-000000000301",
            "events": [],
            "has_more": False,
            "output_complete": True,
        },
    }
    controller.list_executions.return_value = {
        "executions": [_execution_wire("error")],
    }
    controller.cancel_execution.return_value = _execution_wire("interrupted")
    controller.execution_output.return_value = {
        "execution_id": "00000000-0000-4000-8000-000000000301",
        "events": [],
        "has_more": False,
        "output_complete": True,
    }

    status = client.execution_status(
        "00000000-0000-4000-8000-000000000301"
    )
    waited = client.wait_execution(
        "00000000-0000-4000-8000-000000000301",
        timeout=0.1,
    )
    listed = client.list_executions()
    cancelled = client.cancel_execution(
        "00000000-0000-4000-8000-000000000301"
    )
    output = client.execution_output(
        "00000000-0000-4000-8000-000000000301"
    )

    assert status.state is ExecutionState.ERROR
    assert isinstance(waited, ExecutionWaitResult)
    assert isinstance(listed, ExecutionListResult)
    assert cancelled.state is ExecutionState.INTERRUPTED
    assert output.events == []


def test_controller_profile_parameters_never_include_runtime_secrets(
    mocker, client
):
    controller = mocker.patch(
        "better_colab.client.ControllerClient",
    ).return_value
    controller.execution_status.return_value = _execution_wire()

    client.execution_status("00000000-0000-4000-8000-000000000301")

    profile = controller.execution_status.call_args.kwargs["profile"]
    assert isinstance(profile, ProfileSpec)
    assert "token" not in repr(profile)
