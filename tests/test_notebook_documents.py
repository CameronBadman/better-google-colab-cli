import hashlib
import json
import shutil
import stat

import nbformat
import pytest

from better_colab import BetterColabClient, BetterColabError
from better_colab.notebooks import NotebookDocument
from better_colab.protocol import DEFAULT_NOTEBOOK_CELL_LIMIT


def _write_notebook(path, cells):
    notebook = nbformat.v4.new_notebook()
    notebook.cells = cells
    nbformat.write(notebook, path)
    return path


def _write_raw_notebook(path, cells):
    path.write_text(
        json.dumps(
            {
                "cells": cells,
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_notebook_identity_is_canonical_path_namespaced(tmp_path):
    first = _write_notebook(
        tmp_path / "first.ipynb",
        [
            nbformat.v4.new_code_cell(
                "print('same')\r\n",
                id="shared-cell",
                outputs=[
                    nbformat.v4.new_output(
                        "stream",
                        name="stdout",
                        text="old output\n",
                    )
                ],
            )
        ],
    )
    second = tmp_path / "second.ipynb"
    shutil.copyfile(first, second)

    first_cell = NotebookDocument(first).cell(cell_id="shared-cell")
    second_cell = NotebookDocument(second).cell(cell_id="shared-cell")

    assert first_cell.notebook_id != second_cell.notebook_id
    assert first_cell.cell_id == second_cell.cell_id == "shared-cell"
    assert first_cell.source == "print('same')\r\n"
    assert first_cell.source_sha256 == hashlib.sha256(
        first_cell.source.encode("utf-8")
    ).hexdigest()
    assert "outputs" not in first_cell.to_wire()


def test_cell_lists_are_bounded_and_exclude_source_and_outputs(tmp_path):
    path = _write_notebook(
        tmp_path / "many.ipynb",
        [
            nbformat.v4.new_code_cell(
                f"value_{index} = {index}",
                id=f"cell-{index}",
            )
            for index in range(DEFAULT_NOTEBOOK_CELL_LIMIT + 1)
        ],
    )
    document = NotebookDocument(path)

    first = document.cells()
    repeated = document.cells()
    second = document.cells(cursor=first.next_cursor)

    assert first == repeated
    assert len(first.cells) == DEFAULT_NOTEBOOK_CELL_LIMIT
    assert first.next_cursor is not None
    assert len(second.cells) == 1
    assert second.next_cursor is None
    assert all("source" not in item.to_wire() for item in first.cells)
    assert all("outputs" not in item.to_wire() for item in first.cells)

    with BetterColabClient() as client:
        typed = client.notebook_cells(path, limit=1)
        inspected = client.notebook_cell(path, cell_id="cell-0")
    assert len(typed.cells) == 1
    assert inspected.source == "value_0 = 0"


def test_missing_cell_ids_are_observed_without_mutating_the_file(tmp_path):
    path = _write_raw_notebook(
        tmp_path / "missing-id.ipynb",
        [
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": "x = 1",
            }
        ],
    )
    before = path.read_bytes()

    result = NotebookDocument(path).cells()

    assert result.cells[0].cell_id is None
    assert path.read_bytes() == before


def test_duplicate_ids_make_id_selection_ambiguous_but_index_is_safe(tmp_path):
    path = _write_raw_notebook(
        tmp_path / "duplicate.ipynb",
        [
            {
                "cell_type": "code",
                "execution_count": None,
                "id": "duplicate",
                "metadata": {},
                "outputs": [],
                "source": "first = 1",
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "id": "duplicate",
                "metadata": {},
                "outputs": [],
                "source": "second = 2",
            },
        ],
    )
    document = NotebookDocument(path)

    with pytest.raises(BetterColabError) as error:
        document.cell(cell_id="duplicate")

    assert error.value.error.code == "DUPLICATE_CELL_ID"
    assert document.cell(index=1).source == "second = 2"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({}, "CELL_SELECTOR_REQUIRED"),
        ({"cell_id": "one", "index": 0}, "CONFLICTING_CELL_SELECTOR"),
        ({"cell_id": "missing"}, "CELL_NOT_FOUND"),
        ({"index": 9}, "CELL_NOT_FOUND"),
    ],
)
def test_cell_selection_errors_are_stable(tmp_path, kwargs, code):
    path = _write_notebook(
        tmp_path / "one.ipynb",
        [nbformat.v4.new_code_cell("pass", id="one")],
    )

    with pytest.raises(BetterColabError) as error:
        NotebookDocument(path).cell(**kwargs)

    assert error.value.error.code == code


def test_guarded_source_update_is_atomic_and_preserves_other_cells(tmp_path):
    path = _write_notebook(
        tmp_path / "update.ipynb",
        [
            nbformat.v4.new_markdown_cell("keep me", id="markdown"),
            nbformat.v4.new_code_cell(
                "old = True",
                id="target",
                outputs=[
                    nbformat.v4.new_output(
                        "stream",
                        name="stdout",
                        text="old\n",
                    )
                ],
            ),
        ],
    )
    path.chmod(0o640)
    document = NotebookDocument(path)
    inspected = document.cell(cell_id="target")

    with BetterColabClient() as client:
        updated = client.update_notebook_cell(
            path,
            cell_id="target",
            source="new = '🙂'\r\n",
            expected_sha256=inspected.source_sha256,
        )
    notebook = nbformat.read(path, as_version=4)

    assert updated.source == "new = '🙂'\r\n"
    assert updated.source_sha256 != inspected.source_sha256
    assert notebook.cells[0].source == "keep me"
    assert notebook.cells[1].outputs[0].text == "old\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_stale_source_hash_rejects_update_without_touching_file(tmp_path):
    path = _write_notebook(
        tmp_path / "stale.ipynb",
        [nbformat.v4.new_code_cell("current = 1", id="target")],
    )
    before = path.read_bytes()

    with pytest.raises(BetterColabError) as error:
        NotebookDocument(path).update_source(
            cell_id="target",
            source="stale = 2",
            expected_sha256="0" * 64,
        )

    assert error.value.error.code == "SOURCE_HASH_MISMATCH"
    assert path.read_bytes() == before


def test_atomic_write_failure_keeps_original_notebook(tmp_path, mocker):
    path = _write_notebook(
        tmp_path / "atomic.ipynb",
        [nbformat.v4.new_code_cell("original = 1", id="target")],
    )
    before = path.read_bytes()
    mocker.patch("better_colab.notebooks.os.replace", side_effect=OSError("boom"))

    with pytest.raises(BetterColabError) as error:
        NotebookDocument(path).update_source(
            cell_id="target",
            source="replacement = 2",
        )

    assert error.value.error.code == "NOTEBOOK_WRITE_FAILED"
    assert path.read_bytes() == before
    assert list(tmp_path.glob(".atomic.ipynb.*")) == []


def test_ids_assign_requires_notebook_hash_and_only_fills_missing_ids(tmp_path):
    path = _write_raw_notebook(
        tmp_path / "ids.ipynb",
        [
            {
                "cell_type": "markdown",
                "id": "existing",
                "metadata": {},
                "source": "keep",
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": "missing = 1",
            },
        ],
    )
    document = NotebookDocument(path)
    before = document.cells()

    with pytest.raises(BetterColabError) as mismatch:
        document.assign_ids(expected_notebook_sha256="f" * 64)
    assert mismatch.value.error.code == "NOTEBOOK_HASH_MISMATCH"

    with BetterColabClient() as client:
        assigned = client.assign_notebook_ids(
            path,
            expected_notebook_sha256=before.notebook_sha256,
        )
    notebook = nbformat.read(path, as_version=4)

    assert assigned.assigned == [notebook.cells[1].id]
    assert notebook.cells[0].id == "existing"
    assert notebook.cells[1].id
    assert len({cell.id for cell in notebook.cells}) == 2
    assert assigned.notebook_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_mutations_reject_duplicate_or_missing_ids_outside_explicit_assignment(
    tmp_path,
):
    duplicate = _write_raw_notebook(
        tmp_path / "duplicate-mutation.ipynb",
        [
            {
                "cell_type": "code",
                "execution_count": None,
                "id": "same",
                "metadata": {},
                "outputs": [],
                "source": "first",
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "id": "same",
                "metadata": {},
                "outputs": [],
                "source": "second",
            },
        ],
    )
    missing = _write_raw_notebook(
        tmp_path / "missing-mutation.ipynb",
        [
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": "missing",
            }
        ],
    )

    with pytest.raises(BetterColabError) as duplicate_error:
        NotebookDocument(duplicate).assign_ids(
            expected_notebook_sha256=hashlib.sha256(
                duplicate.read_bytes()
            ).hexdigest()
        )
    with pytest.raises(BetterColabError) as missing_error:
        NotebookDocument(missing).update_source(index=0, source="changed")

    assert duplicate_error.value.error.code == "DUPLICATE_CELL_ID"
    assert missing_error.value.error.code == "MISSING_CELL_IDS"
