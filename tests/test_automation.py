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

from unittest.mock import patch
import pytest
from typer.testing import CliRunner
from colab_cli.cli import app
from colab_cli.state import SessionState

runner = CliRunner()


@pytest.fixture
def mock_session():
    return SessionState(
        name="test-session",
        token="test-token",
        url="https://test.url",
        endpoint="e1",
    )


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_cli_auth(mock_state, mock_runtime_class, mock_session):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"

    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"text": "Success"}]

    result = runner.invoke(app, ["auth", "-s", "test-session"])
    assert result.exit_code == 0

    assert mock_session.last_execution[0] == "automation:auth"
    assert mock_session.last_execution[1] is None
    assert mock_session.last_execution[2] is not None
    mock_state.store.add.assert_called_with(mock_session)

    # Verify ColabRuntime was invoked with the correct code
    mock_runtime.execute_code.assert_called_once()
    called_code = mock_runtime.execute_code.call_args[0][0]

    assert "os.environ['USE_AUTH_EPHEM'] = '0'" in called_code
    assert "auth.authenticate_user()" in called_code


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_cli_install(mock_state, mock_runtime_class, mock_session):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"

    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"text": "Installed"}]

    result = runner.invoke(app, ["install", "-s", "test-session", "pandas", "numpy"])
    assert result.exit_code == 0
    assert mock_session.last_execution[0] == "automation:install"
    assert mock_session.last_execution[2] is not None
    mock_state.store.add.assert_called_with(mock_session)

    mock_runtime.execute_code.assert_called_once()
    called_code = mock_runtime.execute_code.call_args[0][0]

    assert "subprocess" in called_code
    assert "pip" in called_code
    assert "pandas" in called_code
    assert "numpy" in called_code


@patch("colab_cli.commands.automation.get_credentials")
@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_cli_drivemount(
    mock_state, mock_runtime_class, mock_get_credentials, mock_session
):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"

    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"text": "Mounted"}]

    result = runner.invoke(app, ["drivemount", "-s", "test-session", "/foo/bar"])
    assert result.exit_code == 0

    # Verify ColabRuntime was invoked with the correct code
    mock_runtime.execute_code.assert_called_once()
    called_code = mock_runtime.execute_code.call_args[0][0]

    assert "drive.mount('/foo/bar')" in called_code
    assert mock_runtime.colab_request_hook is not None
    mock_get_credentials.assert_called_once()
    # Drivemount waits for the user to OAuth in their browser; the kernel
    # goes silent during that wait and the default 10s execute() timeout
    # would raise TimeoutError mid-flow. Insist on a generous timeout
    # (>= 5 minutes) being forwarded to runtime.execute_code.
    _, kwargs = mock_runtime.execute_code.call_args
    assert kwargs.get("timeout") is not None and kwargs["timeout"] >= 300


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_auth_does_not_install_drive_hook(mock_state, mock_runtime_class, mock_session):
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = []
    mock_runtime.colab_request_hook = None

    result = runner.invoke(app, ["auth", "-s", "test-session"])

    assert result.exit_code == 0
    assert mock_runtime.colab_request_hook is None


@patch("colab_cli.common.state")
def test_run_automation_rejects_missing_session(mock_state):
    from colab_cli.commands.automation import run_automation

    mock_state.store.get.return_value = None
    with pytest.raises(ValueError, match="not found"):
        run_automation("missing", "install", "print(1)")


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_run_automation_stops_runtime_if_final_state_save_fails(
    mock_state, mock_runtime_class, mock_session
):
    from colab_cli.commands.automation import run_automation

    mock_state.store.get.return_value = mock_session
    mock_state.store.add.side_effect = [None, RuntimeError("state save failed")]
    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = []

    with pytest.raises(RuntimeError, match="state save failed"):
        run_automation("test-session", "install", "print(1)")

    mock_runtime.stop.assert_called_once()


def test_generated_drivemount_code_uses_one_safe_string_literal():
    import ast
    from colab_cli.commands.automation import _build_drivemount_code

    payload = "x'); __import__('os').system('bad') #\n\\tail"
    tree = ast.parse(_build_drivemount_code(payload))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    mount_call = next(
        node
        for node in calls
        if isinstance(node.func, ast.Attribute) and node.func.attr == "mount"
    )
    assert len(mount_call.args) == 1
    assert isinstance(mount_call.args[0], ast.Constant)
    assert mount_call.args[0].value == payload


def test_generated_install_code_uses_safe_list_literal_and_narrow_fallback():
    import ast
    from colab_cli.commands.automation import _build_install_code

    payload = "pkg'); __import__('os').system('bad') #\n\\tail"
    tree = ast.parse(_build_install_code([payload, "normal"])).body
    install_function = next(node for node in tree if isinstance(node, ast.FunctionDef))
    assignment = next(
        node
        for node in install_function.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "packages"
            for target in node.targets
        )
    )
    assert ast.literal_eval(assignment.value) == [payload, "normal"]

    handler = next(
        node
        for node in ast.walk(install_function)
        if isinstance(node, ast.ExceptHandler)
    )
    caught = {ast.unparse(item) for item in handler.type.elts}
    assert caught == {"subprocess.CalledProcessError", "OSError"}


@patch("colab_cli.commands.automation.ColabRuntime")
@patch("colab_cli.common.state")
def test_cli_auth_uses_long_timeout(mock_state, mock_runtime_class, mock_session):
    """`colab auth` walks the user through a paste-the-code flow that
    routinely takes >10s, so it must pass a generous timeout to
    runtime.execute_code or the call will TimeoutError mid-flow."""
    mock_state.store.get.return_value = mock_session
    mock_state.resolve_session.return_value = "test-session"

    mock_runtime = mock_runtime_class.return_value
    mock_runtime.execute_code.return_value = [{"text": "Authenticated"}]

    result = runner.invoke(app, ["auth", "-s", "test-session"])
    assert result.exit_code == 0

    _, kwargs = mock_runtime.execute_code.call_args
    assert kwargs.get("timeout") is not None and kwargs["timeout"] >= 300
