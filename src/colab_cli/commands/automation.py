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

import datetime
import base64
import hashlib
import os
import sys
import json
from typing import Optional, List
import typer
from rich.console import Console
from typing_extensions import Annotated

from better_colab.commands import _emit_json_operation
from better_colab.compatibility import compatibility_session_lease
from better_colab.durable_commands import _client_from_cli_state
from better_colab.errors import BetterColabError, ExitCode, api_error
from better_colab.legacy import (
    emit_error as emit_json_error,
    emit_success as emit_json_success,
    resolve_session as resolve_json_session,
    wants_json,
)
from colab_cli.runtime import ColabRuntime
from colab_cli.contents import ContentsClient
from colab_cli.auth import get_credentials
from colab_cli.utils import get_status_code, render_display_data

_console = Console()


# Default execute() timeout for human-in-the-loop automations (auth /
# drivemount). The kernel goes silent while the user completes a browser
# OAuth flow, which can routinely take 30s+; the upstream 10s default
# raises ``TimeoutError`` mid-flow even though the mount actually succeeds.
# 10 minutes is long enough for any realistic interactive auth ceremony
# without leaving CI hangs unbounded.
INTERACTIVE_AUTOMATION_TIMEOUT_SEC = 600



def run_automation(
    name: str,
    op: str,
    code: str,
    allow_stdin: bool = False,
    path: str = None,
    timeout: Optional[float] = None,
    emit_output: bool = True,
):
    from colab_cli.common import state

    s = state.store.get(name)
    runtime = ColabRuntime(s.url, s.token, session_name=s.name, history=state.history)

    def drivefs_hook(deserialize_msg, wsclient):
        content = deserialize_msg.get("content", {})
        if content.get("request", {}).get("authType") == "dfs_ephemeral":
            msg_id = deserialize_msg.get("metadata", {}).get("colab_msg_id")
            state.history.log_event(
                s.name,
                "colab_request",
                {"type": "dfs_ephemeral", "colab_msg_id": msg_id},
            )
            url = f"{state.client.colab_domain}/tun/m/credentials-propagation/{s.endpoint}"
            params = {
                "authuser": "0",
                "authtype": "dfs_ephemeral",
                "version": "2",
                "dryrun": "true",
                "propagate": "true",
                "record": "false",
            }
            typer.echo(
                f"\n[colab] Intercepted Drive Auth Request. Connecting to {state.client.colab_domain}..."
            )

            creds = get_credentials(
                state.client_oauth_config, provider=state.auth_provider
            )
            resp = creds.request("GET", url, params=params)
            token = (
                json.loads(resp.text.split("\n", 1)[-1]).get("token")
                if get_status_code(resp) == 200
                else None
            )

            headers = {"x-goog-colab-token": token}
            resp = creds.request(
                "POST",
                url,
                params=params,
                headers=headers,
                files={"file_id": (None, "empty.ipynb")},
            )
            data = json.loads(resp.text.split("\n", 1)[-1])

            if not data.get("success"):
                uri = data.get("unauthorized_redirect_uri")
                typer.echo(
                    f"\n[colab] REQUIRED: Google Drive Authorization needed.\nPlease visit:\n\n{uri}\n"
                )
                state.history.log_event(s.name, "drive_auth_needed", {"uri": uri})
                sys.stdout.write("Press Enter after you have granted access... ")
                sys.stdout.flush()
                with open("/dev/tty") as tty:
                    tty.readline()

            typer.echo("[colab] Authorizing VM...")
            params["dryrun"] = "false"
            resp = creds.request(
                "POST",
                url,
                params=params,
                headers=headers,
                files={"file_id": (None, "empty.ipynb")},
            )
            if get_status_code(resp) == 200:
                typer.echo("[colab] Credentials propagated. Resuming mount...")
                state.history.log_event(s.name, "drive_auth_success", {})
                reply = wsclient.session.msg(
                    "input_reply",
                    {"value": {"type": "colab_reply", "colab_msg_id": msg_id}},
                )
                if "header" in deserialize_msg:
                    reply["parent_header"] = deserialize_msg["header"]
                wsclient.stdin_channel.send(reply)
            else:
                typer.echo(
                    f"[colab] Error propagating: {get_status_code(resp)} {resp.text}"
                )
            return True
        return False

    runtime.colab_request_hook = drivefs_hook
    outputs = []
    try:
        s.running = f"automation({op})"
        s.last_execution = (
            f"automation:{op}",
            None,
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        state.store.add(s)

        if op == "drivemount":
            state.history.log_event(
                name, "automation", {"op": "drivemount", "path": path, "code": code}
            )
        else:
            state.history.log_event(name, "automation", {"op": op, "code": code})

        outputs = runtime.execute_code(code, allow_stdin=allow_stdin, timeout=timeout)
        state.history.log_event(
            name, "automation_result", {"op": op, "outputs": outputs}
        )

        if emit_output:
            for out in outputs:
                if "text" in out:
                    sys.stdout.write(out["text"])
                elif "data" in out:
                    text = render_display_data(out["data"])
                    if text is not None:
                        _console.print(text)
                elif out.get("output_type") == "error":
                    ename = out.get("ename", "Error")
                    evalue = out.get("evalue", "")
                    tb = out.get("traceback", [])
                    if tb:
                        sys.stderr.write("".join(tb) + "\n")
                    else:
                        sys.stderr.write(f"{ename}: {evalue}\n")
    finally:
        s.running = None
        state.store.add(s)
        runtime.stop()
    return outputs


def auth(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
):
    """Authenticate with Google on the VM"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    code = "import os\nos.environ['USE_AUTH_EPHEM'] = '0'\nfrom google.colab import auth\nauth.authenticate_user()"
    typer.echo(f"[colab] Starting Google Auth flow on {name}...")
    with compatibility_session_lease(name):
        run_automation(
            name,
            "auth",
            code,
            allow_stdin=True,
            timeout=INTERACTIVE_AUTOMATION_TIMEOUT_SEC,
        )


def drivemount(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    path: Annotated[str, typer.Argument(help="Mount path")] = "/content/drive",
):
    """Mount Google Drive at path"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    code = f"from google.colab import drive\ndrive.mount('{path}')"
    typer.echo(f"[colab] Mounting Google Drive to '{path}' on {name}...")
    with compatibility_session_lease(name):
        run_automation(
            name,
            "drivemount",
            code,
            allow_stdin=True,
            path=path,
            timeout=INTERACTIVE_AUTOMATION_TIMEOUT_SEC,
        )


def install(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    packages: Annotated[
        Optional[List[str]], typer.Argument(help="Packages to install")
    ] = None,
    requirement: Annotated[
        Optional[str], typer.Option("-r", "--requirement", help="Requirements file")
    ] = None,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
):
    """Install python packages on the VM"""
    from colab_cli.common import state

    if state.durable_wrappers:
        _durable_install(
            session=session,
            packages=list(packages or []),
            requirement=requirement,
            output_format=output_format,
        )
        return

    json_output = wants_json(output_format)
    name = (
        resolve_json_session(state, session)
        if json_output
        else state.resolve_session(session)
    )
    if not packages and not requirement:
        if json_output:
            emit_json_error(
                "INSTALL_INPUT_REQUIRED",
                "Specify packages or --requirement",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="specify_packages",
            )
        typer.echo("[colab] No packages or requirements specified.")
        raise typer.Exit(1)

    commands = []
    if requirement:
        if not os.path.isfile(requirement):
            if json_output:
                emit_json_error(
                    "LOCAL_FILE_NOT_FOUND",
                    f"Requirements file '{requirement}' not found",
                    exit_code=ExitCode.NOT_FOUND,
                    retryable=False,
                    suggested_action="check_local_path",
                )
            typer.echo(f"[colab] Requirements file '{requirement}' not found locally.")
            raise typer.Exit(1)
        contents = ContentsClient(state.store.get(name))
        remote_path = f"content/{os.path.basename(requirement)}"
        contents.upload(requirement, remote_path)
        commands.extend(["-r", f"/{remote_path}"])
    if packages:
        commands.extend(packages)

    cmd_str = ", ".join(f"'{c}'" for c in commands)
    code = f"""
import subprocess, sys
def install():
    packages = [{cmd_str}]
    try:
        subprocess.check_call(['uv', 'pip', 'install', '--system'] + packages)
        print('Installation Complete (via uv)!')
    except:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install'] + packages)
        print('Installation Complete (via pip)!')
install()
"""
    if not json_output:
        typer.echo(f"[colab] Installing packages on {name} (preferring uv)...")
    try:
        run_automation(name, "install", code, emit_output=not json_output)
    except Exception as error:
        if json_output:
            emit_json_error(
                "INSTALL_FAILED",
                str(error),
                exit_code=ExitCode.UNAVAILABLE,
                retryable=True,
                suggested_action="inspect_execution_and_retry",
            )
        raise
    if json_output:
        emit_json_success(
            {
                "session": name,
                "packages": list(packages or []),
                "requirement": requirement,
                "installed": True,
            }
        )


def _durable_install(
    *,
    session: str | None,
    packages: list[str],
    requirement: str | None,
    output_format: str,
) -> None:
    from colab_cli.commands.execution import (
        FAILED_EXECUTION_STATES,
        _durable_session_name,
        _render_durable_execution,
    )

    normalized = output_format.lower()
    if normalized not in {"text", "json"}:
        typer.echo("format must be 'text' or 'json'", err=True)
        raise typer.Exit(code=int(ExitCode.USAGE))

    def execute():
        if not packages and requirement is None:
            raise api_error(
                "INSTALL_INPUT_REQUIRED",
                "Specify packages or --requirement",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="specify_packages",
            )
        source, requirement_path = _durable_install_source(
            packages,
            requirement,
        )
        with _client_from_cli_state() as client:
            name = _durable_session_name(client, session)
            provenance = {"kind": "install"}
            if requirement_path is not None:
                provenance["path"] = requirement_path
            result = client.start_execution(
                session=name,
                source=source,
                provenance=provenance,
                execution_timeout=None,
            )
            return client, name, result

    if normalized == "json":
        def operation():
            _client, name, result = execute()
            if result.state in FAILED_EXECUTION_STATES:
                raise _install_execution_error(result)
            return {
                "session": name,
                "packages": packages,
                "requirement": requirement,
                "installed": True,
            }

        _emit_json_operation(operation)
        return

    try:
        client, _name, result = execute()
    except BetterColabError as error:
        typer.echo(error.error.message, err=True)
        raise typer.Exit(code=int(error.exit_code))
    _render_durable_execution(client, result, None)
    if result.state in FAILED_EXECUTION_STATES:
        raise typer.Exit(code=int(ExitCode.EXECUTION_FAILED))


def _durable_install_source(
    packages: list[str],
    requirement: str | None,
) -> tuple[str, str | None]:
    arguments = list(packages)
    setup = "_requirement_path = None"
    requirement_path: str | None = None
    if requirement is not None:
        requirement_path = os.path.realpath(requirement)
        try:
            with open(requirement_path, "rb") as requirement_file:
                raw = requirement_file.read()
        except FileNotFoundError as error:
            raise api_error(
                "LOCAL_FILE_NOT_FOUND",
                f"Requirements file '{requirement}' not found",
                exit_code=ExitCode.NOT_FOUND,
                retryable=False,
                suggested_action="check_local_path",
            ) from error
        except OSError as error:
            raise api_error(
                "LOCAL_FILE_UNREADABLE",
                f"Requirements file '{requirement}' could not be read",
                exit_code=ExitCode.UNAVAILABLE,
                retryable=False,
                suggested_action="check_local_permissions",
                details={"error": str(error)},
            ) from error
        digest = hashlib.sha256(raw).hexdigest()
        remote_path = f"/tmp/better-colab-requirements-{digest[:16]}.txt"
        encoded = base64.b64encode(raw).decode("ascii")
        setup = (
            f"_requirement_path = {remote_path!r}\n"
            "with open(_requirement_path, 'wb') as _requirements:\n"
            f"    _requirements.write(base64.b64decode({encoded!r}))"
        )
        arguments = ["-r", remote_path, *arguments]

    source = f"""\
import base64, contextlib, os, subprocess, sys
{setup}
_packages = {arguments!r}
try:
    try:
        subprocess.check_call(['uv', 'pip', 'install', '--system'] + _packages)
        print('Installation Complete (via uv)!')
    except (FileNotFoundError, subprocess.CalledProcessError):
        subprocess.check_call([sys.executable, '-m', 'pip', 'install'] + _packages)
        print('Installation Complete (via pip)!')
finally:
    if _requirement_path is not None:
        with contextlib.suppress(OSError):
            os.unlink(_requirement_path)
"""
    return source, requirement_path


def _install_execution_error(result) -> BetterColabError:
    message = "Package installation failed"
    if result.error_name:
        message = f"{result.error_name}: {result.error_value or ''}"
    return api_error(
        "INSTALL_FAILED",
        message,
        exit_code=ExitCode.EXECUTION_FAILED,
        retryable=False,
        suggested_action="inspect_execution_and_retry",
        details={
            "execution_id": result.execution_id,
            "state": result.state.value,
        },
    )


def register(app: typer.Typer, *, include_drive: bool = True):
    app.command(hidden=True)(auth)
    if include_drive:
        app.command()(drivemount)
    app.command()(install)
