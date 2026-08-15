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

import contextlib
import getpass
import logging
import os
import signal
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Optional

import jupyter_kernel_client
import requests


logger = logging.getLogger(__name__)
REDACTED = "<redacted>"
INTERRUPT_TIMEOUT_SEC = 5
StdinHook = Callable[[Dict[str, Any]], None]
ColabRequestHook = Callable[[Dict[str, Any], Any], bool]


@contextlib.contextmanager
def _execution_deadline(timeout: Optional[float]) -> Iterator[None]:
    if timeout is None:
        yield
        return
    if timeout <= 0:
        raise TimeoutError("Colab execution timed out")
    if os.name == "nt" or not hasattr(signal, "setitimer"):
        raise RuntimeError("Finite Colab timeouts require POSIX signal support")
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Finite Colab timeouts must run on the main thread")
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    if previous_timer[0] > 0:
        raise RuntimeError("Cannot replace an existing real-time signal timer")
    previous_handler = signal.getsignal(signal.SIGALRM)

    def raise_timeout(_signum, _frame):
        raise TimeoutError("Colab execution timed out")

    signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


class ColabRuntime:
    def __init__(
        self,
        url: str,
        token: str,
        session_name: Optional[str] = None,
        history: Optional[Any] = None,
        kernel_id: Optional[str] = None,
        session_id: Optional[str] = None,
        on_kernel_started: Optional[Callable[[str], None]] = None,
        on_session_started: Optional[Callable[[str], None]] = None,
    ):
        self.url = url
        self.token = token
        self.session_name = session_name
        self.history = history
        self.kernel_id = kernel_id
        self.session_id = session_id
        self.on_kernel_started = on_kernel_started
        self.on_session_started = on_session_started
        self._kernel_client = None
        self.colab_request_hook: Optional[ColabRequestHook] = None

    @property
    def _raw_client(self):
        return self.kernel_client._manager.client

    def _apply_ws_hook(self):
        wsclient = self._kernel_client._manager.client
        original_on_message = wsclient.kernel_socket.on_message

        def hooked_on_message(s_ws, message):
            if not self.colab_request_hook:
                return original_on_message(s_ws, message)

            try:
                from jupyter_kernel_client.wsclient import JupyterSubprotocol

                if wsclient._subprotocol == JupyterSubprotocol.DEFAULT:
                    from jupyter_kernel_client.wsclient import (
                        deserialize_msg_from_ws_default,
                    )

                    deserialize_msg = deserialize_msg_from_ws_default(message)
                elif wsclient._subprotocol == JupyterSubprotocol.V1:
                    from jupyter_kernel_client.wsclient import (
                        deserialize_msg_from_ws_v1,
                    )

                    channel, msg_list = deserialize_msg_from_ws_v1(message)
                    deserialize_msg = wsclient.session.deserialize(msg_list)
                else:
                    deserialize_msg = None

                if deserialize_msg:
                    msg_type = deserialize_msg.get("msg_type")
                    if msg_type == "colab_request":
                        # We pass the deserialized msg and the wsclient to the hook
                        if self.colab_request_hook(deserialize_msg, wsclient):
                            # If the hook returns True, we intercept and do NOT pass to original
                            return

            except Exception as error:
                logger.debug("Error in colab_request hook: %s", type(error).__name__)

            # Call original for all other messages
            original_on_message(s_ws, message)

        wsclient.kernel_socket.on_message = hooked_on_message

    @property
    def kernel_client(self):
        if self._kernel_client:
            return self._kernel_client

        for attempt in range(3):
            try:
                self._start_kernel_client()
                break
            except (
                requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectTimeout,
            ):
                if attempt == 2:
                    raise
                sleep_time = 2 ** (attempt + 1)
                logger.debug(
                    "Kernel startup timeout, retrying in %ss (%s/3)",
                    sleep_time,
                    attempt + 1,
                )
                time.sleep(sleep_time)

        return self._kernel_client

    def _start_kernel_client(self) -> None:
        client_kwargs = {
            "subprotocol": jupyter_kernel_client.JupyterSubprotocol.DEFAULT,
            "extra_params": {"colab-runtime-proxy-token": self.token},
        }
        if self.session_id:
            client_kwargs["session"] = self.session_id
        self._kernel_client = jupyter_kernel_client.KernelClient(
            server_url=self.url,
            token=self.token,
            kernel_id=self.kernel_id,
            client_kwargs=client_kwargs,
            headers={
                "X-Colab-Client-Agent": "colab-cli",
                "X-Colab-Runtime-Proxy-Token": self.token,
            },
        )
        self._kernel_client._own_kernel = False
        self._kernel_client.start()
        self._apply_ws_hook()
        self._capture_started_ids()

    def _capture_started_ids(self) -> None:
        if not self.kernel_id and self._kernel_client.id:
            self.kernel_id = self._kernel_client.id
            if self.on_kernel_started:
                self.on_kernel_started(self.kernel_id)
        remote_session_id = self._kernel_client._manager.client.session.session
        if not self.session_id and remote_session_id:
            self.session_id = remote_session_id
            if self.on_session_started:
                self.on_session_started(self.session_id)

    def restart(
        self,
        timeout: Optional[float] = None,
    ):
        self.kernel_client.restart(timeout=timeout)

    def execute_code(
        self,
        code: str,
        allow_stdin: bool = False,
        stdin_hook: Optional[StdinHook] = None,
        output_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
        timeout: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        kwargs = {"allow_stdin": allow_stdin}
        if timeout is not None:
            kwargs["timeout"] = timeout

        if allow_stdin:
            kwargs["stdin_hook"] = self._make_stdin_hook(stdin_hook)

        try:
            with _execution_deadline(timeout):
                outputs, reply_content = self._execute(
                    code, output_hook=output_hook, kwargs=kwargs
                )
        except (KeyboardInterrupt, TimeoutError):
            self._interrupt_best_effort()
            raise

        self._append_synthetic_error(outputs, reply_content)
        return outputs

    def _log_history(self, event_type: str, data: Dict[str, Any]) -> None:
        if self.history and self.session_name:
            self.history.log_event(self.session_name, event_type, data)

    def _message_ready(self) -> bool:
        return bool(
            self._raw_client.stdin_channel.msg_ready()
            or self._raw_client.shell_channel.msg_ready()
        )

    def _make_stdin_hook(self, custom_hook: Optional[StdinHook]) -> StdinHook:
        def handle_stdin(message: Dict[str, Any]) -> None:
            content = message.get("content", {})
            prompt_text = str(content.get("prompt", ""))
            is_password = bool(content.get("password", False))
            self._log_history(
                "stdin_request", {"prompt": REDACTED, "password": is_password}
            )
            if not self._valid_input_request(message):
                self._log_history(
                    "input_reply_skipped",
                    {"value": REDACTED, "reason": "invalid_input_request"},
                )
                return
            if self._message_ready():
                self._log_skipped_reply()
                return
            if custom_hook is not None:
                custom_hook(message)
                self._log_history("stdin_hook_completed", {})
                return

            prompt = getpass.getpass if is_password else input
            try:
                response = prompt(prompt_text)
            except EOFError:
                response = "\x04"
            if self._message_ready():
                self._log_skipped_reply()
                return
            self._raw_client.input(response)
            self._log_history("input_reply", {"value": REDACTED})

        return handle_stdin

    def _valid_input_request(self, message: Dict[str, Any]) -> bool:
        message_type = message.get("msg_type") or message.get("header", {}).get(
            "msg_type"
        )
        if message_type is not None and message_type != "input_request":
            return False
        parent_session = message.get("parent_header", {}).get("session")
        current_session = self._raw_client.session.session
        return not (
            parent_session and current_session and parent_session != current_session
        )

    def _log_skipped_reply(self) -> None:
        self._log_history(
            "input_reply_skipped",
            {"value": REDACTED, "reason": "newer_message_ready"},
        )

    def _execute(self, code, *, output_hook, kwargs):
        if output_hook is None:
            reply = self.kernel_client.execute(code, **kwargs)
            if not reply:
                return [], {}
            return reply.get("outputs", []), reply
        return self._execute_streaming(code, output_hook, kwargs)

    def _execute_streaming(self, code, output_hook, kwargs):
        from jupyter_kernel_client.client import output_hook as default_output_hook

        outputs = []

        def wrapped_output_hook(message):
            new_indexes = default_output_hook(outputs, message)
            for index in sorted(new_indexes or []):
                if index < len(outputs):
                    output_hook(outputs[index])

        reply = self.kernel_client.execute_interactive(
            code, output_hook=wrapped_output_hook, **kwargs
        )
        reply_content = reply["content"] if reply else {"status": "error"}
        return outputs, reply_content

    @staticmethod
    def _append_synthetic_error(outputs, reply_content):
        if reply_content.get("status") != "error":
            return
        if any(output.get("output_type") == "error" for output in outputs):
            return
        outputs.append(
            {
                "output_type": "error",
                "ename": reply_content.get("ename", "Error"),
                "evalue": reply_content.get("evalue", "Unknown error"),
                "traceback": reply_content.get("traceback", []),
            }
        )

    def _interrupt_best_effort(self) -> None:
        try:
            self.kernel_client.interrupt(timeout=INTERRUPT_TIMEOUT_SEC)
        except Exception as error:
            logger.warning(
                "Failed to interrupt remote kernel: %s", type(error).__name__
            )

    def stop(self, shutdown_kernel: bool = False):
        if self._kernel_client:
            try:
                # We manage kernel lifecycle explicitly.
                # To prevent automatic shutdown, we bypass the manager's stop() and
                # directly close the channels and socket.
                client = self._kernel_client._manager.client
                client.stop_channels()
                if client.kernel_socket:
                    client.kernel_socket.close()

                if shutdown_kernel:
                    self._kernel_client._manager.shutdown_kernel(now=True)
            except Exception:
                logger.exception("Error stopping kernel client")
