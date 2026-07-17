"""Compact, versioned capability discovery for shell-only consumers."""

from __future__ import annotations

from better_colab.errors import ExitCode, api_error
from better_colab.models import (
    ArgumentSpec,
    CapabilitiesResult,
    CommandCapability,
    Limits,
)
from better_colab.protocol import (
    DEFAULT_EXECUTION_LIMIT,
    DEFAULT_NOTEBOOK_CELL_LIMIT,
    DEFAULT_OUTPUT_PAGE_BYTES,
    INTERNAL_PROTOCOL_VERSION,
    MAX_COLLECTION_LIMIT,
    MAX_CONTROLLER_REQUEST_BYTES,
    MAX_OUTPUT_PAGE_BYTES,
    MAX_RESPONSE_BYTES,
    MIN_OUTPUT_PAGE_BYTES,
    SCHEMA_VERSION,
    page_items,
)


def _arg(
    name: str,
    type: str,
    *,
    required: bool = False,
    repeatable: bool = False,
    default=None,
    choices: list[str] | None = None,
    conflicts_with: list[str] | None = None,
) -> ArgumentSpec:
    return ArgumentSpec(
        name=name,
        type=type,
        required=required,
        repeatable=repeatable,
        default=default,
        choices=choices,
        conflicts_with=conflicts_with,
    )


COMMANDS: tuple[CommandCapability, ...] = (
    CommandCapability(
        name="capabilities",
        summary="Discover schema versions, limits, commands, and argument shapes.",
        arguments=[
            _arg("command", "string"),
            _arg("cursor", "cursor"),
            _arg("limit", "integer", default=DEFAULT_EXECUTION_LIMIT),
            _arg("format", "enum", default="text", choices=["text", "json"]),
        ],
    ),
    CommandCapability(
        name="controller start",
        summary="Elect and start the persistent per-user controller.",
        arguments=[
            _arg("format", "enum", default="text", choices=["text", "json"])
        ],
    ),
    CommandCapability(
        name="controller status",
        summary="Observe controller state without starting it.",
        arguments=[
            _arg("format", "enum", default="text", choices=["text", "json"])
        ],
    ),
    CommandCapability(
        name="controller stop",
        summary="Stop the controller; force records active work as uncertain.",
        arguments=[
            _arg("force", "boolean", default=False),
            _arg("format", "enum", default="text", choices=["text", "json"]),
        ],
    ),
    CommandCapability(
        name="doctor",
        summary="Inspect local controller and configuration health.",
        arguments=[_arg("format", "enum", default="text", choices=["text", "json"])],
    ),
    CommandCapability(
        name="execution batch cancel",
        summary="Request cancellation of a durable cell batch.",
        arguments=[_arg("batch_id", "uuid", required=True)],
    ),
    CommandCapability(
        name="execution batch start",
        summary="Queue selected notebook cells as one ordered durable batch.",
        arguments=[
            _arg("session", "string", required=True),
            _arg("notebook", "path", required=True),
            _arg("cell_id", "string", repeatable=True, conflicts_with=["cell_index"]),
            _arg(
                "cell_index", "integer", repeatable=True, conflicts_with=["cell_id"]
            ),
            _arg("continue_on_error", "boolean", default=False),
            _arg("detach", "boolean", default=False),
        ],
    ),
    CommandCapability(
        name="execution batch status",
        summary="Observe batch and child execution states.",
        arguments=[_arg("batch_id", "uuid", required=True)],
    ),
    CommandCapability(
        name="execution batch wait",
        summary="Wait for batch progress without polling.",
        arguments=[
            _arg("batch_id", "uuid", required=True),
            _arg("timeout", "seconds"),
        ],
    ),
    CommandCapability(
        name="execution cancel",
        summary="Cancel queued work or request a verified kernel interrupt.",
        arguments=[_arg("execution_id", "uuid", required=True)],
    ),
    CommandCapability(
        name="execution list",
        summary="List durable execution records.",
        arguments=[
            _arg("session", "string"),
            _arg("cursor", "cursor"),
            _arg("limit", "integer", default=DEFAULT_EXECUTION_LIMIT),
        ],
    ),
    CommandCapability(
        name="execution output",
        summary="Read one stable bounded output page.",
        arguments=[
            _arg("execution_id", "uuid", required=True),
            _arg("cursor", "cursor"),
            _arg("max_bytes", "integer", default=DEFAULT_OUTPUT_PAGE_BYTES),
        ],
    ),
    CommandCapability(
        name="execution prune",
        summary="Preview or confirm deletion of terminal execution data.",
        arguments=[
            _arg("before", "timestamp", required=True),
            _arg("session", "string"),
            _arg("dry_run", "boolean", default=True, conflicts_with=["confirm"]),
            _arg("confirm", "boolean", default=False, conflicts_with=["dry_run"]),
        ],
    ),
    CommandCapability(
        name="execution start",
        summary="Durably queue one exact source snapshot without allocating a VM.",
        arguments=[
            _arg("session", "string", required=True),
            _arg("file", "path", conflicts_with=["notebook", "stdin"]),
            _arg("notebook", "path", conflicts_with=["file", "stdin"]),
            _arg("cell_id", "string", conflicts_with=["cell_index"]),
            _arg("cell_index", "integer", conflicts_with=["cell_id"]),
            _arg("expected_source_sha256", "sha256"),
            _arg("idempotency_key", "string"),
            _arg("execution_timeout", "seconds"),
            _arg("detach", "boolean", default=False),
            _arg("wait_timeout", "seconds"),
        ],
    ),
    CommandCapability(
        name="execution status",
        summary="Observe one execution without changing its state.",
        arguments=[
            _arg("execution_id", "uuid", required=True),
            _arg(
                "include",
                "enum",
                repeatable=True,
                choices=["provenance", "transitions", "traceback"],
            ),
        ],
    ),
    CommandCapability(
        name="execution wait",
        summary="Condition-wait and include the first bounded output page.",
        arguments=[
            _arg("execution_id", "uuid", required=True),
            _arg("timeout", "seconds"),
            _arg("cursor", "cursor"),
            _arg("max_bytes", "integer", default=DEFAULT_OUTPUT_PAGE_BYTES),
        ],
    ),
    CommandCapability(
        name="notebook cell",
        summary="Inspect one path-namespaced cell with source.",
        arguments=[
            _arg("path", "path", required=True),
            _arg("cell_id", "string", conflicts_with=["index"]),
            _arg("index", "integer", conflicts_with=["cell_id"]),
        ],
    ),
    CommandCapability(
        name="notebook cells",
        summary="List notebook cell metadata without source or outputs.",
        arguments=[
            _arg("path", "path", required=True),
            _arg("cursor", "cursor"),
            _arg("limit", "integer", default=DEFAULT_NOTEBOOK_CELL_LIMIT),
        ],
    ),
    CommandCapability(
        name="notebook ids assign",
        summary="Explicitly assign missing cell IDs under a notebook hash guard.",
        arguments=[
            _arg("path", "path", required=True),
            _arg("expected_notebook_sha256", "sha256", required=True),
        ],
    ),
    CommandCapability(
        name="notebook update",
        summary="Atomically replace one cell source under an optional hash guard.",
        arguments=[
            _arg("path", "path", required=True),
            _arg("cell_id", "string", conflicts_with=["index"]),
            _arg("index", "integer", conflicts_with=["cell_id"]),
            _arg("file", "path", conflicts_with=["stdin"]),
            _arg("expected_sha256", "sha256"),
        ],
    ),
    CommandCapability(
        name="notebook write-output",
        summary="Explicitly write complete guarded execution output to a notebook.",
        arguments=[_arg("execution_id", "uuid", required=True)],
    ),
    CommandCapability(
        name="session ensure",
        summary="Return an existing named session or explicitly allocate it.",
        arguments=[
            _arg("name", "string", required=True),
            _arg("gpu", "string", conflicts_with=["tpu"]),
            _arg("tpu", "string", conflicts_with=["gpu"]),
        ],
    ),
    CommandCapability(
        name="session list",
        summary="List sessions in the active profile.",
    ),
    CommandCapability(
        name="session probe",
        summary="Perform a nonce-verified kernel readiness probe.",
        arguments=[_arg("name", "string", required=True)],
    ),
    CommandCapability(
        name="session status",
        summary="Observe one session without executing a readiness probe.",
        arguments=[_arg("name", "string", required=True)],
    ),
    CommandCapability(
        name="session stop",
        summary="Unassign one named session.",
        arguments=[_arg("name", "string", required=True)],
    ),
)

COMMAND_BY_NAME = {command.name: command for command in COMMANDS}


def normalize_command_name(value: str) -> str:
    return " ".join(value.strip().lower().replace(".", " ").split())


def get_capabilities(
    *,
    command: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_EXECUTION_LIMIT,
) -> CapabilitiesResult:
    if limit < 1 or limit > MAX_COLLECTION_LIMIT:
        raise api_error(
            "INVALID_LIMIT",
            f"limit must be between 1 and {MAX_COLLECTION_LIMIT}",
            exit_code=ExitCode.USAGE,
            retryable=False,
            suggested_action="use_a_supported_limit",
        )

    if command is not None:
        normalized = normalize_command_name(command)
        selected = COMMAND_BY_NAME.get(normalized)
        if selected is None:
            raise api_error(
                "COMMAND_NOT_FOUND",
                f"Unknown command: {command}",
                exit_code=ExitCode.NOT_FOUND,
                retryable=False,
                suggested_action="run_capabilities",
            )
        page = [selected]
        next_cursor = None
    else:
        try:
            page, next_cursor = page_items(COMMANDS, cursor=cursor, limit=limit)
        except ValueError as error:
            raise api_error(
                "INVALID_CURSOR",
                str(error),
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="restart_pagination",
            ) from error

    return CapabilitiesResult(
        schema_versions=[SCHEMA_VERSION],
        internal_protocol_versions=[INTERNAL_PROTOCOL_VERSION],
        limits=Limits(
            response_bytes=MAX_RESPONSE_BYTES,
            output_page_bytes=DEFAULT_OUTPUT_PAGE_BYTES,
            output_page_min_bytes=MIN_OUTPUT_PAGE_BYTES,
            output_page_max_bytes=MAX_OUTPUT_PAGE_BYTES,
            execution_page_default=DEFAULT_EXECUTION_LIMIT,
            notebook_cell_page_default=DEFAULT_NOTEBOOK_CELL_LIMIT,
            collection_page_max=MAX_COLLECTION_LIMIT,
            controller_request_bytes=MAX_CONTROLLER_REQUEST_BYTES,
        ),
        commands=page,
        next_cursor=next_cursor,
    )
