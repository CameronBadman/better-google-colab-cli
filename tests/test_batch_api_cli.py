import asyncio
import json
import queue
import uuid

import nbformat
from typer.testing import CliRunner

from better_colab import BatchResult, BatchState, BatchWaitResult
from better_colab.cli import app
from better_colab.controller import ControllerServer
from better_colab.errors import ExitCode
from better_colab.kernel_transport import KernelEvent, PreparedExecution
from better_colab.models import ExecutionResult
from better_colab.protocol import INTERNAL_PROTOCOL_VERSION
from better_colab.storage import DurableStore, ProfileSpec, StatePaths


runner = CliRunner()
BATCH_ID = "10000000-0000-4000-8000-000000000901"
FIRST_ID = "00000000-0000-4000-8000-000000000911"
SECOND_ID = "00000000-0000-4000-8000-000000000912"


def _event(channel, message_type, parent, content):
    return KernelEvent(
        channel=channel,
        message={
            "header": {"msg_type": message_type, "msg_id": str(uuid.uuid4())},
            "parent_header": {"msg_id": parent},
            "content": content,
        },
    )


class _Transport:
    kernel_id = "kernel"
    jupyter_session_id = "jupyter"

    def __init__(self):
        self.events = queue.Queue()
        self.sent = []

    def prepare_execution(self, code):
        message_id = str(uuid.uuid4())
        return PreparedExecution(
            message_id=message_id,
            message={
                "header": {
                    "msg_id": message_id,
                    "msg_type": "execute_request",
                },
                "content": {"code": code},
            },
        )

    def send(self, prepared):
        source = prepared.message["content"]["code"]
        self.sent.append(source)
        reply = (
            {
                "status": "error",
                "ename": "ValueError",
                "evalue": "bad",
                "traceback": ["trace"],
            }
            if "raise" in source
            else {"status": "ok", "execution_count": len(self.sent)}
        )
        self.events.put(
            _event("shell", "execute_reply", prepared.message_id, reply)
        )
        self.events.put(
            _event(
                "iopub",
                "status",
                prepared.message_id,
                {"execution_state": "idle"},
            )
        )

    def next_event(self, timeout):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError

    def interrupt(self):
        pass

    def close(self):
        pass


def _profile_params(profile):
    return {
        "config_path": str(profile.config_path),
        "auth_provider": profile.auth_provider,
        "oauth_config_path": str(profile.oauth_config_path),
    }


def _request(method, params):
    return {
        "protocol_version": INTERNAL_PROTOCOL_VERSION,
        "request_id": str(uuid.uuid4()),
        "method": method,
        "params": params,
    }


def test_batch_controller_rpc_creates_one_child_per_cell_and_waits(tmp_path):
    async def scenario():
        paths = StatePaths(
            state_dir=tmp_path / "state",
            runtime_dir=tmp_path / "run",
        )
        profile = ProfileSpec.from_values(
            config_path=tmp_path / "sessions.json",
            auth_provider="oauth2",
            oauth_config_path=tmp_path / "oauth.json",
        )
        with DurableStore(paths=paths, profile=profile) as store:
            store.upsert_session(
                name="training",
                endpoint="endpoint",
                backend_url="https://runtime.example",
                runtime_token="secret",
                hardware="CPU",
            )
        transport = _Transport()
        server = ControllerServer(
            paths=paths,
            transport_factory=lambda _session: transport,
        )
        await server.start()
        try:
            started = await server._dispatch(
                _request(
                    "execution.batch.start",
                    {
                        "profile": _profile_params(profile),
                        "batch_id": BATCH_ID,
                        "session": "training",
                        "continue_on_error": False,
                        "members": [
                            {
                                "execution_id": FIRST_ID,
                                "source": "raise ValueError('bad')",
                                "provenance": {
                                    "kind": "notebook_cell",
                                    "path": "/tmp/job.ipynb",
                                    "notebook_id": "a" * 64,
                                    "cell_id": "first",
                                    "cell_index": 0,
                                },
                            },
                            {
                                "execution_id": SECOND_ID,
                                "source": "after = 2",
                                "provenance": {
                                    "kind": "notebook_cell",
                                    "path": "/tmp/job.ipynb",
                                    "notebook_id": "a" * 64,
                                    "cell_id": "second",
                                    "cell_index": 1,
                                },
                            },
                        ],
                    },
                )
            )
            waited = await server._dispatch(
                _request(
                    "execution.batch.wait",
                    {
                        "profile": _profile_params(profile),
                        "batch_id": BATCH_ID,
                        "timeout": 2,
                    },
                )
            )
            status = await server._dispatch(
                _request(
                    "execution.batch.status",
                    {
                        "profile": _profile_params(profile),
                        "batch_id": BATCH_ID,
                    },
                )
            )
            after_id = str(uuid.uuid4())
            after = await server._dispatch(
                _request(
                    "execution.start",
                    {
                        "profile": _profile_params(profile),
                        "execution_id": after_id,
                        "session": "training",
                        "source": "after_batch = True",
                        "provenance": {"kind": "stdin"},
                        "idempotency_key": None,
                        "execution_timeout": None,
                    },
                )
            )
            after_wait = await server._dispatch(
                _request(
                    "execution.wait",
                    {
                        "profile": _profile_params(profile),
                        "execution_id": after_id,
                        "timeout": 2,
                        "cursor": None,
                        "max_bytes": 65536,
                    },
                )
            )
        finally:
            await server.close()

        assert started["state"] == "queued"
        assert [item["execution_id"] for item in started["executions"]] == [
            FIRST_ID,
            SECOND_ID,
        ]
        assert waited["state"] == "error"
        assert waited["wait_timed_out"] is False
        assert [item["state"] for item in status["executions"]] == [
            "error",
            "interrupted",
        ]
        assert transport.sent == [
            "raise ValueError('bad')",
            "after_batch = True",
        ]
        assert after["state"] == "queued"
        assert after_wait["state"] == "finished"

    asyncio.run(scenario())


def _execution(execution_id, state="queued"):
    return ExecutionResult(
        execution_id=execution_id,
        session="training",
        state=state,
        source_sha256="a" * 64,
        output_complete=True,
        created_at="2026-07-17T00:00:00Z",
        updated_at="2026-07-17T00:00:00Z",
    )


def _batch(state=BatchState.QUEUED, *, waited=False):
    model = BatchWaitResult if waited else BatchResult
    values = {
        "batch_id": BATCH_ID,
        "session": "training",
        "state": state,
        "continue_on_error": False,
        "executions": [_execution(FIRST_ID), _execution(SECOND_ID)],
        "created_at": "2026-07-17T00:00:00Z",
        "updated_at": "2026-07-17T00:00:00Z",
    }
    if waited:
        values["wait_timed_out"] = False
    return model(**values)


def test_batch_start_cli_selects_cells_and_emits_parent_and_children(
    mocker,
    tmp_path,
):
    path = tmp_path / "batch.ipynb"
    nbformat.write(
        nbformat.v4.new_notebook(
            cells=[
                nbformat.v4.new_code_cell("first = 1", id="first"),
                nbformat.v4.new_code_cell("second = 2", id="second"),
            ]
        ),
        path,
    )
    start = mocker.patch(
        "better_colab.durable_commands.BetterColabClient.start_batch",
        return_value=_batch(),
    )

    result = runner.invoke(
        app,
        [
            "execution",
            "batch",
            "start",
            "--session",
            "training",
            "--notebook",
            str(path),
            "--cell-id",
            "second",
            "--cell-id",
            "first",
            "--detach",
            "--format=json",
        ],
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == ExitCode.OK
    assert payload["result"]["batch_id"] == BATCH_ID
    assert len(payload["result"]["executions"]) == 2
    assert start.call_args.kwargs["cell_ids"] == ["second", "first"]
    assert start.call_args.kwargs["detach"] is True


def test_batch_status_wait_and_cancel_cli_exit_contract(mocker):
    mocker.patch(
        "better_colab.durable_commands.BetterColabClient.batch_status",
        return_value=_batch(BatchState.ERROR),
    )
    mocker.patch(
        "better_colab.durable_commands.BetterColabClient.wait_batch",
        return_value=_batch(BatchState.ERROR, waited=True),
    )
    mocker.patch(
        "better_colab.durable_commands.BetterColabClient.cancel_batch",
        return_value=_batch(BatchState.INTERRUPTED),
    )

    status = runner.invoke(
        app,
        ["execution", "batch", "status", BATCH_ID, "--format=json"],
    )
    waited = runner.invoke(
        app,
        ["execution", "batch", "wait", BATCH_ID, "--format=json"],
    )
    cancelled = runner.invoke(
        app,
        ["execution", "batch", "cancel", BATCH_ID, "--format=json"],
    )

    assert status.exit_code == ExitCode.OK
    assert waited.exit_code == ExitCode.EXECUTION_FAILED
    assert cancelled.exit_code == ExitCode.OK


def test_batch_wait_timeout_is_observational_exit_124(mocker):
    timed_out = _batch(BatchState.RUNNING, waited=True).model_copy(
        update={"wait_timed_out": True}
    )
    mocker.patch(
        "better_colab.durable_commands.BetterColabClient.wait_batch",
        return_value=timed_out,
    )
    cancel = mocker.patch(
        "better_colab.durable_commands.BetterColabClient.cancel_batch"
    )

    result = runner.invoke(
        app,
        [
            "execution",
            "batch",
            "wait",
            BATCH_ID,
            "--timeout",
            "0.01",
            "--format=json",
        ],
    )

    assert result.exit_code == ExitCode.WAIT_TIMEOUT
    assert json.loads(result.stdout)["result"]["wait_timed_out"] is True
    cancel.assert_not_called()
