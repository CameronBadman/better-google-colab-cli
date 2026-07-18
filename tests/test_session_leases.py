import asyncio
import queue
import uuid

import pytest

from better_colab import BetterColabClient, BetterColabError
from better_colab.controller import ControllerServer
from better_colab.errors import ExitCode
from better_colab.kernel_transport import KernelEvent, PreparedExecution
from better_colab.protocol import INTERNAL_PROTOCOL_VERSION
from better_colab.storage import DurableStore, ProfileSpec, StatePaths


def _request(method, params):
    return {
        "protocol_version": INTERNAL_PROTOCOL_VERSION,
        "request_id": str(uuid.uuid4()),
        "method": method,
        "params": params,
    }


def _profile_params(profile):
    return {
        "profile": {
            "config_path": str(profile.config_path),
            "auth_provider": profile.auth_provider,
            "oauth_config_path": str(profile.oauth_config_path),
        }
    }


class _LeaseTransport:
    kernel_id = "kernel"
    jupyter_session_id = "jupyter"

    def __init__(self):
        self.events = queue.Queue()
        self.closed = False

    def prepare_readiness_probe(self, nonce):
        message_id = str(uuid.uuid4())
        self.events.put(
            KernelEvent(
                channel="shell",
                message={
                    "header": {"msg_type": "execute_reply"},
                    "parent_header": {"msg_id": message_id},
                    "content": {
                        "status": "ok",
                        "user_expressions": {
                            "better_colab_nonce": {
                                "status": "ok",
                                "data": {"text/plain": repr(nonce)},
                            }
                        },
                    },
                },
            )
        )
        self.events.put(
            KernelEvent(
                channel="iopub",
                message={
                    "header": {"msg_type": "status"},
                    "parent_header": {"msg_id": message_id},
                    "content": {"execution_state": "idle"},
                },
            )
        )
        return PreparedExecution(
            message_id=message_id,
            message={"header": {"msg_id": message_id}},
        )

    def send(self, _prepared):
        pass

    def next_event(self, timeout):
        return self.events.get(timeout=timeout)

    def close(self):
        self.closed = True


def test_controller_lease_excludes_durable_dispatch_until_release(
    mocker,
    tmp_path,
):
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
        server = ControllerServer(paths=paths)
        await server.start()
        try:
            params = {**_profile_params(profile), "name": "training"}
            acquired = await server._dispatch(
                _request("session.lease.acquire", params)
            )
            assert acquired["lease_id"]

            with pytest.raises(BetterColabError) as leased:
                await server._dispatch(
                    _request(
                        "execution.start",
                        {
                            **_profile_params(profile),
                            "execution_id": str(uuid.uuid4()),
                            "session": "training",
                            "source": "x = 1",
                            "provenance": {"kind": "stdin"},
                        },
                    )
                )
            assert leased.value.error.code == "SESSION_LEASED"
            assert leased.value.exit_code is ExitCode.CONFLICT

            released = await server._dispatch(
                _request(
                    "session.lease.release",
                    {
                        **params,
                        "lease_id": acquired["lease_id"],
                        "reconnect": False,
                    },
                )
            )
            assert released == {"released": True, "reconnected": False}

            mocker.patch.object(server._coordinator, "submit")
            started = await server._dispatch(
                _request(
                    "execution.start",
                    {
                        **_profile_params(profile),
                        "execution_id": str(uuid.uuid4()),
                        "session": "training",
                        "source": "x = 2",
                        "provenance": {"kind": "stdin"},
                    },
                )
            )
            assert started["state"] == "queued"
        finally:
            await server.close()

    asyncio.run(scenario())


def test_controller_refuses_lease_while_durable_work_is_active(tmp_path):
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
            store.create_execution(
                execution_id=str(uuid.uuid4()),
                session_name="training",
                source=b"x = 1",
                request={"session": "training"},
                provenance={"kind": "stdin"},
            )
        server = ControllerServer(paths=paths)
        await server.start()
        try:
            with pytest.raises(BetterColabError) as busy:
                await server._dispatch(
                    _request(
                        "session.lease.acquire",
                        {**_profile_params(profile), "name": "training"},
                    )
                )
            assert busy.value.error.code == "SESSION_BUSY"
        finally:
            await server.close()

    asyncio.run(scenario())


def test_lease_closes_transport_and_release_reconnects_with_nonce(tmp_path):
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
        transports = []

        def factory(_session):
            transport = _LeaseTransport()
            transports.append(transport)
            return transport

        server = ControllerServer(paths=paths, transport_factory=factory)
        await server.start()
        try:
            params = {**_profile_params(profile), "name": "training"}
            ready = await server._dispatch(
                _request("session.probe", {**params, "timeout": 1})
            )
            assert ready["kernel_execution_ready"] is True

            acquired = await server._dispatch(
                _request("session.lease.acquire", params)
            )
            assert transports[0].closed is True

            released = await server._dispatch(
                _request(
                    "session.lease.release",
                    {
                        **params,
                        "lease_id": acquired["lease_id"],
                        "reconnect": True,
                    },
                )
            )
            assert released == {"released": True, "reconnected": True}
            assert len(transports) == 2
        finally:
            await server.close()

    asyncio.run(scenario())


def test_typed_session_lease_always_releases(mocker, tmp_path):
    client = BetterColabClient(
        config_path=tmp_path / "sessions.json",
        paths=StatePaths(
            state_dir=tmp_path / "state",
            runtime_dir=tmp_path / "run",
        ),
    )
    controller = mocker.patch(
        "better_colab.client.ControllerClient"
    ).return_value
    controller.acquire_session_lease.return_value = {"lease_id": "lease"}

    with pytest.raises(RuntimeError):
        with client.session_lease("training", reconnect=True):
            raise RuntimeError("interactive command failed")

    controller.release_session_lease.assert_called_once_with(
        profile=client.profile,
        name="training",
        lease_id="lease",
        reconnect=True,
    )
