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

import click
import hashlib
import os
import tempfile
import typer
from typing import Optional
from typing_extensions import Annotated

from better_colab.errors import ExitCode
from better_colab.legacy import (
    emit_error as emit_json_error,
    emit_success as emit_json_success,
    resolve_session as resolve_json_session,
    wants_json,
)
from colab_cli.contents import ContentsClient


def ls(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    path: Annotated[str, typer.Argument(help="Remote path to list")] = "content",
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
):
    """List files in a session"""
    from colab_cli.common import state

    json_output = wants_json(output_format)
    name = (
        resolve_json_session(state, session)
        if json_output
        else state.resolve_session(session)
    )
    s = state.store.get(name)
    if not s:
        if json_output:
            emit_json_error(
                "SESSION_NOT_FOUND",
                f"Session '{name}' not found",
                exit_code=ExitCode.NOT_FOUND,
                retryable=False,
                suggested_action="list_sessions",
            )
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)
    contents = ContentsClient(s)
    try:
        data = contents.list_dir(path)
        state.history.log_event(name, "file_operation", {"op": "ls", "path": path})
        if data.get("type") == "directory":
            items = data.get("content", [])
            sorted_items = sorted(
                items, key=lambda x: (x.get("type") != "directory", x.get("name"))
            )
            if json_output:
                emit_json_success(
                    {
                        "session": name,
                        "path": path,
                        "type": "directory",
                        "items": [
                            {
                                key: item[key]
                                for key in ("name", "type", "size")
                                if item.get(key) is not None
                            }
                            for item in sorted_items
                        ],
                    }
                )
            else:
                for item in sorted_items:
                    suffix = "/" if item.get("type") == "directory" else ""
                    typer.echo(f"{item.get('name')}{suffix}")
        else:
            if json_output:
                emit_json_success(
                    {
                        "session": name,
                        "path": path,
                        "type": data.get("type", "file"),
                        "name": data.get("name"),
                        "size": data.get("size"),
                    }
                )
            else:
                typer.echo(data.get("name"))
    except Exception as e:
        if isinstance(e, typer.Exit):
            raise
        if json_output:
            emit_json_error(
                "FILE_OPERATION_FAILED",
                str(e),
                exit_code=ExitCode.UNAVAILABLE,
                retryable=True,
                suggested_action="retry_file_operation",
            )
        typer.echo(f"[colab] Error: {e}")
        raise typer.Exit(1)


def rm(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    path: Annotated[str, typer.Argument(help="Remote path to remove")] = ...,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
):
    """Remove a remote file"""
    from colab_cli.common import state

    json_output = wants_json(output_format)
    name = (
        resolve_json_session(state, session)
        if json_output
        else state.resolve_session(session)
    )
    s = state.store.get(name)
    if not s:
        if json_output:
            emit_json_error(
                "SESSION_NOT_FOUND",
                f"Session '{name}' not found",
                exit_code=ExitCode.NOT_FOUND,
                retryable=False,
                suggested_action="list_sessions",
            )
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)
    contents = ContentsClient(s)
    try:
        contents.rm(path)
        state.history.log_event(name, "file_operation", {"op": "rm", "path": path})
        if json_output:
            emit_json_success({"session": name, "path": path, "deleted": True})
        else:
            typer.echo(f"[colab] Deleted {path}")
    except Exception as e:
        if isinstance(e, typer.Exit):
            raise
        if json_output:
            emit_json_error(
                "FILE_OPERATION_FAILED",
                str(e),
                exit_code=ExitCode.UNAVAILABLE,
                retryable=True,
                suggested_action="retry_file_operation",
            )
        typer.echo(f"[colab] Error: {e}")
        raise typer.Exit(1)


def upload(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    local_path: Annotated[str, typer.Argument(help="Local file to upload")] = ...,
    remote_path: Annotated[str, typer.Argument(help="Remote path to upload to")] = ...,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
):
    """Upload a file to a session"""
    from colab_cli.common import state

    json_output = wants_json(output_format)
    name = (
        resolve_json_session(state, session)
        if json_output
        else state.resolve_session(session)
    )
    s = state.store.get(name)
    if not s:
        if json_output:
            emit_json_error(
                "SESSION_NOT_FOUND",
                f"Session '{name}' not found",
                exit_code=ExitCode.NOT_FOUND,
                retryable=False,
                suggested_action="list_sessions",
            )
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)
    if not os.path.isfile(local_path):
        if json_output:
            emit_json_error(
                "LOCAL_FILE_NOT_FOUND",
                f"Local file '{local_path}' not found",
                exit_code=ExitCode.NOT_FOUND,
                retryable=False,
                suggested_action="check_local_path",
            )
        typer.echo(f"[colab] Local file '{local_path}' not found.")
        raise typer.Exit(1)
    contents = ContentsClient(s)
    try:
        contents.upload(local_path, remote_path)
        state.history.log_event(
            name,
            "file_operation",
            {"op": "upload", "local": local_path, "remote": remote_path},
        )
        if json_output:
            emit_json_success(
                {
                    "session": name,
                    "local_path": local_path,
                    "remote_path": remote_path,
                    "uploaded": True,
                }
            )
        else:
            typer.echo(f"[colab] Uploaded '{local_path}' to '{remote_path}'")
    except Exception as e:
        if isinstance(e, typer.Exit):
            raise
        if json_output:
            emit_json_error(
                "UPLOAD_FAILED",
                str(e),
                exit_code=ExitCode.UNAVAILABLE,
                retryable=True,
                suggested_action="retry_upload",
            )
        typer.echo(f"[colab] Upload failed: {e}")
        raise typer.Exit(1)


def download(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    remote_path: Annotated[
        str, typer.Argument(help="Remote path to download from")
    ] = ...,
    local_path: Annotated[
        str, typer.Argument(help="Local path to save the file")
    ] = ...,
    output_format: Annotated[
        str,
        typer.Option("--format", help="Output format: text or json"),
    ] = "text",
):
    """Download a file from a session"""
    from colab_cli.common import state

    json_output = wants_json(output_format)
    name = (
        resolve_json_session(state, session)
        if json_output
        else state.resolve_session(session)
    )
    s = state.store.get(name)
    if not s:
        if json_output:
            emit_json_error(
                "SESSION_NOT_FOUND",
                f"Session '{name}' not found",
                exit_code=ExitCode.NOT_FOUND,
                retryable=False,
                suggested_action="list_sessions",
            )
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)
    contents = ContentsClient(s)
    try:
        contents.download(remote_path, local_path)
        state.history.log_event(
            name,
            "file_operation",
            {"op": "download", "remote": remote_path, "local": local_path},
        )
        if json_output:
            emit_json_success(
                {
                    "session": name,
                    "remote_path": remote_path,
                    "local_path": local_path,
                    "downloaded": True,
                }
            )
        else:
            typer.echo(f"[colab] Downloaded '{remote_path}' to '{local_path}'")
    except Exception as e:
        if isinstance(e, typer.Exit):
            raise
        if json_output:
            emit_json_error(
                "DOWNLOAD_FAILED",
                str(e),
                exit_code=ExitCode.UNAVAILABLE,
                retryable=True,
                suggested_action="retry_download",
            )
        typer.echo(f"[colab] Download failed: {e}")
        raise typer.Exit(1)


def edit(
    session: Annotated[
        Optional[str], typer.Option("-s", "--session", help="Session name")
    ] = None,
    remote_path: Annotated[str, typer.Argument(help="Remote path to edit")] = ...,
):
    """Edit a file on a running Colab session"""
    from colab_cli.common import state

    name = state.resolve_session(session)
    s = state.store.get(name)
    if not s:
        typer.echo(f"[colab] Session '{name}' not found.")
        raise typer.Exit(1)

    contents = ContentsClient(s)

    def get_file_hash(path):
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()

    _, ext = os.path.splitext(remote_path)

    with tempfile.NamedTemporaryFile(suffix=ext) as tf:
        local_path = tf.name

        try:
            contents.download(remote_path, local_path)
        except Exception:
            # If download fails, assume file doesn't exist and start empty
            pass

        hash_before = get_file_hash(local_path)

        click.edit(filename=local_path)

        hash_after = get_file_hash(local_path)

        if hash_after != hash_before:
            contents.upload(local_path, remote_path)
            state.history.log_event(
                name,
                "file_operation",
                {"op": "edit", "remote": remote_path},
            )
            typer.echo(f"[colab] Edited and uploaded '{remote_path}'")
        else:
            typer.echo(f"[colab] No changes made to '{remote_path}'")


def register(app: typer.Typer):
    app.command()(ls)
    app.command()(rm)
    app.command()(upload)
    app.command()(download)
    app.command()(edit)
