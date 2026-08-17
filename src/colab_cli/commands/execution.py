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
import datetime
import nbformat
import os
import re
import shutil
import sys
import typer
import uuid
from nbformat.v4 import new_output
from rich.console import Console
from typing import Optional
from typing_extensions import Annotated

from better_colab.compatibility import compatibility_session_lease
from better_colab.durable_commands import _client_from_cli_state
from better_colab.models import (
    BatchState,
    ExecutionState,
    OutputPage,
)
from colab_cli.runtime import ColabRuntime
from colab_cli.utils import handle_image, is_terminal_error, render_display_data
from colab_cli.console import connect_console

_console = Console()

TITLE_REGEX = re.compile(r"^\s*#\s*@title\s+(.*)", re.MULTILINE)
FAILED_EXECUTION_STATES = {
    ExecutionState.ERROR,
    ExecutionState.INTERRUPTED,
    ExecutionState.TIMED_OUT,
    ExecutionState.UNKNOWN,
}


def is_stdin_tty():
    return sys.stdin.isatty()


def save_output(outputs, cell):
    if cell is None:
        return

    if not hasattr(cell, "outputs"):
        cell.outputs = []
    else:
        cell.outputs.clear()

    for out in outputs:
        if out.get("output_type") == "stream":
            cell.outputs.append(
                new_output(
                    output_type="stream",
                    name=out.get("name", "stdout"),
                    text=out.get("text", ""),
                )
            )
        elif "data" in out:
            output_type = out.get("output_type", "display_data")
            cell.outputs.append(
                new_output(
                    output_type=output_type,
                    data=out["data"],
                    metadata=out.get("metadata", {}),
                )
            )
        elif out.get("output_type") == "error":
            cell.outputs.append(
                new_output(
                    output_type="error",
                    ename=out.get("ename", "Error"),
                    evalue=out.get("evalue", ""),
                    traceback=out.get("traceback", []),
                )
            )



def display_output(out, output_image=None):
    if out.get("output_type") == "stream":
        stream = sys.stderr if out.get("name") == "stderr" else sys.stdout
        stream.write(out.get("text", ""))
        stream.flush()
    elif "data" in out:
        data = out["data"]
        text = render_display_data(data)
        if text is not None:
            _console.print(text)
        if png := data.get("image/png"):
            handle_image(png, "image/png", target_path=output_image)
        elif jpeg := data.get("image/jpeg"):
            handle_image(jpeg, "image/jpeg", target_path=output_image)
    elif out.get("output_type") == "error":
        tb = out.get("traceback", [])
        if tb:
            sys.stderr.write("".join(tb) + "\n")
        else:
            ename = out.get("ename", "Error")
            evalue = out.get("evalue", "")
            sys.stderr.write(f"{ename}: {evalue}\n")
    else:
        # Ignore silent outputs like metadata or clear_output for streaming
        pass


def _durable_session_name(client, requested: str | None) -> str:
    if requested:
        return requested
    sessions = client.list_sessions().sessions
    if len(sessions) == 1:
        return sessions[0].name
    if not sessions:
        typer.echo("[colab] Error: No active sessions found.", err=True)
    else:
        typer.echo(
            "[colab] Error: Multiple active sessions found. Specify one with -s.",
            err=True,
        )
    raise typer.Exit(1)


def _render_durable_page(
    client,
    page: OutputPage,
    output_image: str | None,
    *,
    suppress_error_names: set[str] | None = None,
) -> None:
    current = page
    while True:
        for event in current.events:
            if event.text:
                stream = (
                    sys.stderr if event.stream == "stderr" else sys.stdout
                )
                stream.write(event.text)
                stream.flush()
            suppressed_error = (
                event.event_type == "error"
                and suppress_error_names is not None
                and event.error_name in suppress_error_names
            )
            if (
                event.event_type == "error"
                and event.traceback
                and not suppressed_error
            ):
                sys.stderr.write("".join(event.traceback) + "\n")
            if (
                output_image is not None
                and event.artifact is not None
                and event.mime_type in {"image/png", "image/jpeg"}
            ):
                shutil.copyfile(event.artifact.path, output_image)
        if not current.has_more or current.next_cursor is None:
            return
        current = client.execution_output(
            current.execution_id,
            cursor=current.next_cursor,
        )


def _render_durable_execution(client, result, output_image: str | None) -> None:
    _render_durable_page(client, result.output, output_image)
    if result.state in FAILED_EXECUTION_STATES and result.error_name:
        typer.echo(
            f"{result.error_name}: {result.error_value or ''}",
            err=True,
        )


def _durable_source_execution(
    *,
    session: str | None,
    source: str,
    provenance: dict,
    timeout: float | None,
    output_image: str | None,
) -> None:
    if not source.strip():
        return
    with _client_from_cli_state() as client:
        name = _durable_session_name(client, session)
        result = client.start_execution(
            session=name,
            source=source,
            provenance=provenance,
            execution_timeout=timeout,
        )
        _render_durable_execution(client, result, output_image)
    if result.state in FAILED_EXECUTION_STATES:
        raise typer.Exit(1)


def _durable_notebook_execution(
    *,
    session: str | None,
    path: str,
    output_image: str | None,
    write_output: bool,
) -> None:
    source_path = os.path.realpath(path)
    output_path = os.path.splitext(source_path)[0] + "_output.ipynb"
    execution_path = source_path
    if write_output:
        shutil.copyfile(source_path, output_path)
        execution_path = output_path

    notebook = nbformat.read(execution_path, as_version=4)
    indexes = [
        index
        for index, cell in enumerate(notebook.cells)
        if cell.cell_type == "code"
    ]
    if not indexes:
        return
    with _client_from_cli_state() as client:
        name = _durable_session_name(client, session)
        batch = client.start_batch(
            session=name,
            notebook=execution_path,
            cell_indexes=indexes,
            continue_on_error=True,
        )
        for execution in batch.executions:
            page = client.execution_output(execution.execution_id)
            _render_durable_page(client, page, output_image)
            if (
                write_output
                and execution.state
                in {ExecutionState.FINISHED, ExecutionState.ERROR}
                and execution.output_complete
            ):
                client.write_notebook_output(execution.execution_id)
    if write_output:
        typer.echo(
            f"[colab] Saved notebook outputs to '{output_path}'.",
            err=True,
        )
    if batch.state in {BatchState.ERROR, BatchState.INTERRUPTED}:
        raise typer.Exit(1)


def exec_command(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    file: Annotated[
        Optional[str], typer.Option("-f", "--file", help="File to execute")
    ] = None,
    output_image: Annotated[
        Optional[str], typer.Option("--output-image", help="Path to save plot")
    ] = None,
    timeout: Annotated[
        Optional[float],
        typer.Option("--timeout", help="Timeout in seconds for code execution"),
    ] = 30.0,
    write_output: Annotated[
        bool,
        typer.Option(
            "--write-output",
            help="Write an explicit *_output.ipynb copy",
        ),
    ] = False,
):
    """Execute code in a session"""
    from colab_cli.common import state

    if state.durable_wrappers:
        if file and file.endswith(".ipynb"):
            _durable_notebook_execution(
                session=session,
                path=file,
                output_image=output_image,
                write_output=write_output,
            )
            return
        if file:
            with open(file, encoding="utf-8") as source_file:
                source = source_file.read()
            provenance = {
                "kind": "file",
                "path": os.path.realpath(file),
            }
        else:
            if is_stdin_tty():
                typer.echo(
                    "[colab] Error: No input provided. "
                    "Pipe code or provide a file.",
                    err=True,
                )
                raise typer.Exit(1)
            source = sys.stdin.read()
            provenance = {"kind": "stdin"}
        _durable_source_execution(
            session=session,
            source=source,
            provenance=provenance,
            timeout=timeout,
            output_image=output_image,
        )
        return

    name = state.resolve_session(session)
    s = state.store.get(name)
    if not s:
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)

    code_blocks = []
    if file:
        if file.endswith(".ipynb"):
            typer.echo(f"[colab] Parsing notebook '{file}'...")
            with open(file, "r", encoding="utf-8") as f:
                nb = nbformat.read(f, as_version=4)
                for cell in nb.cells:
                    # nbformat v4.5+ requires 'id' at the top level
                    if not hasattr(cell, "id") or not cell.id:
                        cell.id = str(uuid.uuid4())

                    if cell.cell_type == "code":
                        code_blocks.append(
                            {"code": cell.source, "id": cell.id, "cell": cell}
                        )
        else:
            with open(file, "r") as f:
                code_blocks.append({"code": f.read(), "id": None})
    else:
        if is_stdin_tty():
            typer.echo("[colab] Error: No input provided. Pipe code or provide a file.")
            raise typer.Exit(1)
        code_blocks.append({"code": sys.stdin.read(), "id": None})

    if not any(b["code"].strip() for b in code_blocks):
        raise typer.Exit(0)

    def on_started(kid):
        s.kernel_id = kid
        state.store.add(s)

    def on_sess_started(sid):
        s.session_id = sid
        state.store.add(s)

    runtime = ColabRuntime(
        s.url,
        s.token,
        kernel_id=s.kernel_id,
        session_id=s.session_id,
        on_kernel_started=on_started,
        on_session_started=on_sess_started,
    )
    try:
        # Ensure we are in /content which is the standard Colab working directory
        runtime.execute_code(
            "import os; os.makedirs('/content', exist_ok=True); os.chdir('/content')"
        )
    except Exception as e:
        if is_terminal_error(e):
            typer.echo(
                f"[colab] Session '{name}' appears to be lost (404/401). Cleaning up."
            )
            state.prune_session(name)
            raise typer.Exit(1)
        raise e

    try:
        is_nb = file and file.endswith(".ipynb")
        s.running = f"exec({file or 'stdin'})"
        state.store.add(s)

        for i, block in enumerate(code_blocks):
            code = block["code"]
            identifier = None
            if is_nb:
                title_match = TITLE_REGEX.search(code)
                if title_match:
                    identifier = title_match.group(1).strip()
                elif block.get("id"):
                    identifier = block["id"]
                else:
                    identifier = ""

                identifier_str = f" - {identifier}" if identifier else ""
                typer.echo(
                    f"[colab] Executing cell {i + 1}/{len(code_blocks)}{identifier_str}..."
                )

            s.last_execution = (
                file or "stdin",
                identifier,
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            state.store.add(s)

            outputs = runtime.execute_code(
                code,
                output_hook=lambda o: display_output(o, output_image),
                timeout=timeout,
            )
            if "cell" in block:
                save_output(outputs, block["cell"])
            state.history.log_event(
                name,
                "execution",
                {
                    "code": code,
                    "outputs": outputs,
                    "cell_index": i if len(code_blocks) > 1 else None,
                    "cell_id": block.get("id"),
                },
            )
    finally:
        s.running = None
        state.store.add(s)
        runtime.stop()
        if write_output and file and file.endswith(".ipynb"):
            output_file = os.path.splitext(file)[0] + "_output.ipynb"
            typer.echo(f"[colab] Saving notebook with outputs to '{output_file}'...")
            with open(output_file, "w", encoding="utf-8") as f:
                nbformat.write(nb, f)


def repl(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    output_image: Annotated[
        Optional[str], typer.Option("--output-image", help="Path to save plot")
    ] = None,
):
    """Start an interactive REPL"""
    from colab_cli.common import state

    interactive = is_stdin_tty()
    if state.durable_wrappers and not interactive:
        _durable_source_execution(
            session=session,
            source=sys.stdin.read(),
            provenance={
                "kind": "stdin",
                "compatibility_command": "repl",
            },
            timeout=None,
            output_image=output_image,
        )
        return

    name = state.resolve_session(session)
    s = state.store.get(name)
    if not s:
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)

    lease = (
        compatibility_session_lease(name, endpoint=s.endpoint)
        if interactive
        else contextlib.nullcontext()
    )
    with lease:
        def on_started(kid):
            s.kernel_id = kid
            state.store.add(s)

        def on_sess_started(sid):
            s.session_id = sid
            state.store.add(s)

        runtime = ColabRuntime(
            s.url,
            s.token,
            kernel_id=s.kernel_id,
            session_id=s.session_id,
            on_kernel_started=on_started,
            on_session_started=on_sess_started,
        )
        try:
            # Ensure we are in /content, the standard Colab working directory.
            runtime.execute_code(
                "import os; os.makedirs('/content', exist_ok=True); "
                "os.chdir('/content')"
            )
        except Exception as e:
            if is_terminal_error(e):
                typer.echo(
                    f"[colab] Session '{name}' appears to be lost "
                    "(404/401). Cleaning up."
                )
                state.prune_session(name)
                raise typer.Exit(1)
            raise e

        if not interactive:
            code = sys.stdin.read()
            if not code.strip():
                runtime.stop()
                raise typer.Exit(0)

            s.last_execution = (
                "stdin",
                None,
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            s.running = "repl(stdin)"
            state.store.add(s)
            try:
                outputs = runtime.execute_code(
                    code,
                    output_hook=lambda o: display_output(o, output_image),
                )
                state.history.log_event(
                    name,
                    "execution",
                    {"code": code, "outputs": outputs, "source": "piped"},
                )
            finally:
                s.running = None
                state.store.add(s)
                runtime.stop()
        else:
            from colab_cli.repl import ColabREPL

            s.running = "repl"
            state.store.add(s)
            try:
                repl_inst = ColabREPL(
                    runtime,
                    session_name=s.name,
                    history_logger=state.history,
                    output_image=output_image,
                )
                state.history.log_event(name, "repl_started", {})
                repl_inst.run()
            finally:
                s.running = None
                state.store.add(s)


def console(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
):
    """Connect to raw TTY console"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.store.get(name)
    if not s:
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)
    with compatibility_session_lease(name, endpoint=s.endpoint):
        state.history.log_event(s.name, "console_started", {})
        s.running = "console"
        state.store.add(s)
        try:
            connect_console(s)
        except Exception as e:
            if is_terminal_error(e):
                typer.echo(
                    f"[colab] Session '{name}' appears to be lost "
                    "(404/401). Cleaning up."
                )
                state.prune_session(name)
                raise typer.Exit(1)
            raise e
        finally:
            s.running = None
            state.store.add(s)


def register(app: typer.Typer):
    app.command(name="exec")(exec_command)
    app.command()(repl)
    app.command()(console)
