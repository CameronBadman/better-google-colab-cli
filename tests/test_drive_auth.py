import json
import queue
import threading
import time
from unittest.mock import MagicMock

import pytest
import requests

from colab_cli.drive_auth import (
    MAX_RESPONSE_BYTES,
    DriveAuthCoordinator,
    DriveAuthError,
)


def _response(data, status=200):
    response = MagicMock()
    response.status_code = status
    response.text = ")]}'\n" + json.dumps(data)
    response.content = response.text.encode()
    return response


def _request(uri=None, msg_id="msg-1"):
    data = {
        "msg_type": "colab_request",
        "metadata": {"colab_msg_id": msg_id},
        "header": {"msg_id": "header-1"},
        "content": {"request": {"authType": "dfs_ephemeral"}},
    }
    if uri:
        data["uri"] = uri
    return data


def _coordinator(responses, **kwargs):
    credentials = MagicMock()
    credentials.request.side_effect = responses
    history = MagicMock()
    emit = MagicMock()
    coordinator = DriveAuthCoordinator(
        credentials=credentials,
        colab_domain="https://colab.research.google.com",
        endpoint="endpoint-1",
        session_name="session-1",
        history=history,
        deadline=time.monotonic() + 2,
        emit=emit,
        consent_waiter=kwargs.get("consent_waiter", MagicMock()),
    )
    return coordinator, credentials, history, emit


def test_drive_coordinator_success_replies_once_with_bounded_requests():
    coordinator, credentials, history, _emit = _coordinator(
        [
            _response({"token": "secret-colab-token"}),
            _response({"success": True}),
            _response({"success": True}),
        ]
    )
    wsclient = MagicMock()
    message = _request()

    assert coordinator.intercept(message, wsclient) is True
    coordinator.wait()

    wsclient.stdin_channel.send.assert_called_once()
    reply = wsclient.stdin_channel.send.call_args.args[0]
    assert reply["content"]["value"] == {
        "type": "colab_reply",
        "colab_msg_id": "msg-1",
    }
    assert reply["parent_header"] == message["header"]
    assert credentials.request.call_count == 3
    for call in credentials.request.call_args_list:
        assert 0 < call.kwargs["timeout"] <= 30
    serialized_history = json.dumps(
        [call.args for call in history.log_event.call_args_list]
    )
    assert "secret-colab-token" not in serialized_history


def test_drive_coordinator_accepts_integer_message_id_and_preserves_its_type():
    coordinator, _credentials, _history, _emit = _coordinator(
        [
            _response({"token": "token"}),
            _response({"success": True}),
            _response({"success": True}),
        ]
    )
    wsclient = MagicMock()

    assert coordinator.intercept(_request(msg_id=17), wsclient) is True
    coordinator.wait()

    reply = wsclient.stdin_channel.send.call_args.args[0]
    assert reply["content"]["value"]["colab_msg_id"] == 17
    assert type(reply["content"]["value"]["colab_msg_id"]) is int


def test_drive_coordinator_does_not_persist_raw_message_id():
    message_id = "CANARY-colab-message-id"
    coordinator, _credentials, history, _emit = _coordinator(
        [
            _response({"token": "token"}),
            _response({"success": True}),
            _response({"success": True}),
        ]
    )

    coordinator.intercept(_request(msg_id=message_id), MagicMock())
    coordinator.wait()

    assert message_id not in json.dumps(
        [call.args for call in history.log_event.call_args_list]
    )


@pytest.mark.parametrize("message_id", [None, "", True, 1.5, [], {}])
def test_drive_coordinator_rejects_invalid_message_id_types(message_id):
    coordinator, *_ = _coordinator([])

    assert coordinator.intercept(_request(msg_id=message_id), MagicMock()) is False


def test_drive_coordinator_redacts_valid_authorization_uri():
    uri = "https://accounts.google.com/o/oauth2/v2/auth?state=secret-state"
    consent_waiter = MagicMock()
    coordinator, _credentials, history, emit = _coordinator(
        [
            _response({"token": "token"}),
            _response({"success": False, "unauthorized_redirect_uri": uri}),
            _response({"success": True}),
            _response({"success": True}),
        ],
        consent_waiter=consent_waiter,
    )
    wsclient = MagicMock()

    coordinator.intercept(_request(), wsclient)
    coordinator.wait()

    consent_waiter.assert_called_once()
    assert uri in "\n".join(str(call.args) for call in emit.call_args_list)
    history.log_event.assert_any_call(
        "session-1", "drive_auth_needed", {"uri": "<redacted>"}
    )
    assert uri not in json.dumps(
        [call.args for call in history.log_event.call_args_list]
    )
    wsclient.stdin_channel.send.assert_called_once()


def test_drive_coordinator_polls_until_consent_is_observed_before_propagating():
    uri = "https://accounts.google.com/o/oauth2/v2/auth?state=secret-state"
    consent_waiter = MagicMock()
    coordinator, credentials, _history, emit = _coordinator(
        [
            _response({"token": "token"}),
            _response({"success": False, "unauthorized_redirect_uri": uri}),
            _response({"success": False, "unauthorized_redirect_uri": uri}),
            _response({"success": True}),
            _response({"success": True}),
        ],
        consent_waiter=consent_waiter,
    )
    wsclient = MagicMock()

    coordinator.intercept(_request(), wsclient)
    coordinator.wait()

    assert consent_waiter.call_count == 2
    assert credentials.request.call_count == 5
    assert sum(uri in str(call.args) for call in emit.call_args_list) == 1
    dry_run_calls = credentials.request.call_args_list[1:4]
    assert all(call.kwargs["params"]["dryrun"] == "true" for call in dry_run_calls)
    assert credentials.request.call_args_list[4].kwargs["params"]["dryrun"] == "false"
    wsclient.stdin_channel.send.assert_called_once()


def test_drive_coordinator_rejects_malicious_redirect_but_unblocks_kernel():
    uri = "https://evil.example/oauth?token=secret"
    coordinator, _credentials, history, emit = _coordinator(
        [
            _response({"token": "token"}),
            _response({"success": False, "unauthorized_redirect_uri": uri}),
        ]
    )
    wsclient = MagicMock()

    coordinator.intercept(_request(), wsclient)
    with pytest.raises(DriveAuthError, match="invalid_authorization_redirect"):
        coordinator.wait()

    wsclient.stdin_channel.send.assert_called_once()
    assert uri not in "\n".join(str(call.args) for call in emit.call_args_list)
    assert uri not in json.dumps(
        [call.args for call in history.log_event.call_args_list]
    )


@pytest.mark.parametrize(
    "response,error_code",
    [
        (_response({"error": "nope"}, status=500), "http_error"),
        (_response({"token": ""}), "missing_token"),
    ],
)
def test_drive_coordinator_sanitizes_failure_and_replies(response, error_code):
    coordinator, _credentials, _history, _emit = _coordinator([response])
    wsclient = MagicMock()

    coordinator.intercept(_request(), wsclient)
    with pytest.raises(DriveAuthError, match=error_code) as raised:
        coordinator.wait()

    assert "nope" not in str(raised.value)
    wsclient.stdin_channel.send.assert_called_once()


def test_drive_coordinator_deduplicates_message_id():
    coordinator, _credentials, _history, _emit = _coordinator(
        [
            _response({"token": "token"}),
            _response({"success": True}),
            _response({"success": True}),
        ]
    )
    wsclient = MagicMock()
    message = _request()

    assert coordinator.intercept(message, wsclient) is True
    assert coordinator.intercept(message, wsclient) is True
    coordinator.wait()

    wsclient.stdin_channel.send.assert_called_once()


def test_non_drive_colab_request_is_not_intercepted():
    coordinator, *_ = _coordinator([])
    message = _request()
    message["content"]["request"]["authType"] = "something_else"

    assert coordinator.intercept(message, MagicMock()) is False


@pytest.mark.parametrize(
    "response,error_code",
    [
        (_response({"token": "token"}), None),
        (
            MagicMock(status_code=200, content=b"not-json", text="not-json"),
            "malformed_response",
        ),
        (
            MagicMock(
                status_code=200,
                content=b"x" * (MAX_RESPONSE_BYTES + 1),
                text="",
            ),
            "response_too_large",
        ),
        (
            MagicMock(status_code=200, content=b"\xff", text=""),
            "malformed_response",
        ),
    ],
)
def test_drive_response_validation(response, error_code):
    responses = [response]
    if error_code is None:
        responses.extend([_response({"success": True}), _response({"success": True})])
    coordinator, *_ = _coordinator(responses)
    wsclient = MagicMock()
    coordinator.intercept(_request(), wsclient)

    if error_code is None:
        coordinator.wait()
    else:
        with pytest.raises(DriveAuthError, match=error_code):
            coordinator.wait()
    wsclient.stdin_channel.send.assert_called_once()


def test_drive_request_timeout_is_sanitized_and_unblocks():
    coordinator, credentials, _history, _emit = _coordinator([])
    credentials.request.side_effect = requests.Timeout("CANARY-network-detail")
    wsclient = MagicMock()

    coordinator.intercept(_request(), wsclient)
    with pytest.raises(DriveAuthError, match="request_failed") as raised:
        coordinator.wait()

    assert "CANARY-network-detail" not in str(raised.value)
    wsclient.stdin_channel.send.assert_called_once()


def test_drive_reply_failure_is_surfaced():
    coordinator, *_ = _coordinator(
        [
            _response({"token": "token"}),
            _response({"success": True}),
            _response({"success": True}),
        ]
    )
    wsclient = MagicMock()
    wsclient.stdin_channel.send.side_effect = OSError("secret transport detail")

    coordinator.intercept(_request(), wsclient)
    with pytest.raises(DriveAuthError, match="reply_failed") as raised:
        coordinator.wait()

    assert "secret transport detail" not in str(raised.value)


def test_drive_consent_wait_is_cancellable_and_unblocks():
    entered = threading.Event()

    def waiter(_timeout, cancelled):
        entered.set()
        while not cancelled.wait(0.01):
            pass
        raise DriveAuthError("cancelled", phase="consent")

    uri = "https://accounts.google.com/o/oauth2/v2/auth?state=state"
    coordinator, *_ = _coordinator(
        [
            _response({"token": "token"}),
            _response({"success": False, "unauthorized_redirect_uri": uri}),
        ],
        consent_waiter=waiter,
    )
    wsclient = MagicMock()
    coordinator.intercept(_request(), wsclient)
    assert entered.wait(timeout=1)

    started = time.monotonic()
    coordinator.cancel()
    with pytest.raises(DriveAuthError, match="cancelled"):
        coordinator.wait()

    assert time.monotonic() - started < 0.5
    wsclient.stdin_channel.send.assert_called_once()


def test_drive_worker_does_not_strand_request_arriving_as_queue_drains(
    monkeypatch,
):
    coordinator, *_ = _coordinator([])
    monkeypatch.setattr(coordinator, "_propagate", MagicMock())
    original_get = coordinator._requests.get_nowait
    drain_started = threading.Event()
    release_drain = threading.Event()
    call_count = 0

    def controlled_get():
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            try:
                return original_get()
            except queue.Empty:
                drain_started.set()
                assert release_drain.wait(timeout=2)
                raise
        return original_get()

    monkeypatch.setattr(coordinator._requests, "get_nowait", controlled_get)
    first_client = MagicMock()
    second_client = MagicMock()
    coordinator.intercept(_request(msg_id="first"), first_client)
    assert drain_started.wait(timeout=2)

    second_intercept = threading.Thread(
        target=coordinator.intercept,
        args=(_request(msg_id="second"), second_client),
    )
    second_intercept.start()
    release_drain.set()
    second_intercept.join(timeout=2)
    coordinator.wait()

    assert not second_intercept.is_alive()
    first_client.stdin_channel.send.assert_called_once()
    second_client.stdin_channel.send.assert_called_once()
