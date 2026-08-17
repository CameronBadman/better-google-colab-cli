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

import json
import os
import threading
import time
from unittest.mock import MagicMock, patch

import jupyter_kernel_client
import pytest

from colab_cli.runtime import ColabRuntime


@patch("colab_cli.runtime.jupyter_kernel_client.JupyterKernelClient")
def test_colab_runtime_kernel_client(mock_kc_cls):
    mock_kc = mock_kc_cls.return_value

    runtime = ColabRuntime("http://url", "token123")

    assert runtime._kernel_client is None

    kc = runtime.kernel_client

    mock_kc_cls.assert_called_once_with(
        server_url="http://url",
        token="token123",
        kernel_id=None,
        client_kwargs={
            "subprotocol": jupyter_kernel_client.JupyterSubprotocol.DEFAULT,
            "extra_params": {"colab-runtime-proxy-token": "token123"},
        },
        headers={
            "X-Colab-Client-Agent": "colab-cli",
            "X-Colab-Runtime-Proxy-Token": "token123",
        },
    )
    mock_kc.start.assert_called_once()
    assert kc == mock_kc


def test_colab_runtime_execute_code():
    runtime = ColabRuntime("http://url", "token123")
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc

    # Test empty reply
    mock_kc.execute.return_value = {}
    assert runtime.execute_code("print(1)") == []

    # Test normal reply
    mock_kc.execute.return_value = {"outputs": [{"text": "1\n"}]}
    assert runtime.execute_code("print(1)") == [{"text": "1\n"}]

    # Test error status without error output
    mock_kc.execute.return_value = {
        "status": "error",
        "ename": "ValueError",
        "evalue": "bad",
        "outputs": [{"text": "partial"}],
    }
    outputs = runtime.execute_code("raise ValueError")
    assert len(outputs) == 2
    assert outputs[0] == {"text": "partial"}
    assert outputs[1] == {
        "output_type": "error",
        "ename": "ValueError",
        "evalue": "bad",
        "traceback": [],
    }


def test_colab_runtime_execute_code_default_no_timeout():
    """By default, execute_code should NOT pass a timeout (relies on jupyter
    kernel client default), preserving existing behavior for fast / streaming
    workloads."""
    runtime = ColabRuntime("http://url", "token123")
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc

    mock_kc.execute.return_value = {"outputs": []}
    runtime.execute_code("print(1)")

    _, kwargs = mock_kc.execute.call_args
    assert "timeout" not in kwargs


def test_colab_runtime_execute_code_with_timeout():
    """When a timeout is supplied, it must be forwarded to kernel_client.execute."""
    runtime = ColabRuntime("http://url", "token123")
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc

    mock_kc.execute.return_value = {"outputs": []}
    runtime.execute_code("print(1)", timeout=600)

    _, kwargs = mock_kc.execute.call_args
    assert kwargs.get("timeout") == 600


def test_colab_runtime_execute_interactive_with_timeout():
    """timeout must also be plumbed through the execute_interactive branch
    (used when an output_hook is supplied)."""
    runtime = ColabRuntime("http://url", "token123")
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc

    mock_kc.execute_interactive.return_value = {"content": {"status": "ok"}}
    runtime.execute_code("print(1)", output_hook=lambda o: None, timeout=600)

    _, kwargs = mock_kc.execute_interactive.call_args
    assert kwargs.get("timeout") == 600


def test_colab_runtime_stop():
    runtime = ColabRuntime("http://url", "token123")
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc

    runtime.stop()
    mock_kc._manager.client.stop_channels.assert_called_once()


def test_colab_runtime_stop_exception(caplog):
    runtime = ColabRuntime("http://url", "token123")
    mock_kc = MagicMock()
    mock_kc._manager.client.stop_channels.side_effect = Exception("Stop failed")
    runtime._kernel_client = mock_kc

    runtime.stop()  # Should not raise
    assert "Error stopping kernel client" in caplog.text


def test_colab_runtime_stdin_reply_is_sent_and_redacted():
    mock_history = MagicMock()
    runtime = ColabRuntime(
        "http://url", "token", session_name="test-s", history=mock_history
    )
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc
    mock_kc._manager.client.stdin_channel.msg_ready.return_value = False
    mock_kc._manager.client.shell_channel.msg_ready.return_value = False

    def execute(code, allow_stdin=False, stdin_hook=None):
        message = {"content": {"prompt": "Enter something: ", "password": False}}
        stdin_hook(message)
        return {"outputs": []}

    mock_kc.execute.side_effect = execute

    canary = "CANARY-user-input-001"
    with patch("colab_cli.runtime.input", return_value=canary):
        outputs = runtime.execute_code("code", allow_stdin=True)

    assert outputs == []
    mock_kc._manager.client.input.assert_called_once_with(canary)
    mock_history.log_event.assert_any_call(
        "test-s",
        "stdin_request",
        {"prompt": "<redacted>", "password": False},
    )
    mock_history.log_event.assert_any_call(
        "test-s", "input_reply", {"value": "<redacted>"}
    )
    assert canary not in json.dumps(
        [call.args for call in mock_history.log_event.call_args_list]
    )


def test_colab_runtime_password_reply_uses_getpass_and_is_redacted():
    mock_history = MagicMock()
    runtime = ColabRuntime(
        "http://url", "token", session_name="test-s", history=mock_history
    )
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc
    mock_kc._manager.client.stdin_channel.msg_ready.return_value = False
    mock_kc._manager.client.shell_channel.msg_ready.return_value = False

    def execute(code, allow_stdin=False, stdin_hook=None):
        stdin_hook({"content": {"prompt": "Secret: ", "password": True}})
        return {"outputs": []}

    mock_kc.execute.side_effect = execute

    with patch("colab_cli.runtime.getpass.getpass", return_value="secret-value"):
        runtime.execute_code("code", allow_stdin=True)

    mock_kc._manager.client.input.assert_called_once_with("secret-value")
    mock_history.log_event.assert_any_call(
        "test-s", "input_reply", {"value": "<redacted>"}
    )
    assert "secret-value" not in json.dumps(
        [call.args for call in mock_history.log_event.call_args_list]
    )


def test_colab_runtime_skips_stale_stdin_reply():
    mock_history = MagicMock()
    runtime = ColabRuntime(
        "http://url", "token", session_name="test-s", history=mock_history
    )
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc
    mock_kc._manager.client.stdin_channel.msg_ready.return_value = True
    mock_kc._manager.client.shell_channel.msg_ready.return_value = False

    def execute(code, allow_stdin=False, stdin_hook=None):
        stdin_hook({"content": {"prompt": "Already obsolete: ", "password": False}})
        return {"outputs": []}

    mock_kc.execute.side_effect = execute

    with patch("colab_cli.runtime.input", return_value="do not send") as prompt:
        runtime.execute_code("code", allow_stdin=True)

    prompt.assert_not_called()
    mock_kc._manager.client.input.assert_not_called()
    mock_history.log_event.assert_any_call(
        "test-s",
        "input_reply_skipped",
        {"value": "<redacted>", "reason": "newer_message_ready"},
    )


def test_colab_runtime_eof_sends_control_d():
    runtime = ColabRuntime("http://url", "token")
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc
    mock_kc._manager.client.stdin_channel.msg_ready.return_value = False
    mock_kc._manager.client.shell_channel.msg_ready.return_value = False

    def execute(code, allow_stdin=False, stdin_hook=None):
        stdin_hook({"content": {"prompt": "Value: ", "password": False}})
        return {"outputs": []}

    mock_kc.execute.side_effect = execute
    with patch("colab_cli.runtime.input", side_effect=EOFError):
        runtime.execute_code("code", allow_stdin=True)

    mock_kc._manager.client.input.assert_called_once_with("\x04")


def test_colab_runtime_ctrl_c_interrupts_remote_and_propagates():
    runtime = ColabRuntime("http://url", "token")
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc
    mock_kc._manager.client.stdin_channel.msg_ready.return_value = False
    mock_kc._manager.client.shell_channel.msg_ready.return_value = False

    def execute(code, allow_stdin=False, stdin_hook=None):
        stdin_hook({"content": {"prompt": "Value: ", "password": False}})
        return {"outputs": []}

    mock_kc.execute.side_effect = execute
    with patch("colab_cli.runtime.input", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            runtime.execute_code("code", allow_stdin=True)

    mock_kc.interrupt.assert_called_once()


def test_custom_stdin_hook_records_completion_not_claimed_reply():
    history = MagicMock()
    runtime = ColabRuntime(
        "http://url", "token", session_name="test-s", history=history
    )
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc
    mock_kc._manager.client.stdin_channel.msg_ready.return_value = False
    mock_kc._manager.client.shell_channel.msg_ready.return_value = False
    hook = MagicMock()

    def execute(code, allow_stdin=False, stdin_hook=None):
        stdin_hook({"content": {"prompt": "Value: ", "password": False}})
        return {"outputs": []}

    mock_kc.execute.side_effect = execute
    runtime.execute_code("code", allow_stdin=True, stdin_hook=hook)

    hook.assert_called_once()
    event_types = [call.args[1] for call in history.log_event.call_args_list]
    assert "stdin_hook_completed" in event_types
    assert "input_reply" not in event_types


@pytest.mark.parametrize(
    "message",
    [
        {"msg_type": "execute_reply", "content": {"prompt": "bad"}},
        {
            "msg_type": "input_request",
            "parent_header": {"session": "other-session"},
            "content": {"prompt": "bad"},
        },
    ],
)
def test_invalid_or_foreign_stdin_message_is_not_prompted(message):
    history = MagicMock()
    runtime = ColabRuntime(
        "http://url", "token", session_name="test-s", history=history
    )
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc
    mock_kc._manager.client.session.session = "our-session"
    mock_kc._manager.client.stdin_channel.msg_ready.return_value = False
    mock_kc._manager.client.shell_channel.msg_ready.return_value = False

    def execute(code, allow_stdin=False, stdin_hook=None):
        stdin_hook(message)
        return {"outputs": []}

    mock_kc.execute.side_effect = execute
    with patch("colab_cli.runtime.input") as prompt:
        runtime.execute_code("code", allow_stdin=True)

    prompt.assert_not_called()
    mock_kc._manager.client.input.assert_not_called()
    history.log_event.assert_any_call(
        "test-s",
        "input_reply_skipped",
        {"value": "<redacted>", "reason": "invalid_input_request"},
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard deadline")
def test_execute_code_timeout_is_a_real_wall_clock_bound():
    runtime = ColabRuntime("http://url", "token")
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc
    mock_kc.execute.side_effect = lambda *_args, **_kwargs: time.sleep(2)

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="timed out"):
        runtime.execute_code("code", timeout=0.05)

    assert time.monotonic() - started < 0.5
    mock_kc.interrupt.assert_called_once()


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard deadline")
def test_finite_timeout_off_main_thread_fails_before_execution():
    runtime = ColabRuntime("http://url", "token")
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc
    errors = []

    def worker():
        try:
            runtime.execute_code("code", timeout=1)
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=2)

    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert "main thread" in str(errors[0])
    mock_kc.execute.assert_not_called()
