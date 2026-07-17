"""Guarded local notebook document operations implemented with nbformat."""

from __future__ import annotations

import copy
import hashlib
import os
import stat
import tempfile
import uuid
import warnings
from pathlib import Path
from typing import Any

import nbformat

from better_colab.errors import ExitCode, api_error
from better_colab.models import (
    NotebookCell,
    NotebookCellSummary,
    NotebookCellsResult,
    NotebookIdsResult,
)
from better_colab.protocol import (
    DEFAULT_NOTEBOOK_CELL_LIMIT,
    MAX_COLLECTION_LIMIT,
    decode_cursor,
    encode_cursor,
)


class NotebookDocument:
    """One canonical-path notebook snapshot reader."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve(strict=False)

    @property
    def notebook_id(self) -> str:
        return hashlib.sha256(str(self.path).encode("utf-8")).hexdigest()

    def _snapshot(self) -> tuple[Any, str]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError as error:
            raise api_error(
                "NOTEBOOK_NOT_FOUND",
                f"Notebook not found: {self.path}",
                exit_code=ExitCode.NOT_FOUND,
                retryable=False,
                suggested_action="check_notebook_path",
            ) from error
        except OSError as error:
            raise api_error(
                "NOTEBOOK_UNREADABLE",
                f"Could not read notebook: {self.path}",
                exit_code=ExitCode.UNAVAILABLE,
                retryable=False,
                suggested_action="check_notebook_permissions",
                details={"error": str(error)},
            ) from error
        try:
            text = raw.decode("utf-8")
            notebook = nbformat.reader.reads(text)
            notebook = nbformat.convert(notebook, 4)
            # Validate a copy because nbformat's compatibility validator may
            # repair missing/duplicate IDs in place. Reads must never mutate
            # the caller-visible snapshot.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                nbformat.validate(copy.deepcopy(notebook))
        except (UnicodeDecodeError, ValueError, nbformat.ValidationError) as error:
            raise api_error(
                "NOTEBOOK_INVALID",
                f"Notebook is not valid nbformat JSON: {self.path}",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="repair_notebook",
                details={"error": str(error)},
            ) from error
        return notebook, hashlib.sha256(raw).hexdigest()

    def _summary(self, cell: Any, index: int) -> NotebookCellSummary:
        source = str(cell.get("source", ""))
        cell_id = cell.get("id")
        return NotebookCellSummary(
            notebook_id=self.notebook_id,
            index=index,
            cell_type=str(cell.get("cell_type") or ""),
            cell_id=cell_id if isinstance(cell_id, str) and cell_id else None,
            source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _duplicate_ids(notebook: Any) -> set[str]:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for cell in notebook.cells:
            cell_id = cell.get("id")
            if not isinstance(cell_id, str) or not cell_id:
                continue
            if cell_id in seen:
                duplicates.add(cell_id)
            seen.add(cell_id)
        return duplicates

    def _require_unique_ids(self, notebook: Any) -> None:
        duplicates = self._duplicate_ids(notebook)
        if duplicates:
            duplicate = sorted(duplicates)[0]
            raise api_error(
                "DUPLICATE_CELL_ID",
                f"Cell ID is not unique in this notebook: {duplicate}",
                exit_code=ExitCode.CONFLICT,
                retryable=False,
                suggested_action="repair_duplicate_cell_ids",
            )

    @staticmethod
    def _missing_id_indexes(notebook: Any) -> list[int]:
        return [
            index
            for index, cell in enumerate(notebook.cells)
            if not isinstance(cell.get("id"), str) or not cell.get("id")
        ]

    def _select_index(
        self,
        notebook: Any,
        *,
        cell_id: str | None,
        index: int | None,
    ) -> int:
        if cell_id is not None and index is not None:
            raise api_error(
                "CONFLICTING_CELL_SELECTOR",
                "cell_id and index are mutually exclusive",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="choose_one_cell_selector",
            )
        if cell_id is None and index is None:
            raise api_error(
                "CELL_SELECTOR_REQUIRED",
                "cell_id or index is required",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="specify_cell_selector",
            )
        if cell_id is not None:
            matches = [
                position
                for position, cell in enumerate(notebook.cells)
                if cell.get("id") == cell_id
            ]
            if len(matches) > 1:
                raise api_error(
                    "DUPLICATE_CELL_ID",
                    f"Cell ID is not unique in this notebook: {cell_id}",
                    exit_code=ExitCode.CONFLICT,
                    retryable=False,
                    suggested_action="repair_duplicate_cell_ids",
                )
            if not matches:
                raise api_error(
                    "CELL_NOT_FOUND",
                    f"Cell not found in notebook: {cell_id}",
                    exit_code=ExitCode.NOT_FOUND,
                    retryable=False,
                    suggested_action="list_notebook_cells",
                )
            return matches[0]
        assert index is not None
        if index < 0 or index >= len(notebook.cells):
            raise api_error(
                "CELL_NOT_FOUND",
                f"Cell index is outside the notebook: {index}",
                exit_code=ExitCode.NOT_FOUND,
                retryable=False,
                suggested_action="list_notebook_cells",
            )
        return index

    def _atomic_write(self, notebook: Any) -> str:
        try:
            serialized = nbformat.writes(notebook, version=4).encode("utf-8")
            original_mode = stat.S_IMODE(self.path.stat().st_mode)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                dir=self.path.parent,
            )
            temporary_path = Path(temporary)
            try:
                os.fchmod(descriptor, original_mode)
                with os.fdopen(descriptor, "wb") as file:
                    descriptor = -1
                    file.write(serialized)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary_path, self.path)
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except BaseException:
                if descriptor >= 0:
                    os.close(descriptor)
                temporary_path.unlink(missing_ok=True)
                raise
        except (OSError, ValueError, nbformat.ValidationError) as error:
            raise api_error(
                "NOTEBOOK_WRITE_FAILED",
                f"Could not atomically update notebook: {self.path}",
                exit_code=ExitCode.UNAVAILABLE,
                retryable=False,
                suggested_action="check_notebook_permissions",
                details={"error": str(error)},
            ) from error
        return hashlib.sha256(serialized).hexdigest()

    def cells(
        self,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_NOTEBOOK_CELL_LIMIT,
    ) -> NotebookCellsResult:
        if limit < 1 or limit > MAX_COLLECTION_LIMIT:
            raise api_error(
                "INVALID_LIMIT",
                f"limit must be between 1 and {MAX_COLLECTION_LIMIT}",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="use_a_supported_limit",
            )
        try:
            offset = decode_cursor(cursor)
        except ValueError as error:
            raise api_error(
                "INVALID_CURSOR",
                "invalid notebook cell cursor",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="restart_pagination",
            ) from error
        notebook, notebook_sha256 = self._snapshot()
        if offset > len(notebook.cells):
            raise api_error(
                "INVALID_CURSOR",
                "notebook cell cursor is beyond the collection",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="restart_pagination",
            )
        selected = notebook.cells[offset : offset + limit]
        next_offset = offset + len(selected)
        return NotebookCellsResult(
            notebook_id=self.notebook_id,
            path=str(self.path),
            notebook_sha256=notebook_sha256,
            cells=[
                self._summary(cell, offset + position)
                for position, cell in enumerate(selected)
            ],
            next_cursor=(
                encode_cursor(next_offset)
                if next_offset < len(notebook.cells)
                else None
            ),
        )

    def cell(
        self,
        *,
        cell_id: str | None = None,
        index: int | None = None,
    ) -> NotebookCell:
        notebook, notebook_sha256 = self._snapshot()
        selected_index = self._select_index(
            notebook,
            cell_id=cell_id,
            index=index,
        )
        selected = notebook.cells[selected_index]
        summary = self._summary(selected, selected_index)
        return NotebookCell(
            **summary.model_dump(),
            path=str(self.path),
            notebook_sha256=notebook_sha256,
            source=str(selected.get("source", "")),
        )

    def update_source(
        self,
        *,
        source: str,
        cell_id: str | None = None,
        index: int | None = None,
        expected_sha256: str | None = None,
    ) -> NotebookCell:
        notebook, _notebook_sha256 = self._snapshot()
        self._require_unique_ids(notebook)
        if self._missing_id_indexes(notebook):
            raise api_error(
                "MISSING_CELL_IDS",
                "Assign missing cell IDs before mutating notebook cells",
                exit_code=ExitCode.CONFLICT,
                retryable=False,
                suggested_action="assign_notebook_ids",
            )
        selected_index = self._select_index(
            notebook,
            cell_id=cell_id,
            index=index,
        )
        current_source = str(notebook.cells[selected_index].get("source", ""))
        current_hash = hashlib.sha256(current_source.encode("utf-8")).hexdigest()
        if expected_sha256 is not None:
            expected = expected_sha256.removeprefix("sha256:").lower()
            if expected != current_hash:
                raise api_error(
                    "SOURCE_HASH_MISMATCH",
                    "Cell source changed since it was inspected",
                    exit_code=ExitCode.CONFLICT,
                    retryable=False,
                    suggested_action="reinspect_cell",
                    details={
                        "expected": f"sha256:{expected}",
                        "actual": f"sha256:{current_hash}",
                    },
                )
        notebook.cells[selected_index]["source"] = source
        self._atomic_write(notebook)
        selected_id = notebook.cells[selected_index]["id"]
        return self.cell(cell_id=selected_id)

    def assign_ids(
        self,
        *,
        expected_notebook_sha256: str,
    ) -> NotebookIdsResult:
        notebook, notebook_sha256 = self._snapshot()
        expected = expected_notebook_sha256.removeprefix("sha256:").lower()
        if expected != notebook_sha256:
            raise api_error(
                "NOTEBOOK_HASH_MISMATCH",
                "Notebook changed since it was inspected",
                exit_code=ExitCode.CONFLICT,
                retryable=False,
                suggested_action="reinspect_notebook",
                details={
                    "expected": f"sha256:{expected}",
                    "actual": f"sha256:{notebook_sha256}",
                },
            )
        self._require_unique_ids(notebook)
        existing = {
            cell["id"]
            for cell in notebook.cells
            if isinstance(cell.get("id"), str) and cell.get("id")
        }
        assigned: list[str] = []
        for index in self._missing_id_indexes(notebook):
            while True:
                candidate = uuid.uuid4().hex
                if candidate not in existing:
                    break
            notebook.cells[index]["id"] = candidate
            existing.add(candidate)
            assigned.append(candidate)
        new_hash = (
            self._atomic_write(notebook)
            if assigned
            else notebook_sha256
        )
        return NotebookIdsResult(
            notebook_id=self.notebook_id,
            path=str(self.path),
            notebook_sha256=new_hash,
            assigned=assigned,
        )
