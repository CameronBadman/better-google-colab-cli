"""Bounded handling of Colab's DriveFS credential-propagation extension."""

from __future__ import annotations

import json
import queue
import select
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit


MAX_RESPONSE_BYTES = 1024 * 1024
HTTP_TIMEOUT_SEC = 30.0
REDACTED = "<redacted>"
ACCEPT_JSON = "application/json"
CLIENT_AGENT = "colab-cli"
XSRF_HEADER = "X-Goog-Colab-Token"


class DriveAuthError(RuntimeError):
    def __init__(
        self, code: str, *, phase: str | None = None, status: int | None = None
    ):
        details = [code]
        if phase:
            details.append(f"phase={phase}")
        if status is not None:
            details.append(f"status={status}")
        super().__init__("Drive authorization failed: " + " ".join(details))
        self.code = code
        self.phase = phase
        self.status = status


@dataclass(frozen=True)
class _DriveRequest:
    message: dict[str, Any]
    wsclient: Any
    message_id: str | int


def _wait_for_continue(
    timeout: float,
    cancelled: threading.Event,
    *,
    tty_opener: Callable[..., Any] = open,
    selector: Callable[..., tuple[list[Any], list[Any], list[Any]]] = select.select,
) -> None:
    """Wait for one terminal line without consuming kernel stdin.

    The browser's close-window page has no signaling path back to the CLI.  A
    line read from ``/dev/tty`` is therefore only permission to attempt final
    propagation; it is never treated as evidence that Google granted access.
    """

    if timeout <= 0:
        raise DriveAuthError("deadline_exceeded", phase="consent")
    try:
        tty = tty_opener("/dev/tty", "r", encoding="utf-8", errors="strict")
    except (OSError, ValueError) as error:
        raise DriveAuthError("tty_unavailable", phase="consent") from error

    expires_at = time.monotonic() + timeout
    try:
        while True:
            if cancelled.is_set():
                raise DriveAuthError("cancelled", phase="consent")
            remaining = expires_at - time.monotonic()
            if remaining <= 0:
                raise DriveAuthError("deadline_exceeded", phase="consent")
            try:
                readable, _, _ = selector(
                    [tty], [], [], min(0.1, remaining)
                )
            except InterruptedError:
                continue
            except (OSError, ValueError) as error:
                raise DriveAuthError("tty_unavailable", phase="consent") from error
            if not readable:
                continue
            try:
                line = tty.readline()
            except (OSError, UnicodeError) as error:
                raise DriveAuthError("tty_read_failed", phase="consent") from error
            if line == "":
                raise DriveAuthError("tty_eof", phase="consent")
            return
    finally:
        try:
            tty.close()
        except OSError:
            pass


class DriveAuthCoordinator:
    def __init__(
        self,
        *,
        credentials: Any,
        colab_domain: str,
        endpoint: str,
        session_name: str,
        history: Any,
        deadline: float,
        emit: Callable[[str], None],
        consent_waiter: Callable[
            [float, threading.Event], None
        ] = _wait_for_continue,
    ):
        self.credentials = credentials
        self.url = f"{colab_domain}/tun/m/credentials-propagation/{endpoint}"
        self.session_name = session_name
        self.history = history
        self.deadline = deadline
        self.emit = emit
        self.consent_waiter = consent_waiter
        self.cancelled = threading.Event()
        self._requests: queue.Queue[_DriveRequest] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._seen: set[str | int] = set()
        self._replied: set[str | int] = set()
        self._error: DriveAuthError | None = None

    def intercept(self, message: dict[str, Any], wsclient: Any) -> bool:
        request = message.get("content", {}).get("request", {})
        if request.get("authType") != "dfs_ephemeral":
            return False
        message_id = message.get("metadata", {}).get("colab_msg_id")
        valid_string_id = isinstance(message_id, str) and bool(message_id)
        valid_integer_id = type(message_id) is int
        if not (valid_string_id or valid_integer_id):
            return False

        with self._lock:
            if message_id in self._seen:
                return True
            self._seen.add(message_id)
            self._requests.put(_DriveRequest(message, wsclient, message_id))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run,
                    name="colab-drive-auth",
                    daemon=True,
                )
                self._thread.start()
        self._log(
            "colab_request",
            {
                "type": "dfs_ephemeral",
                "colab_msg_id_type": type(message_id).__name__,
            },
        )
        return True

    def cancel(self) -> None:
        self.cancelled.set()

    def wait(self) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0, self.deadline - time.monotonic()))
            if thread.is_alive():
                self.cancel()
                thread.join(timeout=0.5)
                if thread.is_alive():
                    raise DriveAuthError("deadline_exceeded", phase="coordinator")
        if self._error is not None:
            raise self._error

    def _run(self) -> None:
        while True:
            with self._lock:
                try:
                    request = self._requests.get_nowait()
                except queue.Empty:
                    self._thread = None
                    return
            try:
                self._propagate()
            except DriveAuthError as error:
                self._record_error(error)
            except Exception:
                self._record_error(DriveAuthError("unexpected_error"))
            finally:
                self._send_reply(request)
                self._requests.task_done()

    def _record_error(self, error: DriveAuthError) -> None:
        with self._lock:
            if self._error is None:
                self._error = error

    def _remaining(self, phase: str) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DriveAuthError("deadline_exceeded", phase=phase)
        if self.cancelled.is_set():
            raise DriveAuthError("cancelled", phase=phase)
        return remaining

    def _request_json(self, method: str, phase: str, **kwargs) -> dict[str, Any]:
        timeout = min(HTTP_TIMEOUT_SEC, self._remaining(phase))
        headers = {
            "Accept": ACCEPT_JSON,
            "X-Colab-Client-Agent": CLIENT_AGENT,
            **kwargs.pop("headers", {}),
        }
        try:
            response = self.credentials.request(
                method, self.url, timeout=timeout, headers=headers, **kwargs
            )
        except Exception as error:
            self._log_http(
                phase=phase,
                method=method,
                status=None,
                byte_count=0,
                success=None,
                has_redirect=False,
            )
            raise DriveAuthError("request_failed", phase=phase) from error
        status = int(response.status_code)
        raw = getattr(response, "content", None)
        body = raw if isinstance(raw, bytes) else (response.text or "").encode("utf-8")
        if not 200 <= status < 300:
            self._log_http(
                phase=phase,
                method=method,
                status=status,
                byte_count=len(body),
                success=None,
                has_redirect=False,
            )
            raise DriveAuthError("http_error", phase=phase, status=status)
        if len(body) > MAX_RESPONSE_BYTES:
            self._log_http(
                phase=phase,
                method=method,
                status=status,
                byte_count=len(body),
                success=None,
                has_redirect=False,
            )
            raise DriveAuthError("response_too_large", phase=phase, status=status)
        try:
            text = body.decode("utf-8", errors="strict")
            if text.startswith(")]}'"):
                text = text[4:].lstrip("\r\n")
            data = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            self._log_http(
                phase=phase,
                method=method,
                status=status,
                byte_count=len(body),
                success=None,
                has_redirect=False,
            )
            raise DriveAuthError(
                "malformed_response", phase=phase, status=status
            ) from error
        if not isinstance(data, dict):
            self._log_http(
                phase=phase,
                method=method,
                status=status,
                byte_count=len(body),
                success=None,
                has_redirect=False,
            )
            raise DriveAuthError("malformed_response", phase=phase, status=status)
        raw_success = data.get("success")
        success = raw_success if type(raw_success) is bool else None
        redirect = data.get("unauthorized_redirect_uri")
        self._log_http(
            phase=phase,
            method=method,
            status=status,
            byte_count=len(body),
            success=success,
            has_redirect=isinstance(redirect, str) and bool(redirect),
        )
        return data

    def _log_http(
        self,
        *,
        phase: str,
        method: str,
        status: int | None,
        byte_count: int,
        success: bool | None,
        has_redirect: bool,
    ) -> None:
        self._log(
            "drive_auth_http",
            {
                "phase": phase,
                "method": method,
                "status": status,
                "byte_count": byte_count,
                "success": success,
                "has_redirect": has_redirect,
            },
        )

    def _propagation_attempt(
        self,
        params: dict[str, str],
        *,
        token_phase: str,
        post_phase: str,
    ) -> dict[str, Any]:
        token_data = self._request_json("GET", token_phase, params=params)
        token = token_data.get("token")
        if not isinstance(token, str) or not token:
            raise DriveAuthError("missing_token", phase=token_phase)
        return self._request_json(
            "POST",
            post_phase,
            params=params,
            headers={XSRF_HEADER: token},
        )

    def _propagate(self) -> None:
        base_params = {
            "authuser": "0",
            "authtype": "dfs_ephemeral",
            "version": "2",
            "propagate": "true",
            "record": "false",
        }
        dry_run_params = {**base_params, "dryrun": "true"}
        dry_run = self._propagation_attempt(
            dry_run_params,
            token_phase="dry_run_token",
            post_phase="dry_run",
        )
        if dry_run.get("success") is not True:
            self._authorize(dry_run)
            self.consent_waiter(self._remaining("consent"), self.cancelled)

        final_params = {**base_params, "dryrun": "false"}
        propagated = self._propagation_attempt(
            final_params,
            token_phase="propagate_token",
            post_phase="propagate",
        )
        if propagated.get("success") is not True:
            raise DriveAuthError("propagation_rejected", phase="propagate")
        self._log("drive_auth_success", {})
        self.emit("[colab] Credentials propagated. Resuming mount...")

    def _authorize(self, data: dict[str, Any]) -> None:
        uri = data.get("unauthorized_redirect_uri")
        if not self._valid_authorization_uri(uri):
            raise DriveAuthError("invalid_authorization_redirect", phase="dry_run")
        self._log("drive_auth_needed", {"uri": REDACTED})
        self.emit(
            "[colab] Google Drive authorization is required.\n"
            f"Visit:\n\n{uri}\n\n"
            "After the browser shows 'Please close this tab / window', "
            "return here and press Enter to continue."
        )

    @staticmethod
    def _valid_authorization_uri(uri: Any) -> bool:
        if not isinstance(uri, str):
            return False
        parsed = urlsplit(uri)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "accounts.google.com"
            and parsed.path.startswith("/o/oauth2/")
            and parsed.username is None
            and parsed.password is None
        )

    def _send_reply(self, request: _DriveRequest) -> None:
        with self._lock:
            if request.message_id in self._replied:
                return
            self._replied.add(request.message_id)
        content = {
            "value": {
                "type": "colab_reply",
                "colab_msg_id": request.message_id,
            }
        }
        reply = request.wsclient.session.msg("input_reply", content)
        if not isinstance(reply, dict):
            reply = {"msg_type": "input_reply", "content": content}
        if "header" in request.message:
            reply["parent_header"] = request.message["header"]
        try:
            request.wsclient.stdin_channel.send(reply)
        except Exception:
            self._record_error(DriveAuthError("reply_failed", phase="reply"))

    def _log(self, event_type: str, data: dict[str, Any]) -> None:
        if self.history is not None:
            self.history.log_event(self.session_name, event_type, data)


__all__ = ["DriveAuthCoordinator", "DriveAuthError"]
