import json
import io
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
    _wait_for_continue,
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
        deadline=kwargs.get("deadline", time.monotonic() + 2),
        emit=emit,
        consent_waiter=kwargs.get("consent_waiter", MagicMock()),
    )
    return coordinator, credentials, history, emit


def _authorized_responses(token_1="token-1", token_2="token-2"):
    return [
        _response({"token": token_1}),
        _response({"success": True}),
        _response({"token": token_2}),
        _response({"success": True}),
    ]


def test_drive_coordinator_uses_exact_bodyless_sequence_and_fresh_tokens():
    coordinator, credentials, history, _emit = _coordinator(
        _authorized_responses(
            "CANARY-secret-colab-token-one",
            "CANARY-secret-colab-token-two",
        )
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
    assert credentials.request.call_count == 4
    calls = credentials.request.call_args_list
    assert [call.args[0] for call in calls] == ["GET", "POST", "GET", "POST"]
    assert [call.kwargs["params"]["dryrun"] for call in calls] == [
        "true",
        "true",
        "false",
        "false",
    ]
    assert "X-Goog-Colab-Token" not in calls[0].kwargs["headers"]
    assert calls[1].kwargs["headers"]["X-Goog-Colab-Token"] == (
        "CANARY-secret-colab-token-one"
    )
    assert "X-Goog-Colab-Token" not in calls[2].kwargs["headers"]
    assert calls[3].kwargs["headers"]["X-Goog-Colab-Token"] == (
        "CANARY-secret-colab-token-two"
    )
    for call in calls:
        assert 0 < call.kwargs["timeout"] <= 30
        assert call.kwargs["headers"]["Accept"] == "application/json"
        assert call.kwargs["headers"]["X-Colab-Client-Agent"] == "colab-cli"
        assert "X-Colab-VS-Code-App-Name" not in call.kwargs["headers"]
        assert "X-Colab-VS-Code-Extension-Version" not in call.kwargs["headers"]
    for call in (calls[1], calls[3]):
        assert not ({"data", "files", "json"} & call.kwargs.keys())
    serialized_history = json.dumps(
        [call.args for call in history.log_event.call_args_list]
    )
    assert "CANARY-secret-colab-token" not in serialized_history

    http_events = [
        call.args[2]
        for call in history.log_event.call_args_list
        if call.args[1] == "drive_auth_http"
    ]
    assert [(event["phase"], event["method"]) for event in http_events] == [
        ("dry_run_token", "GET"),
        ("dry_run", "POST"),
        ("propagate_token", "GET"),
        ("propagate", "POST"),
    ]
    assert all(
        set(event)
        == {"phase", "method", "status", "byte_count", "success", "has_redirect"}
        for event in http_events
    )


def test_drive_coordinator_accepts_integer_message_id_and_preserves_its_type():
    coordinator, _credentials, _history, _emit = _coordinator(_authorized_responses())
    wsclient = MagicMock()

    assert coordinator.intercept(_request(msg_id=17), wsclient) is True
    coordinator.wait()

    reply = wsclient.stdin_channel.send.call_args.args[0]
    assert reply["content"]["value"]["colab_msg_id"] == 17
    assert type(reply["content"]["value"]["colab_msg_id"]) is int


def test_drive_coordinator_does_not_persist_raw_message_id():
    message_id = "CANARY-colab-message-id"
    coordinator, _credentials, history, _emit = _coordinator(_authorized_responses())

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
            _response({"token": "dry-token"}),
            _response({"success": False, "unauthorized_redirect_uri": uri}),
            _response({"token": "final-token"}),
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


def test_drive_coordinator_waits_for_one_continue_then_propagates_once():
    uri = "https://accounts.google.com/o/oauth2/v2/auth?state=secret-state"
    consent_waiter = MagicMock()
    coordinator, credentials, _history, emit = _coordinator(
        [
            _response({"token": "dry-token"}),
            _response({"success": False, "unauthorized_redirect_uri": uri}),
            _response({"token": "final-token"}),
            _response({"success": True}),
        ],
        consent_waiter=consent_waiter,
    )
    wsclient = MagicMock()

    coordinator.intercept(_request(), wsclient)
    coordinator.wait()

    consent_waiter.assert_called_once()
    assert credentials.request.call_count == 4
    assert sum(uri in str(call.args) for call in emit.call_args_list) == 1
    assert [
        call.kwargs["params"]["dryrun"]
        for call in credentials.request.call_args_list
    ] == ["true", "true", "false", "false"]
    wsclient.stdin_channel.send.assert_called_once()


def test_drive_coordinator_already_authorized_does_not_request_continue():
    consent_waiter = MagicMock()
    coordinator, credentials, _history, emit = _coordinator(
        _authorized_responses(), consent_waiter=consent_waiter
    )

    coordinator.intercept(_request(), MagicMock())
    coordinator.wait()

    consent_waiter.assert_not_called()
    assert credentials.request.call_count == 4
    assert not any("Visit:" in str(call.args) for call in emit.call_args_list)


def test_final_propagation_response_is_authoritative_and_sanitized():
    canary = "CANARY-final-response-body"
    coordinator, _credentials, history, _emit = _coordinator(
        [
            _response({"token": "dry-token"}),
            _response({"success": True}),
            _response({"token": "final-token"}),
            _response({"success": False, "detail": canary}),
        ]
    )
    wsclient = MagicMock()

    coordinator.intercept(_request(), wsclient)
    with pytest.raises(DriveAuthError, match="propagation_rejected") as raised:
        coordinator.wait()

    assert canary not in str(raised.value)
    assert canary not in json.dumps(
        [call.args for call in history.log_event.call_args_list]
    )
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
    coordinator, _credentials, _history, _emit = _coordinator(_authorized_responses())
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
        responses.extend(
            [
                _response({"success": True}),
                _response({"token": "fresh-token"}),
                _response({"success": True}),
            ]
        )
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
    coordinator, *_ = _coordinator(_authorized_responses())
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


def test_drive_consent_wait_reports_no_tty_without_leaking_os_error():
    def unavailable_tty(*_args, **_kwargs):
        raise OSError("CANARY-device-detail")

    with pytest.raises(DriveAuthError, match="tty_unavailable") as raised:
        _wait_for_continue(1, threading.Event(), tty_opener=unavailable_tty)

    assert "CANARY-device-detail" not in str(raised.value)


def test_drive_consent_wait_reports_tty_eof():
    tty = io.StringIO("")

    with pytest.raises(DriveAuthError, match="tty_eof"):
        _wait_for_continue(
            1,
            threading.Event(),
            tty_opener=lambda *_args, **_kwargs: tty,
            selector=lambda *_args, **_kwargs: ([tty], [], []),
        )


def test_drive_coordinator_enforces_deadline_before_any_http_request():
    coordinator, credentials, _history, _emit = _coordinator(
        [], deadline=time.monotonic() - 1
    )
    wsclient = MagicMock()

    coordinator.intercept(_request(), wsclient)
    with pytest.raises(DriveAuthError, match="deadline_exceeded"):
        coordinator.wait()

    credentials.request.assert_not_called()
    wsclient.stdin_channel.send.assert_called_once()


def test_drive_secret_canaries_are_absent_from_history_and_failures():
    token_canary = "CANARY-xsrf-secret"
    body_canary = "CANARY-body-secret"
    cookie_canary = "CANARY-cookie-secret"
    response = _response({"error": body_canary}, status=400)
    response.headers = {"Set-Cookie": cookie_canary}
    response.cookies = {"session": cookie_canary}
    coordinator, _credentials, history, _emit = _coordinator([response])
    wsclient = MagicMock()

    coordinator.intercept(_request(msg_id=token_canary), wsclient)
    with pytest.raises(DriveAuthError) as raised:
        coordinator.wait()

    persisted = json.dumps([call.args for call in history.log_event.call_args_list])
    for canary in (token_canary, body_canary, cookie_canary):
        assert canary not in persisted
        assert canary not in str(raised.value)


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
