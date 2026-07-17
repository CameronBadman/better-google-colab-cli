import json
import hashlib

import nbformat
from typer.testing import CliRunner

from better_colab.cli import app
from better_colab.errors import ExitCode


runner = CliRunner()


def _notebook(path):
    notebook = nbformat.v4.new_notebook(
        cells=[
            nbformat.v4.new_markdown_cell("# Heading", id="heading"),
            nbformat.v4.new_code_cell("answer = 42", id="answer"),
        ]
    )
    nbformat.write(notebook, path)
    return path


def test_notebook_cells_cli_emits_compact_metadata_only(tmp_path):
    path = _notebook(tmp_path / "sample.ipynb")

    result = runner.invoke(
        app,
        ["notebook", "cells", str(path), "--limit", "1", "--format=json"],
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == ExitCode.OK
    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    assert len(payload["result"]["cells"]) == 1
    assert payload["result"]["next_cursor"]
    assert "source" not in payload["result"]["cells"][0]
    assert "outputs" not in payload["result"]["cells"][0]


def test_notebook_cell_cli_returns_source_without_outputs(tmp_path):
    path = _notebook(tmp_path / "sample.ipynb")

    result = runner.invoke(
        app,
        [
            "notebook",
            "cell",
            str(path),
            "--cell-id",
            "answer",
            "--format=json",
        ],
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == ExitCode.OK
    assert payload["result"]["cell_id"] == "answer"
    assert payload["result"]["source"] == "answer = 42"
    assert "outputs" not in payload["result"]


def test_notebook_cell_cli_rejects_conflicting_selector(tmp_path):
    path = _notebook(tmp_path / "sample.ipynb")

    result = runner.invoke(
        app,
        [
            "notebook",
            "cell",
            str(path),
            "--cell-id",
            "answer",
            "--index",
            "1",
            "--format=json",
        ],
    )

    assert result.exit_code == ExitCode.USAGE
    assert json.loads(result.stdout)["error"]["code"] == (
        "CONFLICTING_CELL_SELECTOR"
    )


def test_notebook_update_cli_reads_stdin_under_hash_guard(tmp_path):
    path = _notebook(tmp_path / "sample.ipynb")
    expected = hashlib.sha256(b"answer = 42").hexdigest()

    result = runner.invoke(
        app,
        [
            "notebook",
            "update",
            str(path),
            "--cell-id",
            "answer",
            "--expected-sha256",
            expected,
            "--format=json",
        ],
        input="answer = 43\n",
    )
    payload = json.loads(result.stdout)

    assert result.exit_code == ExitCode.OK
    assert payload["result"]["source"] == "answer = 43\n"
    assert nbformat.read(path, as_version=4).cells[1].source == "answer = 43\n"


def test_notebook_ids_assign_cli_is_not_an_implicit_read_mutation(tmp_path):
    path = _notebook(tmp_path / "sample.ipynb")
    notebook = nbformat.read(path, as_version=4)
    notebook.cells[0].pop("id")
    raw = json.dumps(notebook, ensure_ascii=False).encode()
    path.write_bytes(raw)
    expected = hashlib.sha256(raw).hexdigest()

    listed = runner.invoke(
        app,
        ["notebook", "cells", str(path), "--format=json"],
    )
    assert json.loads(listed.stdout)["result"]["cells"][0].get("cell_id") is None
    assert path.read_bytes() == raw

    assigned = runner.invoke(
        app,
        [
            "notebook",
            "ids",
            "assign",
            str(path),
            "--expected-notebook-sha256",
            expected,
            "--format=json",
        ],
    )
    payload = json.loads(assigned.stdout)

    assert assigned.exit_code == ExitCode.OK
    assert len(payload["result"]["assigned"]) == 1
    assert nbformat.read(path, as_version=4).cells[0].id
