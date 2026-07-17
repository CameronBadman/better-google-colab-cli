"""Synchronous public facade for Better Colab."""

from __future__ import annotations

import hashlib
import os
import platform
import sys
import uuid
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from better_colab.capabilities import get_capabilities
from better_colab.controller_client import ControllerClient
from better_colab.errors import BetterColabError, ExitCode, api_error
from better_colab.models import (
    CapabilitiesResult,
    ControllerStatus,
    ControllerStopResult,
    DoctorResult,
    ExecutionListResult,
    ExecutionResult,
    ExecutionWaitResult,
    NotebookCell,
    NotebookCellsResult,
    NotebookIdsResult,
    OutputPage,
    PruneResult,
    SessionHealthResult,
)
from better_colab.notebooks import NotebookDocument
from better_colab.protocol import (
    DEFAULT_EXECUTION_LIMIT,
    DEFAULT_NOTEBOOK_CELL_LIMIT,
    DEFAULT_OUTPUT_PAGE_BYTES,
    SCHEMA_VERSION,
)
from better_colab.storage import DurableStore, ProfileSpec, StatePaths


def _package_version() -> str:
    try:
        return version("better-google-colab-cli")
    except PackageNotFoundError:
        return "unknown"


class BetterColabClient:
    """Thread-compatible synchronous client with no terminal dependencies."""

    def __init__(
        self,
        *,
        config_path: str | os.PathLike[str] | None = None,
        auth_provider: str = "oauth2",
        oauth_config_path: str | os.PathLike[str] | None = None,
        paths: StatePaths | None = None,
    ):
        self.paths = paths or StatePaths.discover()
        self.profile = ProfileSpec.from_values(
            config_path=config_path,
            auth_provider=auth_provider,
            oauth_config_path=oauth_config_path,
        )
        self._store: DurableStore | None = None

    @property
    def store(self) -> DurableStore:
        if self._store is None:
            self._store = DurableStore(paths=self.paths, profile=self.profile)
        return self._store

    def capabilities(
        self,
        command: str | None = None,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_EXECUTION_LIMIT,
    ) -> CapabilitiesResult:
        return get_capabilities(command=command, cursor=cursor, limit=limit)

    def doctor(self) -> DoctorResult:
        runtime_dir = self.paths.runtime_dir
        controller_socket = runtime_dir / "controller.sock"
        return DoctorResult(
            controller_alive=controller_socket.is_socket(),
            backend_alive=False,
            kernel_connected=False,
            kernel_execution_ready=False,
            kernel_probe_at=None,
            kernel_probe_latency_ms=None,
            kernel_probe_error=None,
            schema_versions=[SCHEMA_VERSION],
            package_version=_package_version(),
            python_version=platform.python_version(),
            platform=sys.platform,
            state_path=str(self.paths.database),
            runtime_dir=str(runtime_dir),
        )

    def prune_executions(
        self,
        *,
        before: str,
        session: str | None = None,
        confirm: bool = False,
    ) -> PruneResult:
        return self.store.prune_executions(
            before=before,
            session_name=session,
            confirm=confirm,
        )

    def controller_status(self) -> ControllerStatus:
        controller = ControllerClient(paths=self.paths)
        try:
            result = controller.status()
        except BetterColabError as error:
            if error.error.code != "CONTROLLER_NOT_RUNNING":
                raise
            return ControllerStatus(controller_alive=False)
        return ControllerStatus.model_validate(result)

    def controller_start(self) -> ControllerStatus:
        result = ControllerClient(paths=self.paths).ensure_running()
        return ControllerStatus.model_validate(result)

    def controller_stop(
        self,
        *,
        force: bool = False,
        wait_timeout: float = 3,
    ) -> ControllerStopResult:
        controller = ControllerClient(paths=self.paths)
        try:
            result = controller.stop(force=force)
        except BetterColabError as error:
            if error.error.code != "CONTROLLER_NOT_RUNNING":
                raise
            return ControllerStopResult(
                stopping=False,
                forced=force,
                affected=[],
                controller_alive=False,
            )
        controller.wait_until_stopped(timeout=wait_timeout)
        return ControllerStopResult(
            **result,
            controller_alive=False,
        )

    def session_status(self, name: str) -> SessionHealthResult:
        result = ControllerClient(paths=self.paths).session_status(
            profile=self.profile,
            name=name,
        )
        return SessionHealthResult.model_validate(result)

    def session_probe(
        self,
        name: str,
        *,
        timeout: float = 10,
    ) -> SessionHealthResult:
        result = ControllerClient(paths=self.paths).session_probe(
            profile=self.profile,
            name=name,
            timeout=timeout,
        )
        return SessionHealthResult.model_validate(result)

    def start_execution(
        self,
        *,
        session: str,
        source: str | bytes,
        provenance: dict[str, Any],
        expected_source_sha256: str | None = None,
        idempotency_key: str | None = None,
        execution_timeout: float | None = None,
        detach: bool = False,
        wait_timeout: float | None = None,
    ) -> ExecutionResult | ExecutionWaitResult:
        if detach and wait_timeout is not None:
            raise api_error(
                "CONFLICTING_FLAGS",
                "detach and wait_timeout are mutually exclusive",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="choose_detach_or_wait_timeout",
            )
        if isinstance(source, bytes):
            try:
                source_text = source.decode("utf-8")
            except UnicodeDecodeError as error:
                raise api_error(
                    "SOURCE_NOT_UTF8",
                    "Execution source must be valid UTF-8",
                    exit_code=ExitCode.USAGE,
                    retryable=False,
                    suggested_action="provide_utf8_source",
                ) from error
        else:
            source_text = source
        digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        if expected_source_sha256 is not None:
            expected = expected_source_sha256.removeprefix("sha256:").lower()
            if expected != digest:
                raise api_error(
                    "SOURCE_HASH_MISMATCH",
                    "Source changed since it was inspected",
                    exit_code=ExitCode.CONFLICT,
                    retryable=False,
                    suggested_action="reinspect_source",
                    details={
                        "expected": f"sha256:{expected}",
                        "actual": f"sha256:{digest}",
                    },
                )
        controller = ControllerClient(paths=self.paths)
        queued = controller.start_execution(
            profile=self.profile,
            execution_id=str(uuid.uuid4()),
            session=session,
            source=source_text,
            provenance=provenance,
            idempotency_key=idempotency_key,
            execution_timeout=execution_timeout,
        )
        result = ExecutionResult.model_validate(queued)
        if detach:
            return result
        return self.wait_execution(
            result.execution_id,
            timeout=wait_timeout,
        )

    def execution_status(
        self,
        execution_id: str,
        *,
        include: list[str] | None = None,
    ) -> ExecutionResult:
        result = ControllerClient(paths=self.paths).execution_status(
            profile=self.profile,
            execution_id=execution_id,
            include=include,
        )
        return ExecutionResult.model_validate(result)

    def wait_execution(
        self,
        execution_id: str,
        *,
        timeout: float | None = None,
        cursor: str | None = None,
        max_bytes: int = DEFAULT_OUTPUT_PAGE_BYTES,
    ) -> ExecutionWaitResult:
        result = ControllerClient(paths=self.paths).wait_execution(
            profile=self.profile,
            execution_id=execution_id,
            timeout=timeout,
            cursor=cursor,
            max_bytes=max_bytes,
        )
        return ExecutionWaitResult.model_validate(result)

    def execution_output(
        self,
        execution_id: str,
        *,
        cursor: str | None = None,
        max_bytes: int = DEFAULT_OUTPUT_PAGE_BYTES,
    ) -> OutputPage:
        result = ControllerClient(paths=self.paths).execution_output(
            profile=self.profile,
            execution_id=execution_id,
            cursor=cursor,
            max_bytes=max_bytes,
        )
        return OutputPage.model_validate(result)

    def cancel_execution(self, execution_id: str) -> ExecutionResult:
        result = ControllerClient(paths=self.paths).cancel_execution(
            profile=self.profile,
            execution_id=execution_id,
        )
        return ExecutionResult.model_validate(result)

    def list_executions(
        self,
        *,
        session: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_EXECUTION_LIMIT,
    ) -> ExecutionListResult:
        result = ControllerClient(paths=self.paths).list_executions(
            profile=self.profile,
            session=session,
            cursor=cursor,
            limit=limit,
        )
        return ExecutionListResult.model_validate(result)

    def notebook_cells(
        self,
        path: str | os.PathLike[str],
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_NOTEBOOK_CELL_LIMIT,
    ) -> NotebookCellsResult:
        return NotebookDocument(path).cells(cursor=cursor, limit=limit)

    def notebook_cell(
        self,
        path: str | os.PathLike[str],
        *,
        cell_id: str | None = None,
        index: int | None = None,
    ) -> NotebookCell:
        return NotebookDocument(path).cell(cell_id=cell_id, index=index)

    def update_notebook_cell(
        self,
        path: str | os.PathLike[str],
        *,
        source: str,
        cell_id: str | None = None,
        index: int | None = None,
        expected_sha256: str | None = None,
    ) -> NotebookCell:
        return NotebookDocument(path).update_source(
            source=source,
            cell_id=cell_id,
            index=index,
            expected_sha256=expected_sha256,
        )

    def assign_notebook_ids(
        self,
        path: str | os.PathLike[str],
        *,
        expected_notebook_sha256: str,
    ) -> NotebookIdsResult:
        return NotebookDocument(path).assign_ids(
            expected_notebook_sha256=expected_notebook_sha256,
        )

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def __enter__(self) -> BetterColabClient:
        return self

    def __exit__(self, *_args) -> None:
        self.close()
