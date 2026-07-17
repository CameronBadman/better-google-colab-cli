"""Stable public models shared by the Python API and JSON v1 CLI."""

from __future__ import annotations

from enum import Enum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field


class PublicModel(BaseModel):
    """Strict immutable base with compact wire serialization."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    wire_mandatory: ClassVar[tuple[str, ...]] = ()

    def to_wire(self) -> dict[str, Any]:
        data = self.model_dump(
            mode="json",
            exclude_none=True,
            exclude_defaults=True,
        )
        if self.wire_mandatory:
            complete = self.model_dump(mode="json")
            for field in self.wire_mandatory:
                data[field] = complete[field]
        return data


class ErrorDetail(PublicModel):
    code: str
    message: str
    retryable: bool
    suggested_action: str
    details: dict[str, Any] | None = None


class ExecutionState(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    FINISHED = "finished"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    TIMED_OUT = "timed_out"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"


class CompletionSource(str, Enum):
    LIVE = "live"
    RECOVERY = "recovery"
    DURABLE_EVIDENCE = "durable_evidence"


class HealthResult(PublicModel):
    """The seven health fields that are never omitted from wire output."""

    wire_mandatory: ClassVar[tuple[str, ...]] = (
        "controller_alive",
        "backend_alive",
        "kernel_connected",
        "kernel_execution_ready",
        "kernel_probe_at",
        "kernel_probe_latency_ms",
        "kernel_probe_error",
    )

    controller_alive: bool
    backend_alive: bool
    kernel_connected: bool
    kernel_execution_ready: bool
    kernel_probe_at: str | None
    kernel_probe_latency_ms: float | None
    kernel_probe_error: str | None


class Limits(PublicModel):
    response_bytes: int
    output_page_bytes: int
    output_page_min_bytes: int
    output_page_max_bytes: int
    execution_page_default: int
    notebook_cell_page_default: int
    collection_page_max: int
    controller_request_bytes: int


class ArgumentSpec(PublicModel):
    name: str
    type: str
    required: bool = False
    repeatable: bool = False
    default: Any | None = None
    choices: list[str] | None = None
    conflicts_with: list[str] | None = None


class CommandCapability(PublicModel):
    name: str
    summary: str
    arguments: list[ArgumentSpec] = Field(default_factory=list)


class CapabilitiesResult(PublicModel):
    schema_versions: list[int]
    internal_protocol_versions: list[int]
    limits: Limits
    commands: list[CommandCapability]
    next_cursor: str | None = None


class DoctorResult(HealthResult):
    schema_versions: list[int]
    package_version: str
    python_version: str
    platform: str
    state_path: str
    runtime_dir: str


class SessionSummary(PublicModel):
    name: str
    endpoint: str
    hardware: str
    variant: str
    status: str | None = None


class SessionHealthResult(HealthResult):
    name: str
    endpoint: str
    hardware: str
    variant: str


class SourceProvenance(PublicModel):
    kind: str
    sha256: str
    path: str | None = None
    notebook_id: str | None = None
    cell_id: str | None = None
    cell_index: int | None = None


class Artifact(PublicModel):
    path: str
    media_type: str
    byte_size: int
    sha256: str
    purpose: str | None = None


class OutputEvent(PublicModel):
    cursor: str
    event_type: str
    text: str | None = None
    stream: str | None = None
    mime_type: str | None = None
    artifact: Artifact | None = None
    display_id: str | None = None
    execution_count: int | None = None
    metadata: dict[str, Any] | None = None
    error_name: str | None = None
    error_value: str | None = None
    traceback: list[str] | None = None
    wait: bool | None = None


class OutputPage(PublicModel):
    execution_id: str
    events: list[OutputEvent]
    next_cursor: str | None = None
    has_more: bool = False
    output_complete: bool = True


class ExecutionSummary(PublicModel):
    execution_id: str
    session: str
    state: ExecutionState
    source_sha256: str
    output_complete: bool
    created_at: str
    updated_at: str
    idempotency_key: str | None = None
    completion_source: CompletionSource | None = None
    error_name: str | None = None
    error_value: str | None = None


class ExecutionTransitionSummary(PublicModel):
    from_state: ExecutionState | None = None
    to_state: ExecutionState
    reason: str | None = None
    evidence: dict[str, Any] | None = None
    created_at: str


class ExecutionResult(PublicModel):
    execution_id: str
    session: str
    state: ExecutionState
    source_sha256: str
    output_complete: bool
    dispatch_confirmed: bool = False
    reply_received: bool = False
    idle_received: bool = False
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None
    execution_deadline: str | None = None
    idempotency_key: str | None = None
    completion_source: CompletionSource | None = None
    error_name: str | None = None
    error_value: str | None = None
    provenance: SourceProvenance | None = None
    transitions: list[ExecutionTransitionSummary] | None = None
    traceback: list[str] | None = None


class ExecutionWaitResult(ExecutionResult):
    wait_timed_out: bool = False
    output: OutputPage


class ExecutionListResult(PublicModel):
    executions: list[ExecutionResult]
    next_cursor: str | None = None


class NotebookCellSummary(PublicModel):
    notebook_id: str
    index: int
    cell_type: str
    source_sha256: str
    cell_id: str | None = None


class NotebookCell(NotebookCellSummary):
    path: str
    notebook_sha256: str
    source: str


class NotebookCellsResult(PublicModel):
    notebook_id: str
    path: str
    notebook_sha256: str
    cells: list[NotebookCellSummary]
    next_cursor: str | None = None


class NotebookIdsResult(PublicModel):
    notebook_id: str
    path: str
    notebook_sha256: str
    assigned: list[str]


class NotebookWriteResult(PublicModel):
    execution_id: str
    notebook_id: str
    path: str
    cell_id: str
    notebook_sha256: str
    outputs_written: int


class PruneResult(PublicModel):
    dry_run: bool
    matched: int
    deleted: int
    execution_ids: list[str]
    artifact_bytes: int


class ControllerStatus(PublicModel):
    controller_alive: bool
    pid: int | None = None
    protocol_version: int | None = None
    started_at: str | None = None
    active_executions: int | None = None


class ControllerStopResult(PublicModel):
    stopping: bool
    forced: bool = False
    affected: list[str] = Field(default_factory=list)
    controller_alive: bool
