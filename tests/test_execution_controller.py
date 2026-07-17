import asyncio
import queue
import uuid

import pytest

from better_colab import BetterColabError
from better_colab.controller import ControllerServer
from better_colab.kernel_transport import KernelEvent, PreparedExecution
from better_colab.protocol import INTERNAL_PROTOCOL_VERSION
from better_colab.storage import DurableStore, ProfileSpec, StatePaths


class _ImmediateTransport:
    kernel_id = "kernel-live"
    jupyter_session_id = "jupyter-live"

    def __init__(self):
        self.events = queue.Queue()
        self.counter = 0
        self.send_count = 0

    def prepare_execution(self, code):
        self.counter += 1
        message_id = f"message-{self.counter}"
        return PreparedExecution(
            message_id=message_id,
            message={
                "header": {
                    "msg_id": message_id,
                    "msg_type": "execute_request",
                },
                "msg_id": message_id,
                "msg_type": "execute_request",
                "parent_header": {},
                "metadata": {},
                "content": {"code": code},
            },
        )

    def send(self, prepared):
        self.send_count += 1
        message_id = prepared.message_id
        code = prepared.message["content"]["code"]
        if "raise" in code:
            self.events.put(
                _event(
                    "iopub",
                    "stream",
                    message_id,
                    {"name": "stdout", "text": "before\n"},
                )
            )
            reply = {
                "status": "error",
                "ename": "ValueError",
                "evalue": "bad",
                "traceback": ["trace"],
            }
        else:
            reply = {"status": "ok", "execution_count": 1}
        self.events.put(_event("shell", "execute_reply", message_id, reply))
        self.events.put(
            _event(
                "iopub",
                "status",
                message_id,
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


def _event(channel, message_type, parent, content):
    return KernelEvent(
        channel=channel,
        message={
            "header": {"msg_type": message_type, "msg_id": str(uuid.uuid4())},
            "parent_header": {"msg_id": parent},
            "content": content,
        },
    )


def _params(profile, **values):
    return {
        "profile": {
            "config_path": str(profile.config_path),
            "auth_provider": profile.auth_provider,
            "oauth_config_path": str(profile.oauth_config_path),
        },
        **values,
    }


def _request(method, params):
    return {
        "protocol_version": INTERNAL_PROTOCOL_VERSION,
        "request_id": str(uuid.uuid4()),
        "method": method,
        "params": params,
    }


def test_controller_execution_rpc_idempotency_wait_and_error_proof(tmp_path):
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
        transport = _ImmediateTransport()
        server = ControllerServer(
            paths=paths,
            transport_factory=lambda _session: transport,
        )
        await server.start()
        try:
            first_id = str(uuid.uuid4())
            start_params = _params(
                profile,
                execution_id=first_id,
                session="training",
                source="pass",
                provenance={"kind": "stdin"},
                idempotency_key="same-request",
                execution_timeout=None,
            )
            started = await server._dispatch(
                _request("execution.start", start_params)
            )
            waited = await server._dispatch(
                _request(
                    "execution.wait",
                    _params(
                        profile,
                        execution_id=first_id,
                        timeout=2,
                        cursor=None,
                        max_bytes=65536,
                    ),
                )
            )
            repeated = await server._dispatch(
                _request(
                    "execution.start",
                    {
                        **start_params,
                        "execution_id": str(uuid.uuid4()),
                    },
                )
            )

            assert started["state"] == "queued"
            assert waited["state"] == "finished"
            assert waited["dispatch_confirmed"] is True
            assert waited["reply_received"] is True
            assert waited["idle_received"] is True
            assert waited["output"]["events"] == []
            assert repeated["execution_id"] == first_id
            assert transport.send_count == 1

            with pytest.raises(BetterColabError) as conflict:
                await server._dispatch(
                    _request(
                        "execution.start",
                        {
                            **start_params,
                            "execution_id": str(uuid.uuid4()),
                            "source": "print('different')",
                        },
                    )
                )
            assert conflict.value.error.code == "IDEMPOTENCY_CONFLICT"

            error_id = str(uuid.uuid4())
            await server._dispatch(
                _request(
                    "execution.start",
                    _params(
                        profile,
                        execution_id=error_id,
                        session="training",
                        source="print('before'); raise ValueError('bad')",
                        provenance={"kind": "stdin"},
                        idempotency_key=None,
                        execution_timeout=None,
                    ),
                )
            )
            failed = await server._dispatch(
                _request(
                    "execution.wait",
                    _params(
                        profile,
                        execution_id=error_id,
                        timeout=2,
                        cursor=None,
                        max_bytes=65536,
                    ),
                )
            )
            status = await server._dispatch(
                _request(
                    "execution.status",
                    _params(
                        profile,
                        execution_id=error_id,
                        include=["transitions", "traceback"],
                    ),
                )
            )
            listed = await server._dispatch(
                _request(
                    "execution.list",
                    _params(
                        profile,
                        session="training",
                        cursor=None,
                        limit=20,
                    ),
                )
            )

            assert failed["state"] == "error"
            assert failed["error_name"] == "ValueError"
            assert failed["output"]["events"][0]["text"] == "before\n"
            assert status["traceback"] == ["trace"]
            assert status["transitions"][-1]["to_state"] == "error"
            assert [item["execution_id"] for item in listed["executions"]] == [
                error_id,
                first_id,
            ]
        finally:
            await server.close()

    asyncio.run(scenario())


def test_controller_wait_timeout_does_not_mutate_or_cancel(tmp_path):
    class _Blocking(_ImmediateTransport):
        def send(self, prepared):
            self.send_count += 1

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
        server = ControllerServer(
            paths=paths,
            transport_factory=lambda _session: _Blocking(),
        )
        await server.start()
        execution_id = str(uuid.uuid4())
        try:
            await server._dispatch(
                _request(
                    "execution.start",
                    _params(
                        profile,
                        execution_id=execution_id,
                        session="training",
                        source="pass",
                        provenance={"kind": "stdin"},
                        idempotency_key=None,
                        execution_timeout=None,
                    ),
                )
            )
            observed = await server._dispatch(
                _request(
                    "execution.wait",
                    _params(
                        profile,
                        execution_id=execution_id,
                        timeout=0.01,
                        cursor=None,
                        max_bytes=65536,
                    ),
                )
            )
            with DurableStore(paths=paths, profile=profile) as store:
                record = store.get_execution(execution_id)

            assert observed["wait_timed_out"] is True
            assert observed["state"] in {"queued", "dispatching"}
            assert record.cancel_requested is False
            assert record.interrupt_requested_state is None
        finally:
            # Force semantics are tested through the process-level suite.
            server._force_active_uncertain(reason="test_cleanup")
            await server.close()

    asyncio.run(scenario())
