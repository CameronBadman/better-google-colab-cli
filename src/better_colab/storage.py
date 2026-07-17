"""SQLite-backed durable state and protected local artifact storage."""

from __future__ import annotations

import contextlib
import base64
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence


from better_colab.errors import ExitCode, api_error
from better_colab.models import (
    Artifact,
    ExecutionState,
    OutputEvent,
    OutputPage,
    PruneResult,
    PublicModel,
)
from better_colab.protocol import (
    MAX_OUTPUT_PAGE_BYTES,
    MIN_OUTPUT_PAGE_BYTES,
    decode_cursor,
    encode_cursor,
)


DATABASE_SCHEMA_VERSION = 3
OUTPUT_CHUNK_BYTES = 512
LARGE_MIME_ARTIFACT_BYTES = 32 * 1024
OUTPUT_ARTIFACT_THRESHOLD_BYTES = 64 * 1024
TERMINAL_STATES = frozenset(
    {
        ExecutionState.FINISHED,
        ExecutionState.ERROR,
        ExecutionState.INTERRUPTED,
        ExecutionState.TIMED_OUT,
        ExecutionState.UNKNOWN,
    }
)
ALLOWED_TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.CREATED: frozenset({ExecutionState.QUEUED}),
    ExecutionState.QUEUED: frozenset(
        {ExecutionState.DISPATCHING, ExecutionState.INTERRUPTED}
    ),
    ExecutionState.DISPATCHING: frozenset(
        {ExecutionState.RUNNING, ExecutionState.UNKNOWN}
    ),
    ExecutionState.RUNNING: frozenset(
        {
            ExecutionState.FINISHED,
            ExecutionState.ERROR,
            ExecutionState.INTERRUPTED,
            ExecutionState.TIMED_OUT,
            ExecutionState.DISCONNECTED,
        }
    ),
    ExecutionState.DISCONNECTED: frozenset(
        {
            ExecutionState.RUNNING,
            ExecutionState.FINISHED,
            ExecutionState.ERROR,
            ExecutionState.INTERRUPTED,
            ExecutionState.TIMED_OUT,
            ExecutionState.UNKNOWN,
        }
    ),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise api_error(
            "INVALID_TIMESTAMP",
            "Timestamp must include a timezone",
            exit_code=ExitCode.USAGE,
            retryable=False,
            suggested_action="use_an_rfc3339_timestamp",
        )
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            _timestamp(value)
        return value
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise api_error(
            "INVALID_TIMESTAMP",
            f"Invalid timestamp: {value}",
            exit_code=ExitCode.USAGE,
            retryable=False,
            suggested_action="use_an_rfc3339_timestamp",
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _timestamp(parsed)
    return parsed


@dataclass(frozen=True)
class StatePaths:
    state_dir: Path
    runtime_dir: Path

    @classmethod
    def discover(cls) -> StatePaths:
        state_root = Path(
            os.environ.get(
                "XDG_STATE_HOME",
                Path.home() / ".local" / "state",
            )
        ).expanduser()
        configured_runtime = os.environ.get("XDG_RUNTIME_DIR")
        if configured_runtime:
            runtime_dir = Path(configured_runtime).expanduser() / "better-colab"
        else:
            temp_root = Path(
                os.environ.get("TMPDIR", tempfile.gettempdir())
            ).expanduser()
            runtime_dir = temp_root / f"better-colab-{os.getuid()}"
        return cls(
            state_dir=state_root / "better-colab",
            runtime_dir=runtime_dir,
        )

    @property
    def database(self) -> Path:
        return self.state_dir / "controller.sqlite3"

    @property
    def artifacts_dir(self) -> Path:
        return self.state_dir / "artifacts"

    @property
    def sources_dir(self) -> Path:
        return self.artifacts_dir / "sources"

    @property
    def outputs_dir(self) -> Path:
        return self.artifacts_dir / "output"

    @property
    def socket(self) -> Path:
        return self.runtime_dir / "controller.sock"

    @property
    def pid_file(self) -> Path:
        return self.runtime_dir / "controller.pid"

    @property
    def log_file(self) -> Path:
        return self.runtime_dir / "controller.log"

    @property
    def lifetime_lock(self) -> Path:
        return self.runtime_dir / "controller.lock"

    @property
    def startup_lock(self) -> Path:
        return self.runtime_dir / "startup.lock"

    def ensure(self) -> None:
        for directory in (
            self.state_dir,
            self.artifacts_dir,
            self.sources_dir,
            self.outputs_dir,
            self.runtime_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)

        descriptor = os.open(
            self.database,
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        os.close(descriptor)
        self.database.chmod(0o600)


@dataclass(frozen=True)
class ProfileSpec:
    profile_id: str
    config_path: Path
    auth_provider: str
    oauth_config_path: Path

    @classmethod
    def from_values(
        cls,
        *,
        config_path: str | os.PathLike[str] | None,
        auth_provider: str,
        oauth_config_path: str | os.PathLike[str] | None,
    ) -> ProfileSpec:
        resolved_config = Path(
            config_path
            or Path.home() / ".config" / "colab-cli" / "sessions.json"
        ).expanduser().resolve(strict=False)
        resolved_oauth = Path(
            oauth_config_path or Path.home() / ".colab-cli-oauth-config.json"
        ).expanduser().resolve(strict=False)
        normalized_provider = str(auth_provider).lower()
        canonical = json.dumps(
            {
                "config_path": str(resolved_config),
                "auth_provider": normalized_provider,
                "oauth_config_path": str(resolved_oauth),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return cls(
            profile_id=hashlib.sha256(canonical).hexdigest(),
            config_path=resolved_config,
            auth_provider=normalized_provider,
            oauth_config_path=resolved_oauth,
        )


class StorageDiagnostic(PublicModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class StoredSession(PublicModel):
    profile_id: str
    name: str
    endpoint: str
    backend_url: str
    runtime_token: str
    variant: str
    hardware: str
    kernel_id: str | None = None
    jupyter_session_id: str | None = None
    keep_alive_pid: int | None = None
    created_at: str
    updated_at: str
    stopped_at: str | None = None


class ExecutionRecord(PublicModel):
    execution_id: str
    profile_id: str
    session_name: str
    session_endpoint: str | None = None
    kernel_id_snapshot: str | None = None
    jupyter_session_id_snapshot: str | None = None
    idempotency_key: str | None = None
    request_hash: str
    source_kind: str
    source_path: str | None = None
    notebook_id: str | None = None
    cell_id: str | None = None
    cell_index: int | None = None
    source_sha256: str
    source_spool_path: str | None = None
    state: ExecutionState
    kernel_message_id: str | None = None
    dispatch_confirmed: bool
    reply_received: bool
    idle_received: bool
    completion_source: str | None = None
    output_complete: bool
    output_spool_path: str | None = None
    output_byte_size: int = 0
    output_sha256: str | None = None
    output_finalized_at: str | None = None
    execution_timeout_seconds: float | None = None
    execution_deadline: str | None = None
    cancel_requested: bool
    interrupt_requested_state: str | None = None
    reply_status: str | None = None
    reconnect_count: int
    error_name: str | None = None
    error_value: str | None = None
    traceback_json: str | None = None
    created_at: str
    queued_at: str
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str


class ExecutionTransition(PublicModel):
    transition_id: int
    execution_id: str
    from_state: ExecutionState | None
    to_state: ExecutionState
    reason: str | None = None
    evidence: dict[str, Any] | None = None
    created_at: str


class BatchRecord(PublicModel):
    batch_id: str
    profile_id: str
    session_name: str
    state: str
    continue_on_error: bool
    created_at: str
    updated_at: str
    completed_at: str | None = None


SCHEMA_V1 = """
CREATE TABLE profiles (
    profile_id TEXT PRIMARY KEY,
    config_path TEXT NOT NULL,
    auth_provider TEXT NOT NULL,
    oauth_config_path TEXT NOT NULL,
    legacy_import_hash TEXT,
    legacy_imported_at TEXT,
    legacy_observed_mtime_ns INTEGER,
    legacy_change_reported_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(config_path, auth_provider, oauth_config_path)
);

CREATE TABLE sessions (
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
    name TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    backend_url TEXT NOT NULL,
    runtime_token TEXT NOT NULL,
    variant TEXT NOT NULL,
    hardware TEXT NOT NULL,
    kernel_id TEXT,
    jupyter_session_id TEXT,
    keep_alive_pid INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    stopped_at TEXT,
    PRIMARY KEY(profile_id, name)
);
CREATE INDEX sessions_endpoint_idx ON sessions(profile_id, endpoint);

CREATE TABLE executions (
    execution_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
    session_name TEXT NOT NULL,
    session_endpoint TEXT,
    kernel_id_snapshot TEXT,
    jupyter_session_id_snapshot TEXT,
    idempotency_key TEXT,
    request_hash TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_path TEXT,
    notebook_id TEXT,
    cell_id TEXT,
    cell_index INTEGER,
    source_sha256 TEXT NOT NULL,
    source_spool_path TEXT,
    state TEXT NOT NULL,
    kernel_message_id TEXT,
    dispatch_confirmed INTEGER NOT NULL DEFAULT 0,
    reply_received INTEGER NOT NULL DEFAULT 0,
    idle_received INTEGER NOT NULL DEFAULT 0,
    completion_source TEXT,
    output_complete INTEGER NOT NULL DEFAULT 1,
    execution_deadline TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    reconnect_count INTEGER NOT NULL DEFAULT 0,
    error_name TEXT,
    error_value TEXT,
    traceback_json TEXT,
    created_at TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX executions_idempotency_idx
    ON executions(profile_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX executions_profile_state_idx
    ON executions(profile_id, state, updated_at, execution_id);
CREATE INDEX executions_session_idx
    ON executions(profile_id, session_name, created_at, execution_id);

CREATE TABLE execution_transitions (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL REFERENCES executions(execution_id)
        ON DELETE CASCADE,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT,
    evidence_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX execution_transitions_execution_idx
    ON execution_transitions(execution_id, transition_id);

CREATE TABLE execution_batches (
    batch_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
    session_name TEXT NOT NULL,
    state TEXT NOT NULL,
    continue_on_error INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE batch_members (
    batch_id TEXT NOT NULL REFERENCES execution_batches(batch_id)
        ON DELETE CASCADE,
    position INTEGER NOT NULL,
    execution_id TEXT NOT NULL REFERENCES executions(execution_id)
        ON DELETE CASCADE,
    PRIMARY KEY(batch_id, position),
    UNIQUE(batch_id, execution_id)
);

CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
    execution_id TEXT REFERENCES executions(execution_id) ON DELETE CASCADE,
    path TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    purpose TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX artifacts_execution_idx ON artifacts(execution_id, artifact_id);

CREATE TABLE output_chunks (
    execution_id TEXT NOT NULL REFERENCES executions(execution_id)
        ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    stream_name TEXT,
    mime_type TEXT,
    spool_offset INTEGER,
    byte_length INTEGER,
    artifact_id TEXT REFERENCES artifacts(artifact_id),
    display_id TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(execution_id, sequence)
);

CREATE TABLE kernel_connections (
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
    session_name TEXT NOT NULL,
    kernel_id TEXT NOT NULL,
    jupyter_session_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    connected_at TEXT NOT NULL,
    disconnected_at TEXT,
    readiness_nonce TEXT,
    readiness_checked_at TEXT,
    readiness_latency_ms REAL,
    readiness_error TEXT,
    PRIMARY KEY(profile_id, session_name)
);
"""

MIGRATION_2 = """
ALTER TABLE executions ADD COLUMN execution_timeout_seconds REAL;
ALTER TABLE executions ADD COLUMN interrupt_requested_state TEXT;
ALTER TABLE executions ADD COLUMN reply_status TEXT;
"""

MIGRATION_3 = """
ALTER TABLE executions ADD COLUMN output_spool_path TEXT;
ALTER TABLE executions ADD COLUMN output_byte_size INTEGER NOT NULL DEFAULT 0;
ALTER TABLE executions ADD COLUMN output_sha256 TEXT;
ALTER TABLE executions ADD COLUMN output_finalized_at TEXT;
"""


class DurableStore:
    """One profile view over the shared controller database."""

    def __init__(
        self,
        *,
        paths: StatePaths | None = None,
        profile: ProfileSpec | None = None,
        clock: Callable[[], datetime] = _now,
    ):
        self.paths = paths or StatePaths.discover()
        self.profile = profile or ProfileSpec.from_values(
            config_path=None,
            auth_provider="oauth2",
            oauth_config_path=None,
        )
        self._clock = clock
        self._lock = threading.RLock()
        self.diagnostics: list[StorageDiagnostic] = []
        self.paths.ensure()
        self.connection = sqlite3.connect(
            self.paths.database,
            timeout=5,
            isolation_level=None,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        self._configure()
        self._migrate()
        self._register_profile()
        self._import_legacy_state()

    @staticmethod
    def sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _configure(self) -> None:
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.paths.database.chmod(0o600)

    def _migrate(self) -> None:
        current = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if current > DATABASE_SCHEMA_VERSION:
            raise api_error(
                "DATABASE_VERSION_UNSUPPORTED",
                f"Database schema {current} is newer than supported "
                f"{DATABASE_SCHEMA_VERSION}",
                exit_code=ExitCode.CONFLICT,
                retryable=False,
                suggested_action="upgrade_better_colab",
            )
        if current == 0:
            self.connection.executescript(SCHEMA_V1)
            self.connection.execute("PRAGMA user_version=1")
            current = 1
        if current == 1:
            with self.transaction() as connection:
                for statement in MIGRATION_2.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute("PRAGMA user_version=2")
            current = 2
        if current == 2:
            with self.transaction() as connection:
                for statement in MIGRATION_3.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute("PRAGMA user_version=3")

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def _time(self) -> str:
        return _timestamp(self._clock())

    def _register_profile(self) -> None:
        now = self._time()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO profiles (
                    profile_id, config_path, auth_provider, oauth_config_path,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    updated_at = excluded.updated_at
                """,
                (
                    self.profile.profile_id,
                    str(self.profile.config_path),
                    self.profile.auth_provider,
                    str(self.profile.oauth_config_path),
                    now,
                    now,
                ),
            )

    def _import_legacy_state(self) -> None:
        path = self.profile.config_path
        if not path.is_file():
            return
        try:
            raw = path.read_bytes()
            digest = self.sha256(raw)
            mtime_ns = path.stat().st_mtime_ns
        except OSError as error:
            self.diagnostics.append(
                StorageDiagnostic(
                    code="LEGACY_STATE_UNREADABLE",
                    message=f"Could not read legacy state: {path}",
                    details={"error": str(error)},
                )
            )
            return

        row = self.connection.execute(
            """
            SELECT legacy_import_hash, legacy_imported_at
            FROM profiles WHERE profile_id = ?
            """,
            (self.profile.profile_id,),
        ).fetchone()
        imported_hash = row["legacy_import_hash"]
        if imported_hash is not None:
            if imported_hash != digest:
                self.diagnostics.append(
                    StorageDiagnostic(
                        code="LEGACY_STATE_CHANGED",
                        message=(
                            "Legacy sessions.json changed after its one-time "
                            "SQLite import; SQLite remains authoritative"
                        ),
                        details={"path": str(path)},
                    )
                )
                with self.transaction() as connection:
                    connection.execute(
                        """
                        UPDATE profiles SET legacy_change_reported_at = ?,
                            updated_at = ?
                        WHERE profile_id = ?
                        """,
                        (self._time(), self._time(), self.profile.profile_id),
                    )
            return

        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("top-level value must be an object")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            self.diagnostics.append(
                StorageDiagnostic(
                    code="LEGACY_STATE_INVALID",
                    message=f"Legacy state is not valid session JSON: {path}",
                    details={"error": str(error)},
                )
            )
            return

        imported_at = self._time()
        with self.transaction() as connection:
            for key, value in payload.items():
                if not isinstance(value, dict):
                    continue
                name = str(value.get("name") or key)
                connection.execute(
                    """
                    INSERT INTO sessions (
                        profile_id, name, endpoint, backend_url, runtime_token,
                        variant, hardware, kernel_id, jupyter_session_id,
                        keep_alive_pid, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(profile_id, name) DO NOTHING
                    """,
                    (
                        self.profile.profile_id,
                        name,
                        str(value.get("endpoint") or ""),
                        str(value.get("url") or ""),
                        str(value.get("token") or ""),
                        str(value.get("variant") or "DEFAULT"),
                        str(value.get("accelerator") or "NONE"),
                        value.get("kernel_id"),
                        value.get("session_id"),
                        value.get("keep_alive_pid"),
                        imported_at,
                        imported_at,
                    ),
                )
            connection.execute(
                """
                UPDATE profiles
                SET legacy_import_hash = ?, legacy_imported_at = ?,
                    legacy_observed_mtime_ns = ?, updated_at = ?
                WHERE profile_id = ?
                """,
                (
                    digest,
                    imported_at,
                    mtime_ns,
                    imported_at,
                    self.profile.profile_id,
                ),
            )

    def upsert_session(
        self,
        *,
        name: str,
        endpoint: str,
        backend_url: str,
        runtime_token: str,
        hardware: str,
        variant: str = "DEFAULT",
        kernel_id: str | None = None,
        jupyter_session_id: str | None = None,
        keep_alive_pid: int | None = None,
    ) -> StoredSession:
        now = self._time()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    profile_id, name, endpoint, backend_url, runtime_token,
                    variant, hardware, kernel_id, jupyter_session_id,
                    keep_alive_pid, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, name) DO UPDATE SET
                    endpoint = excluded.endpoint,
                    backend_url = excluded.backend_url,
                    runtime_token = excluded.runtime_token,
                    variant = excluded.variant,
                    hardware = excluded.hardware,
                    kernel_id = excluded.kernel_id,
                    jupyter_session_id = excluded.jupyter_session_id,
                    keep_alive_pid = excluded.keep_alive_pid,
                    updated_at = excluded.updated_at,
                    stopped_at = NULL
                """,
                (
                    self.profile.profile_id,
                    name,
                    endpoint,
                    backend_url,
                    runtime_token,
                    variant,
                    hardware,
                    kernel_id,
                    jupyter_session_id,
                    keep_alive_pid,
                    now,
                    now,
                ),
            )
        return self.get_session(name)

    def get_session(self, name: str) -> StoredSession | None:
        row = self.connection.execute(
            """
            SELECT profile_id, name, endpoint, backend_url, runtime_token,
                   variant, hardware, kernel_id, jupyter_session_id,
                   keep_alive_pid, created_at, updated_at, stopped_at
            FROM sessions WHERE profile_id = ? AND name = ?
            """,
            (self.profile.profile_id, name),
        ).fetchone()
        return StoredSession.model_validate(dict(row)) if row else None

    def list_sessions(self) -> list[StoredSession]:
        rows = self.connection.execute(
            """
            SELECT profile_id, name, endpoint, backend_url, runtime_token,
                   variant, hardware, kernel_id, jupyter_session_id,
                   keep_alive_pid, created_at, updated_at, stopped_at
            FROM sessions WHERE profile_id = ? ORDER BY name
            """,
            (self.profile.profile_id,),
        )
        return [StoredSession.model_validate(dict(row)) for row in rows]

    def update_session_connection(
        self,
        name: str,
        *,
        kernel_id: str,
        jupyter_session_id: str,
    ) -> StoredSession:
        with self.transaction() as connection:
            result = connection.execute(
                """
                UPDATE sessions SET kernel_id = ?, jupyter_session_id = ?,
                    updated_at = ?
                WHERE profile_id = ? AND name = ?
                """,
                (
                    kernel_id,
                    jupyter_session_id,
                    self._time(),
                    self.profile.profile_id,
                    name,
                ),
            )
            if result.rowcount != 1:
                raise api_error(
                    "SESSION_NOT_FOUND",
                    f"Session not found: {name}",
                    exit_code=ExitCode.NOT_FOUND,
                    retryable=False,
                    suggested_action="ensure_session",
                )
        return self.get_session(name)

    def delete_session(self, name: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE profile_id = ? AND name = ?",
                (self.profile.profile_id, name),
            )

    def _atomic_write(self, directory: Path, name: str, data: bytes) -> Path:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{name}.",
            dir=directory,
        )
        temporary_path = Path(temporary)
        destination = directory / name
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, destination)
            destination.chmod(0o600)
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            with contextlib.suppress(OSError):
                temporary_path.unlink()
            raise
        return destination

    @staticmethod
    def _request_hash(request: dict[str, Any]) -> str:
        canonical = json.dumps(
            request,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def create_execution(
        self,
        *,
        execution_id: str,
        session_name: str,
        source: bytes,
        provenance: dict[str, Any],
        request: dict[str, Any],
        idempotency_key: str | None = None,
        execution_timeout_seconds: float | None = None,
    ) -> ExecutionRecord:
        if execution_timeout_seconds is not None and (
            not math.isfinite(execution_timeout_seconds)
            or execution_timeout_seconds <= 0
        ):
            raise api_error(
                "INVALID_EXECUTION_TIMEOUT",
                "execution timeout must be a finite positive number",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="use_a_positive_timeout",
            )
        request_hash = self._request_hash(request)
        source_hash = self.sha256(source)
        source_path: Path | None = None
        try:
            with self.transaction() as connection:
                if idempotency_key is not None:
                    existing = connection.execute(
                        """
                        SELECT execution_id, request_hash FROM executions
                        WHERE profile_id = ? AND idempotency_key = ?
                        """,
                        (self.profile.profile_id, idempotency_key),
                    ).fetchone()
                    if existing is not None:
                        if existing["request_hash"] != request_hash:
                            raise api_error(
                                "IDEMPOTENCY_CONFLICT",
                                "Idempotency key was already used with "
                                "different inputs",
                                exit_code=ExitCode.CONFLICT,
                                retryable=False,
                                suggested_action="use_a_new_idempotency_key",
                            )
                        return self._get_execution(
                            connection, existing["execution_id"]
                        )

                existing_id = connection.execute(
                    "SELECT 1 FROM executions WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
                if existing_id:
                    raise api_error(
                        "EXECUTION_ID_CONFLICT",
                        f"Execution already exists: {execution_id}",
                        exit_code=ExitCode.CONFLICT,
                        retryable=False,
                        suggested_action="generate_a_new_execution_id",
                    )

                session = connection.execute(
                    """
                    SELECT endpoint, kernel_id, jupyter_session_id
                    FROM sessions WHERE profile_id = ? AND name = ?
                    """,
                    (self.profile.profile_id, session_name),
                ).fetchone()
                if session is None:
                    raise api_error(
                        "SESSION_NOT_FOUND",
                        f"Session not found: {session_name}",
                        exit_code=ExitCode.NOT_FOUND,
                        retryable=False,
                        suggested_action="ensure_session",
                    )
                source_path = self._atomic_write(
                    self.paths.sources_dir,
                    f"{execution_id}.source",
                    source,
                )
                now = self._time()
                connection.execute(
                    """
                    INSERT INTO executions (
                        execution_id, profile_id, session_name,
                        session_endpoint, kernel_id_snapshot,
                        jupyter_session_id_snapshot, idempotency_key,
                        request_hash, source_kind, source_path, notebook_id,
                        cell_id, cell_index, source_sha256, source_spool_path,
                        state, execution_timeout_seconds,
                        created_at, queued_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        execution_id,
                        self.profile.profile_id,
                        session_name,
                        session["endpoint"] if session else None,
                        session["kernel_id"] if session else None,
                        session["jupyter_session_id"] if session else None,
                        idempotency_key,
                        request_hash,
                        str(provenance.get("kind") or "stdin"),
                        provenance.get("path"),
                        provenance.get("notebook_id"),
                        provenance.get("cell_id"),
                        provenance.get("cell_index"),
                        source_hash,
                        str(source_path),
                        ExecutionState.QUEUED.value,
                        execution_timeout_seconds,
                        now,
                        now,
                        now,
                    ),
                )
                self._insert_transition(
                    connection,
                    execution_id,
                    None,
                    ExecutionState.CREATED,
                    reason="created",
                    created_at=now,
                )
                self._insert_transition(
                    connection,
                    execution_id,
                    ExecutionState.CREATED,
                    ExecutionState.QUEUED,
                    reason="source_durably_queued",
                    created_at=now,
                )
        except BaseException:
            if source_path is not None:
                with contextlib.suppress(OSError):
                    source_path.unlink()
            raise
        return self.get_execution(execution_id)

    def _get_execution(
        self, connection: sqlite3.Connection, execution_id: str
    ) -> ExecutionRecord | None:
        row = connection.execute(
            """
            SELECT execution_id, profile_id, session_name, session_endpoint,
                   kernel_id_snapshot, jupyter_session_id_snapshot,
                   idempotency_key, request_hash, source_kind, source_path,
                   notebook_id, cell_id, cell_index, source_sha256,
                   source_spool_path, state, kernel_message_id,
                   dispatch_confirmed, reply_received, idle_received,
                   completion_source, output_complete,
                   output_spool_path, output_byte_size, output_sha256,
                   output_finalized_at,
                   execution_timeout_seconds, execution_deadline,
                   cancel_requested, interrupt_requested_state, reply_status,
                   reconnect_count, error_name, error_value, traceback_json,
                   created_at, queued_at, started_at,
                   completed_at, updated_at
            FROM executions
            WHERE execution_id = ? AND profile_id = ?
            """,
            (execution_id, self.profile.profile_id),
        ).fetchone()
        return ExecutionRecord.model_validate(dict(row)) if row else None

    def get_execution(self, execution_id: str) -> ExecutionRecord | None:
        return self._get_execution(self.connection, execution_id)

    def read_execution_source(self, execution_id: str) -> bytes:
        record = self.get_execution(execution_id)
        if record is None:
            raise api_error(
                "EXECUTION_NOT_FOUND",
                f"Execution not found: {execution_id}",
                exit_code=ExitCode.NOT_FOUND,
                retryable=False,
                suggested_action="list_executions",
            )
        if record.state is not ExecutionState.QUEUED or not record.source_spool_path:
            raise api_error(
                "EXECUTION_SOURCE_UNAVAILABLE",
                "Execution source is available only while safely queued",
                exit_code=ExitCode.CONFLICT,
                retryable=False,
                suggested_action="refresh_execution_status",
            )
        try:
            source = Path(record.source_spool_path).read_bytes()
        except OSError as error:
            raise api_error(
                "EXECUTION_SOURCE_UNAVAILABLE",
                "Durably queued execution source could not be read",
                exit_code=ExitCode.UNAVAILABLE,
                retryable=False,
                suggested_action="inspect_local_state",
                details={"error": str(error)},
            ) from error
        if self.sha256(source) != record.source_sha256:
            raise api_error(
                "EXECUTION_SOURCE_CORRUPT",
                "Durably queued execution source failed its SHA-256 check",
                exit_code=ExitCode.CONFLICT,
                retryable=False,
                suggested_action="inspect_local_state",
            )
        return source

    def begin_dispatch(
        self,
        execution_id: str,
        *,
        kernel_message_id: str,
        session_endpoint: str,
        kernel_id: str,
        jupyter_session_id: str,
    ) -> ExecutionRecord:
        if not kernel_message_id:
            raise ValueError("kernel_message_id must not be empty")
        with self.transaction() as connection:
            current = self._require_execution(connection, execution_id)
            if current.state is not ExecutionState.QUEUED:
                raise api_error(
                    "INVALID_EXECUTION_TRANSITION",
                    f"Cannot dispatch execution in {current.state.value}",
                    exit_code=ExitCode.CONFLICT,
                    retryable=False,
                    suggested_action="refresh_execution_status",
                )
            now = self._time()
            connection.execute(
                """
                UPDATE executions
                SET state = ?, session_endpoint = ?,
                    kernel_id_snapshot = ?, jupyter_session_id_snapshot = ?,
                    kernel_message_id = ?, updated_at = ?
                WHERE execution_id = ?
                """,
                (
                    ExecutionState.DISPATCHING.value,
                    session_endpoint,
                    kernel_id,
                    jupyter_session_id,
                    kernel_message_id,
                    now,
                    execution_id,
                ),
            )
            self._insert_transition(
                connection,
                execution_id,
                ExecutionState.QUEUED,
                ExecutionState.DISPATCHING,
                reason="request_prepared_before_send",
                evidence={"kernel_message_id": kernel_message_id},
                created_at=now,
            )
        return self.get_execution(execution_id)

    def _require_execution(
        self, connection: sqlite3.Connection, execution_id: str
    ) -> ExecutionRecord:
        record = self._get_execution(connection, execution_id)
        if record is None:
            raise api_error(
                "EXECUTION_NOT_FOUND",
                f"Execution not found: {execution_id}",
                exit_code=ExitCode.NOT_FOUND,
                retryable=False,
                suggested_action="list_executions",
            )
        return record

    def _insert_transition(
        self,
        connection: sqlite3.Connection,
        execution_id: str,
        from_state: ExecutionState | None,
        to_state: ExecutionState,
        *,
        reason: str | None,
        evidence: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO execution_transitions (
                execution_id, from_state, to_state, reason,
                evidence_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                execution_id,
                from_state.value if from_state else None,
                to_state.value,
                reason,
                (
                    json.dumps(evidence, sort_keys=True, separators=(",", ":"))
                    if evidence is not None
                    else None
                ),
                created_at or self._time(),
            ),
        )

    def _discard_source(self, source_spool_path: str | None) -> None:
        if source_spool_path:
            with contextlib.suppress(FileNotFoundError):
                Path(source_spool_path).unlink()

    def transition_execution(
        self,
        execution_id: str,
        to_state: ExecutionState,
        *,
        reason: str | None = None,
        evidence: dict[str, Any] | None = None,
        completion_source: str | None = None,
    ) -> ExecutionRecord:
        with self.transaction() as connection:
            current = self._require_execution(connection, execution_id)
            allowed = ALLOWED_TRANSITIONS.get(current.state, frozenset())
            if to_state not in allowed:
                raise api_error(
                    "INVALID_EXECUTION_TRANSITION",
                    f"Cannot transition {current.state.value} to {to_state.value}",
                    exit_code=ExitCode.CONFLICT,
                    retryable=False,
                    suggested_action="refresh_execution_status",
                    details={
                        "from_state": current.state.value,
                        "to_state": to_state.value,
                    },
                )
            discard_source = to_state in TERMINAL_STATES
            if discard_source and current.output_finalized_at is None:
                raise api_error(
                    "OUTPUT_NOT_FINALIZED",
                    "Output spool must be finalized before a terminal transition",
                    exit_code=ExitCode.CONFLICT,
                    retryable=False,
                    suggested_action="finalize_execution_output",
                )
            if discard_source:
                # Safety wins over replayability if the process crashes before
                # the following transaction commits.
                self._discard_source(current.source_spool_path)
            now = self._time()
            completed_at = now if to_state in TERMINAL_STATES else None
            connection.execute(
                """
                UPDATE executions
                SET state = ?, source_spool_path = CASE WHEN ? THEN NULL
                    ELSE source_spool_path END,
                    completed_at = COALESCE(?, completed_at),
                    completion_source = COALESCE(?, completion_source),
                    updated_at = ?
                WHERE execution_id = ?
                """,
                (
                    to_state.value,
                    int(discard_source),
                    completed_at,
                    completion_source,
                    now,
                    execution_id,
                ),
            )
            self._insert_transition(
                connection,
                execution_id,
                current.state,
                to_state,
                reason=reason,
                evidence=evidence,
                created_at=now,
            )
        return self.get_execution(execution_id)

    def confirm_dispatch(
        self,
        execution_id: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> ExecutionRecord:
        with self.transaction() as connection:
            current = self._require_execution(connection, execution_id)
            if current.state is not ExecutionState.DISPATCHING:
                raise api_error(
                    "INVALID_EXECUTION_TRANSITION",
                    "Dispatch can be confirmed only from dispatching",
                    exit_code=ExitCode.CONFLICT,
                    retryable=False,
                    suggested_action="refresh_execution_status",
                )
            self._discard_source(current.source_spool_path)
            now = self._time()
            deadline = (
                _timestamp(
                    self._clock()
                    + timedelta(seconds=current.execution_timeout_seconds)
                )
                if current.execution_timeout_seconds is not None
                else None
            )
            connection.execute(
                """
                UPDATE executions
                SET state = ?, dispatch_confirmed = 1,
                    source_spool_path = NULL, started_at = COALESCE(started_at, ?),
                    execution_deadline = COALESCE(execution_deadline, ?),
                    updated_at = ?
                WHERE execution_id = ?
                """,
                (
                    ExecutionState.RUNNING.value,
                    now,
                    deadline,
                    now,
                    execution_id,
                ),
            )
            self._insert_transition(
                connection,
                execution_id,
                ExecutionState.DISPATCHING,
                ExecutionState.RUNNING,
                reason="matching_inbound_message",
                evidence=evidence,
                created_at=now,
            )
        return self.get_execution(execution_id)

    def record_execution_evidence(
        self,
        execution_id: str,
        *,
        reply_received: bool = False,
        idle_received: bool = False,
        reply_status: str | None = None,
        error_name: str | None = None,
        error_value: str | None = None,
        traceback: list[str] | None = None,
    ) -> ExecutionRecord:
        if reply_status is not None and reply_status not in {
            "ok",
            "error",
            "aborted",
        }:
            raise ValueError("invalid execute reply status")
        traceback_json = (
            json.dumps(traceback, ensure_ascii=False, separators=(",", ":"))
            if traceback is not None
            else None
        )
        with self.transaction() as connection:
            current = self._require_execution(connection, execution_id)
            if current.state not in {
                ExecutionState.RUNNING,
                ExecutionState.DISCONNECTED,
            }:
                raise api_error(
                    "INVALID_EXECUTION_EVIDENCE",
                    f"Cannot record proof in {current.state.value}",
                    exit_code=ExitCode.CONFLICT,
                    retryable=False,
                    suggested_action="refresh_execution_status",
                )
            connection.execute(
                """
                UPDATE executions
                SET reply_received = CASE WHEN ? THEN 1 ELSE reply_received END,
                    idle_received = CASE WHEN ? THEN 1 ELSE idle_received END,
                    reply_status = COALESCE(?, reply_status),
                    error_name = COALESCE(?, error_name),
                    error_value = COALESCE(?, error_value),
                    traceback_json = COALESCE(?, traceback_json),
                    updated_at = ?
                WHERE execution_id = ?
                """,
                (
                    int(reply_received),
                    int(idle_received),
                    reply_status,
                    error_name,
                    error_value,
                    traceback_json,
                    self._time(),
                    execution_id,
                ),
            )
        return self.get_execution(execution_id)

    def mark_output_incomplete(self, execution_id: str) -> ExecutionRecord:
        with self.transaction() as connection:
            self._require_execution(connection, execution_id)
            connection.execute(
                """
                UPDATE executions SET output_complete = 0, updated_at = ?
                WHERE execution_id = ?
                """,
                (self._time(), execution_id),
            )
        return self.get_execution(execution_id)

    def request_execution_cancel(self, execution_id: str) -> ExecutionRecord:
        with self.transaction() as connection:
            current = self._require_execution(connection, execution_id)
            if current.state in TERMINAL_STATES:
                return current
            now = self._time()
            if current.state is ExecutionState.QUEUED:
                self._discard_source(current.source_spool_path)
                connection.execute(
                    """
                    UPDATE executions
                    SET state = ?, cancel_requested = 1,
                        interrupt_requested_state = ?,
                        source_spool_path = NULL, completion_source = ?,
                        output_sha256 = ?, output_finalized_at = ?,
                        completed_at = ?, updated_at = ?
                    WHERE execution_id = ?
                    """,
                    (
                        ExecutionState.INTERRUPTED.value,
                        ExecutionState.INTERRUPTED.value,
                        "live",
                        self.sha256(b""),
                        now,
                        now,
                        now,
                        execution_id,
                    ),
                )
                self._insert_transition(
                    connection,
                    execution_id,
                    ExecutionState.QUEUED,
                    ExecutionState.INTERRUPTED,
                    reason="cancelled_while_queued",
                    created_at=now,
                )
            else:
                connection.execute(
                    """
                    UPDATE executions
                    SET cancel_requested = 1, interrupt_requested_state = ?,
                        updated_at = ?
                    WHERE execution_id = ?
                    """,
                    (
                        ExecutionState.INTERRUPTED.value,
                        now,
                        execution_id,
                    ),
                )
        return self.get_execution(execution_id)

    def request_execution_timeout(self, execution_id: str) -> ExecutionRecord:
        with self.transaction() as connection:
            current = self._require_execution(connection, execution_id)
            if current.state not in {
                ExecutionState.RUNNING,
                ExecutionState.DISCONNECTED,
            }:
                return current
            connection.execute(
                """
                UPDATE executions
                SET interrupt_requested_state = ?, updated_at = ?
                WHERE execution_id = ?
                """,
                (
                    ExecutionState.TIMED_OUT.value,
                    self._time(),
                    execution_id,
                ),
            )
        return self.get_execution(execution_id)

    def append_output_event(
        self,
        execution_id: str,
        event: dict[str, Any],
    ) -> list[int]:
        event_type = str(event.get("event_type") or "")
        if not event_type:
            raise ValueError("output event_type is required")
        if event_type == "stream":
            return self._append_text_output(
                execution_id,
                event_type=event_type,
                text=str(event.get("text") or ""),
                metadata={"stream": str(event.get("stream") or "stdout")},
                stream_name=str(event.get("stream") or "stdout"),
            )
        if event_type == "error":
            traceback = event.get("traceback")
            lines = (
                traceback
                if isinstance(traceback, list)
                and all(isinstance(line, str) for line in traceback)
                else []
            )
            return self._append_text_output(
                execution_id,
                event_type=event_type,
                text="\n".join(lines),
                metadata={
                    "error_name": str(event.get("error_name") or "Error"),
                    "error_value": str(event.get("error_value") or ""),
                    "traceback": lines,
                },
            )
        if event_type in {
            "display_data",
            "execute_result",
            "update_display_data",
        }:
            return self._append_mime_output(
                execution_id,
                event_type=event_type,
                event=event,
            )
        metadata = {
            key: value
            for key, value in event.items()
            if key != "event_type"
        }
        return [
            self._append_output_index(
                execution_id,
                event_type=event_type,
                metadata=metadata,
            )
        ]

    @staticmethod
    def _split_utf8(text: str) -> list[bytes]:
        data = text.encode("utf-8")
        if not data:
            return [b""]
        chunks: list[bytes] = []
        start = 0
        while start < len(data):
            end = min(start + OUTPUT_CHUNK_BYTES, len(data))
            if end < len(data):
                while end > start and data[end] & 0b1100_0000 == 0b1000_0000:
                    end -= 1
            if end == start:
                end = min(start + OUTPUT_CHUNK_BYTES, len(data))
            chunks.append(data[start:end])
            start = end
        return chunks

    def _next_output_sequence(
        self,
        connection: sqlite3.Connection,
        execution_id: str,
    ) -> int:
        row = connection.execute(
            """
            SELECT COALESCE(MAX(sequence), -1) + 1 AS sequence
            FROM output_chunks WHERE execution_id = ?
            """,
            (execution_id,),
        ).fetchone()
        return int(row["sequence"])

    @staticmethod
    def _metadata_json(metadata: dict[str, Any]) -> str:
        return json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _append_text_output(
        self,
        execution_id: str,
        *,
        event_type: str,
        text: str,
        metadata: dict[str, Any],
        stream_name: str | None = None,
        mime_type: str | None = None,
        display_id: str | None = None,
    ) -> list[int]:
        chunks = self._split_utf8(text)
        rollback_path: Path | None = None
        rollback_offset = 0
        try:
            with self.transaction() as connection:
                record = self._require_execution(connection, execution_id)
                if record.output_finalized_at is not None:
                    raise api_error(
                        "OUTPUT_FINALIZED",
                        "Cannot append output after finalization",
                        exit_code=ExitCode.CONFLICT,
                        retryable=False,
                        suggested_action="refresh_execution_status",
                    )
                path = (
                    Path(record.output_spool_path)
                    if record.output_spool_path
                    else self.paths.outputs_dir / f"{execution_id}.text"
                )
                rollback_path = path
                rollback_offset = record.output_byte_size
                descriptor = os.open(
                    path,
                    os.O_CREAT | os.O_RDWR,
                    0o600,
                )
                try:
                    os.fchmod(descriptor, 0o600)
                    os.ftruncate(descriptor, rollback_offset)
                    os.lseek(descriptor, rollback_offset, os.SEEK_SET)
                    for chunk in chunks:
                        view = memoryview(chunk)
                        while view:
                            written = os.write(descriptor, view)
                            view = view[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

                first_sequence = self._next_output_sequence(
                    connection,
                    execution_id,
                )
                offset = rollback_offset
                now = self._time()
                sequences: list[int] = []
                for position, chunk in enumerate(chunks):
                    sequence = first_sequence + position
                    sequences.append(sequence)
                    chunk_metadata = metadata if position == 0 else {
                        key: value
                        for key, value in metadata.items()
                        if key not in {"traceback", "error_name", "error_value"}
                    }
                    connection.execute(
                        """
                        INSERT INTO output_chunks (
                            execution_id, sequence, event_type, stream_name,
                            mime_type, spool_offset, byte_length, display_id,
                            metadata_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            execution_id,
                            sequence,
                            event_type,
                            stream_name,
                            mime_type,
                            offset,
                            len(chunk),
                            display_id,
                            self._metadata_json(chunk_metadata),
                            now,
                        ),
                    )
                    offset += len(chunk)
                connection.execute(
                    """
                    UPDATE executions
                    SET output_spool_path = ?, output_byte_size = ?,
                        updated_at = ?
                    WHERE execution_id = ?
                    """,
                    (str(path), offset, now, execution_id),
                )
                return sequences
        except BaseException:
            if rollback_path is not None and rollback_path.exists():
                with contextlib.suppress(OSError):
                    with rollback_path.open("r+b") as file:
                        file.truncate(rollback_offset)
                        file.flush()
                        os.fsync(file.fileno())
            raise

    def _append_mime_output(
        self,
        execution_id: str,
        *,
        event_type: str,
        event: dict[str, Any],
    ) -> list[int]:
        data = event.get("data")
        representations = data if isinstance(data, dict) else {}
        display_id = (
            str(event["display_id"])
            if event.get("display_id") is not None
            else None
        )
        common = {
            "execution_count": event.get("execution_count"),
            "metadata": (
                event.get("metadata")
                if isinstance(event.get("metadata"), dict)
                else {}
            ),
        }
        sequences: list[int] = []
        if not representations:
            return [
                self._append_output_index(
                    execution_id,
                    event_type=event_type,
                    metadata=common,
                    display_id=display_id,
                )
            ]
        for mime_type, value in representations.items():
            mime = str(mime_type)
            if self._is_text_mime(mime):
                text = (
                    value
                    if isinstance(value, str)
                    else json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                encoded = text.encode("utf-8")
                if len(encoded) <= LARGE_MIME_ARTIFACT_BYTES:
                    sequences.extend(
                        self._append_text_output(
                            execution_id,
                            event_type=event_type,
                            text=text,
                            metadata=common,
                            mime_type=mime,
                            display_id=display_id,
                        )
                    )
                    continue
                artifact_data = encoded
            else:
                artifact_data = self._decode_binary_representation(value)
            artifact = self.create_artifact(
                execution_id=execution_id,
                data=artifact_data,
                media_type=mime,
                purpose="mime_output",
            )
            artifact_id = self.connection.execute(
                "SELECT artifact_id FROM artifacts WHERE path = ?",
                (artifact.path,),
            ).fetchone()["artifact_id"]
            sequences.append(
                self._append_output_index(
                    execution_id,
                    event_type=event_type,
                    metadata=common,
                    mime_type=mime,
                    artifact_id=artifact_id,
                    display_id=display_id,
                )
            )
        return sequences

    @staticmethod
    def _is_text_mime(mime_type: str) -> bool:
        return (
            mime_type.startswith("text/")
            or mime_type
            in {
                "application/json",
                "application/javascript",
                "application/xml",
                "image/svg+xml",
            }
            or mime_type.endswith("+json")
            or mime_type.endswith("+xml")
        )

    @staticmethod
    def _decode_binary_representation(value: Any) -> bytes:
        if isinstance(value, str):
            try:
                return base64.b64decode(value, validate=True)
            except ValueError:
                return value.encode("utf-8")
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _append_output_index(
        self,
        execution_id: str,
        *,
        event_type: str,
        metadata: dict[str, Any],
        stream_name: str | None = None,
        mime_type: str | None = None,
        artifact_id: str | None = None,
        display_id: str | None = None,
    ) -> int:
        with self.transaction() as connection:
            record = self._require_execution(connection, execution_id)
            if record.output_finalized_at is not None:
                raise api_error(
                    "OUTPUT_FINALIZED",
                    "Cannot append output after finalization",
                    exit_code=ExitCode.CONFLICT,
                    retryable=False,
                    suggested_action="refresh_execution_status",
                )
            sequence = self._next_output_sequence(connection, execution_id)
            connection.execute(
                """
                INSERT INTO output_chunks (
                    execution_id, sequence, event_type, stream_name,
                    mime_type, artifact_id, display_id, metadata_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    sequence,
                    event_type,
                    stream_name,
                    mime_type,
                    artifact_id,
                    display_id,
                    self._metadata_json(metadata),
                    self._time(),
                ),
            )
        return sequence

    def list_output_events(self, execution_id: str) -> list[dict[str, Any]]:
        return [
            event.to_wire()
            for event in self._read_all_output_events(execution_id)
        ]

    def _read_all_output_events(self, execution_id: str) -> list[OutputEvent]:
        record = self.get_execution(execution_id)
        if record is None:
            raise api_error(
                "EXECUTION_NOT_FOUND",
                f"Execution not found: {execution_id}",
                exit_code=ExitCode.NOT_FOUND,
                retryable=False,
                suggested_action="list_executions",
            )
        rows = self.connection.execute(
            """
            SELECT chunks.sequence, chunks.event_type, chunks.stream_name,
                   chunks.mime_type, chunks.spool_offset, chunks.byte_length,
                   chunks.display_id, chunks.metadata_json,
                   artifacts.path AS artifact_path,
                   artifacts.media_type AS artifact_media_type,
                   artifacts.byte_size AS artifact_byte_size,
                   artifacts.sha256 AS artifact_sha256,
                   artifacts.purpose AS artifact_purpose
            FROM output_chunks AS chunks
            LEFT JOIN artifacts
              ON artifacts.artifact_id = chunks.artifact_id
            WHERE chunks.execution_id = ?
            ORDER BY chunks.sequence
            """,
            (execution_id,),
        )
        return [self._output_event_from_row(record, row) for row in rows]

    def _output_event_from_row(
        self,
        record: ExecutionRecord,
        row: sqlite3.Row,
    ) -> OutputEvent:
        metadata = (
            json.loads(row["metadata_json"])
            if row["metadata_json"]
            else {}
        )
        text: str | None = None
        if row["spool_offset"] is not None and row["byte_length"] is not None:
            if not record.output_spool_path:
                raise api_error(
                    "OUTPUT_SPOOL_MISSING",
                    "Output index references a missing spool",
                    exit_code=ExitCode.UNAVAILABLE,
                    retryable=False,
                    suggested_action="inspect_local_state",
                )
            with Path(record.output_spool_path).open("rb") as spool:
                spool.seek(row["spool_offset"])
                data = spool.read(row["byte_length"])
            if len(data) != row["byte_length"]:
                raise api_error(
                    "OUTPUT_SPOOL_INCOMPLETE",
                    "Output spool is shorter than its durable index",
                    exit_code=ExitCode.UNAVAILABLE,
                    retryable=False,
                    suggested_action="inspect_local_state",
                )
            text = data.decode("utf-8")
        elif "text" in metadata:
            # Read compatibility for schema-v1 inline development records.
            text = str(metadata.pop("text"))
        artifact = (
            Artifact(
                path=row["artifact_path"],
                media_type=row["artifact_media_type"],
                byte_size=row["artifact_byte_size"],
                sha256=row["artifact_sha256"],
                purpose=row["artifact_purpose"],
            )
            if row["artifact_path"] is not None
            else None
        )
        traceback = metadata.get("traceback")
        return OutputEvent(
            cursor=encode_cursor(row["sequence"]),
            event_type=row["event_type"],
            text=text,
            stream=row["stream_name"] or metadata.get("stream"),
            mime_type=row["mime_type"],
            artifact=artifact,
            display_id=row["display_id"],
            execution_count=metadata.get("execution_count"),
            metadata=(
                metadata.get("metadata")
                if isinstance(metadata.get("metadata"), dict)
                else None
            ),
            error_name=metadata.get("error_name"),
            error_value=metadata.get("error_value"),
            traceback=traceback if isinstance(traceback, list) else None,
            wait=metadata.get("wait"),
        )

    def read_output_page(
        self,
        execution_id: str,
        *,
        cursor: str | None,
        max_bytes: int,
    ) -> OutputPage:
        if max_bytes < MIN_OUTPUT_PAGE_BYTES:
            raise api_error(
                "OUTPUT_PAGE_BUDGET_TOO_SMALL",
                f"max_bytes must be at least {MIN_OUTPUT_PAGE_BYTES}",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="increase_max_bytes",
            )
        if max_bytes > MAX_OUTPUT_PAGE_BYTES:
            raise api_error(
                "INVALID_MAX_BYTES",
                f"max_bytes cannot exceed {MAX_OUTPUT_PAGE_BYTES}",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="use_a_supported_byte_budget",
            )
        try:
            offset = decode_cursor(cursor)
        except ValueError as error:
            raise api_error(
                "INVALID_CURSOR",
                "invalid output cursor",
                exit_code=ExitCode.USAGE,
                retryable=False,
                suggested_action="restart_output_read",
            ) from error
        record = self.get_execution(execution_id)
        if record is None:
            raise api_error(
                "EXECUTION_NOT_FOUND",
                f"Execution not found: {execution_id}",
                exit_code=ExitCode.NOT_FOUND,
                retryable=False,
                suggested_action="list_executions",
            )
        rows = self.connection.execute(
            """
            SELECT chunks.sequence, chunks.event_type, chunks.stream_name,
                   chunks.mime_type, chunks.spool_offset, chunks.byte_length,
                   chunks.display_id, chunks.metadata_json,
                   artifacts.path AS artifact_path,
                   artifacts.media_type AS artifact_media_type,
                   artifacts.byte_size AS artifact_byte_size,
                   artifacts.sha256 AS artifact_sha256,
                   artifacts.purpose AS artifact_purpose
            FROM output_chunks AS chunks
            LEFT JOIN artifacts
              ON artifacts.artifact_id = chunks.artifact_id
            WHERE chunks.execution_id = ? AND chunks.sequence >= ?
            ORDER BY chunks.sequence
            """,
            (execution_id, offset),
        )
        first_row = rows.fetchone()
        if first_row is None:
            maximum = self.connection.execute(
                """
                SELECT COALESCE(MAX(sequence), -1) AS maximum
                FROM output_chunks WHERE execution_id = ?
                """,
                (execution_id,),
            ).fetchone()["maximum"]
            if offset > maximum + 1:
                raise api_error(
                    "INVALID_CURSOR",
                    "output cursor is beyond the execution output",
                    exit_code=ExitCode.USAGE,
                    retryable=False,
                    suggested_action="restart_output_read",
                )
        events: list[OutputEvent] = []
        used = 0
        next_sequence = offset
        has_more = False
        row = first_row
        while row is not None:
            event = self._output_event_from_row(record, row)
            size = len(
                json.dumps(
                    event.to_wire(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if events and used + size > max_bytes:
                has_more = True
                break
            if not events and size > max_bytes:
                raise api_error(
                    "OUTPUT_EVENT_TOO_LARGE",
                    "One output event exceeds the requested page budget",
                    exit_code=ExitCode.USAGE,
                    retryable=False,
                    suggested_action="increase_max_bytes",
                    details={"event_bytes": size},
                )
            events.append(event)
            used += size
            next_sequence = row["sequence"] + 1
            row = rows.fetchone()
        return OutputPage(
            execution_id=execution_id,
            events=events,
            next_cursor=encode_cursor(next_sequence) if has_more else None,
            has_more=has_more,
            output_complete=record.output_complete,
        )

    def finalize_output(self, execution_id: str) -> list[Artifact]:
        record = self.get_execution(execution_id)
        if record is None:
            raise api_error(
                "EXECUTION_NOT_FOUND",
                f"Execution not found: {execution_id}",
                exit_code=ExitCode.NOT_FOUND,
                retryable=False,
                suggested_action="list_executions",
            )
        existing = self._artifacts_for_execution(
            execution_id,
            purpose="complete_text_output",
        )
        if record.output_finalized_at is not None:
            for artifact in existing:
                self._ensure_artifact_output_index(execution_id, artifact)
            return existing
        byte_size = 0
        digest = self.sha256(b"")
        spool_path: Path | None = None
        if record.output_spool_path:
            spool_path = Path(record.output_spool_path)
            with spool_path.open("rb") as spool:
                os.fsync(spool.fileno())
                digest = hashlib.file_digest(spool, "sha256").hexdigest()
            byte_size = spool_path.stat().st_size
            if byte_size != record.output_byte_size:
                raise api_error(
                    "OUTPUT_SPOOL_INCOMPLETE",
                    "Output spool size does not match its durable index",
                    exit_code=ExitCode.UNAVAILABLE,
                    retryable=False,
                    suggested_action="inspect_local_state",
                )
        if (
            byte_size > OUTPUT_ARTIFACT_THRESHOLD_BYTES
            and not existing
            and spool_path is not None
        ):
            existing = [
                self.create_artifact_from_file(
                    execution_id=execution_id,
                    source=spool_path,
                    media_type="text/plain; charset=utf-8",
                    purpose="complete_text_output",
                )
            ]
        for artifact in existing:
            self._ensure_artifact_output_index(execution_id, artifact)
        with self.transaction() as connection:
            current = self._require_execution(connection, execution_id)
            if current.output_finalized_at is None:
                connection.execute(
                    """
                    UPDATE executions
                    SET output_sha256 = ?, output_finalized_at = ?,
                        updated_at = ?
                    WHERE execution_id = ?
                    """,
                    (
                        digest,
                        self._time(),
                        self._time(),
                        execution_id,
                    ),
                )
        return existing

    def _ensure_artifact_output_index(
        self,
        execution_id: str,
        artifact: Artifact,
    ) -> None:
        with self.transaction() as connection:
            self._require_execution(connection, execution_id)
            artifact_row = connection.execute(
                """
                SELECT artifact_id FROM artifacts
                WHERE execution_id = ? AND path = ?
                """,
                (execution_id, artifact.path),
            ).fetchone()
            if artifact_row is None:
                raise api_error(
                    "ARTIFACT_NOT_FOUND",
                    "Execution artifact metadata is missing",
                    exit_code=ExitCode.UNAVAILABLE,
                    retryable=False,
                    suggested_action="inspect_local_state",
                )
            artifact_id = artifact_row["artifact_id"]
            indexed = connection.execute(
                """
                SELECT 1 FROM output_chunks
                WHERE execution_id = ? AND artifact_id = ?
                """,
                (execution_id, artifact_id),
            ).fetchone()
            if indexed is not None:
                return
            sequence = self._next_output_sequence(connection, execution_id)
            connection.execute(
                """
                INSERT INTO output_chunks (
                    execution_id, sequence, event_type, artifact_id,
                    metadata_json, created_at
                ) VALUES (?, ?, 'artifact', ?, '{}', ?)
                """,
                (execution_id, sequence, artifact_id, self._time()),
            )

    def _artifacts_for_execution(
        self,
        execution_id: str,
        *,
        purpose: str | None = None,
    ) -> list[Artifact]:
        parameters: list[Any] = [execution_id]
        purpose_clause = ""
        if purpose is not None:
            purpose_clause = " AND purpose = ?"
            parameters.append(purpose)
        rows = self.connection.execute(
            f"""
            SELECT path, media_type, byte_size, sha256, purpose
            FROM artifacts
            WHERE execution_id = ? {purpose_clause}
            ORDER BY artifact_id
            """,
            parameters,
        )
        return [Artifact.model_validate(dict(row)) for row in rows]

    def list_executions(
        self,
        *,
        session_name: str | None = None,
    ) -> list[ExecutionRecord]:
        parameters: list[Any] = [self.profile.profile_id]
        session_clause = ""
        if session_name is not None:
            session_clause = " AND session_name = ?"
            parameters.append(session_name)
        rows = self.connection.execute(
            f"""
            SELECT execution_id FROM executions
            WHERE profile_id = ? {session_clause}
            ORDER BY created_at DESC, execution_id DESC
            """,
            parameters,
        )
        return [
            self.get_execution(row["execution_id"])
            for row in rows
        ]

    def list_queued_executions(self) -> list[ExecutionRecord]:
        rows = self.connection.execute(
            """
            SELECT execution_id FROM executions
            WHERE profile_id = ? AND state = ?
            ORDER BY queued_at, execution_id
            """,
            (self.profile.profile_id, ExecutionState.QUEUED.value),
        )
        return [
            self.get_execution(row["execution_id"])
            for row in rows
        ]

    def list_transitions(self, execution_id: str) -> list[ExecutionTransition]:
        rows = self.connection.execute(
            """
            SELECT transition_id, execution_id, from_state, to_state,
                   reason, evidence_json, created_at
            FROM execution_transitions
            WHERE execution_id = ? ORDER BY transition_id
            """,
            (execution_id,),
        )
        results = []
        for row in rows:
            values = dict(row)
            evidence_json = values.pop("evidence_json")
            values["evidence"] = (
                json.loads(evidence_json) if evidence_json is not None else None
            )
            results.append(ExecutionTransition.model_validate(values))
        return results

    def create_batch(
        self,
        *,
        batch_id: str,
        session_name: str,
        execution_ids: Sequence[str],
        continue_on_error: bool,
    ) -> BatchRecord:
        if len(set(execution_ids)) != len(execution_ids):
            raise api_error(
                "DUPLICATE_BATCH_MEMBER",
                "Each execution may appear only once in a batch",
                exit_code=ExitCode.CONFLICT,
                retryable=False,
                suggested_action="deduplicate_batch_members",
            )
        now = self._time()
        with self.transaction() as connection:
            for execution_id in execution_ids:
                self._require_execution(connection, execution_id)
            connection.execute(
                """
                INSERT INTO execution_batches (
                    batch_id, profile_id, session_name, state,
                    continue_on_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    self.profile.profile_id,
                    session_name,
                    "queued",
                    int(continue_on_error),
                    now,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO batch_members (batch_id, position, execution_id)
                VALUES (?, ?, ?)
                """,
                [
                    (batch_id, position, execution_id)
                    for position, execution_id in enumerate(execution_ids)
                ],
            )
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> BatchRecord | None:
        row = self.connection.execute(
            """
            SELECT batch_id, profile_id, session_name, state,
                   continue_on_error, created_at, updated_at, completed_at
            FROM execution_batches
            WHERE batch_id = ? AND profile_id = ?
            """,
            (batch_id, self.profile.profile_id),
        ).fetchone()
        return BatchRecord.model_validate(dict(row)) if row else None

    def list_batch_members(self, batch_id: str) -> list[str]:
        return [
            row["execution_id"]
            for row in self.connection.execute(
                """
                SELECT execution_id FROM batch_members
                WHERE batch_id = ? ORDER BY position
                """,
                (batch_id,),
            )
        ]

    def create_artifact(
        self,
        *,
        execution_id: str,
        data: bytes,
        media_type: str,
        purpose: str | None = None,
    ) -> Artifact:
        if self.get_execution(execution_id) is None:
            raise api_error(
                "EXECUTION_NOT_FOUND",
                f"Execution not found: {execution_id}",
                exit_code=ExitCode.NOT_FOUND,
                retryable=False,
                suggested_action="list_executions",
            )
        artifact_id = str(uuid.uuid4())
        path = self._atomic_write(self.paths.artifacts_dir, artifact_id, data)
        digest = f"sha256:{self.sha256(data)}"
        try:
            with self.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO artifacts (
                        artifact_id, profile_id, execution_id, path, media_type,
                        byte_size, sha256, purpose, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        self.profile.profile_id,
                        execution_id,
                        str(path),
                        media_type,
                        len(data),
                        digest,
                        purpose,
                        self._time(),
                    ),
                )
        except BaseException:
            with contextlib.suppress(OSError):
                path.unlink()
            raise
        return Artifact(
            path=str(path),
            media_type=media_type,
            byte_size=len(data),
            sha256=digest,
            purpose=purpose,
        )

    def create_artifact_from_file(
        self,
        *,
        execution_id: str,
        source: Path,
        media_type: str,
        purpose: str | None = None,
    ) -> Artifact:
        if self.get_execution(execution_id) is None:
            raise api_error(
                "EXECUTION_NOT_FOUND",
                f"Execution not found: {execution_id}",
                exit_code=ExitCode.NOT_FOUND,
                retryable=False,
                suggested_action="list_executions",
            )
        artifact_id = str(uuid.uuid4())
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{artifact_id}.",
            dir=self.paths.artifacts_dir,
        )
        temporary_path = Path(temporary)
        destination = self.paths.artifacts_dir / artifact_id
        digest = hashlib.sha256()
        byte_size = 0
        try:
            os.fchmod(descriptor, 0o600)
            with source.open("rb") as input_file, os.fdopen(
                descriptor,
                "wb",
            ) as output_file:
                while chunk := input_file.read(1024 * 1024):
                    output_file.write(chunk)
                    digest.update(chunk)
                    byte_size += len(chunk)
                output_file.flush()
                os.fsync(output_file.fileno())
            os.replace(temporary_path, destination)
            destination.chmod(0o600)
            directory_descriptor = os.open(
                self.paths.artifacts_dir,
                os.O_RDONLY,
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            checksum = f"sha256:{digest.hexdigest()}"
            with self.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO artifacts (
                        artifact_id, profile_id, execution_id, path, media_type,
                        byte_size, sha256, purpose, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        self.profile.profile_id,
                        execution_id,
                        str(destination),
                        media_type,
                        byte_size,
                        checksum,
                        purpose,
                        self._time(),
                    ),
                )
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            with contextlib.suppress(OSError):
                temporary_path.unlink()
            with contextlib.suppress(OSError):
                destination.unlink()
            raise
        return Artifact(
            path=str(destination),
            media_type=media_type,
            byte_size=byte_size,
            sha256=checksum,
            purpose=purpose,
        )

    def prune_executions(
        self,
        *,
        before: str | datetime,
        session_name: str | None = None,
        confirm: bool = False,
    ) -> PruneResult:
        before_value = _timestamp(parse_timestamp(before))
        terminal_values = sorted(state.value for state in TERMINAL_STATES)
        placeholders = ",".join("?" for _ in terminal_values)
        parameters: list[Any] = [
            self.profile.profile_id,
            *terminal_values,
            before_value,
        ]
        session_clause = ""
        if session_name is not None:
            session_clause = " AND session_name = ?"
            parameters.append(session_name)
        rows = list(
            self.connection.execute(
                f"""
                SELECT execution_id, output_spool_path FROM executions
                WHERE profile_id = ?
                  AND state IN ({placeholders})
                  AND updated_at < ?
                  {session_clause}
                ORDER BY execution_id
                """,
                parameters,
            )
        )
        execution_ids = [row["execution_id"] for row in rows]
        output_spool_paths = [
            row["output_spool_path"]
            for row in rows
            if row["output_spool_path"] is not None
        ]
        if execution_ids:
            artifact_placeholders = ",".join("?" for _ in execution_ids)
            artifact_rows = list(
                self.connection.execute(
                    f"""
                    SELECT path, byte_size FROM artifacts
                    WHERE execution_id IN ({artifact_placeholders})
                    ORDER BY artifact_id
                    """,
                    execution_ids,
                )
            )
        else:
            artifact_rows = []
        artifact_bytes = sum(row["byte_size"] for row in artifact_rows)
        if not confirm:
            return PruneResult(
                dry_run=True,
                matched=len(execution_ids),
                deleted=0,
                execution_ids=execution_ids,
                artifact_bytes=artifact_bytes,
            )

        with self.transaction() as connection:
            if execution_ids:
                delete_placeholders = ",".join("?" for _ in execution_ids)
                connection.execute(
                    f"""
                    DELETE FROM executions
                    WHERE profile_id = ?
                      AND execution_id IN ({delete_placeholders})
                    """,
                    [self.profile.profile_id, *execution_ids],
                )
                connection.execute(
                    """
                    DELETE FROM execution_batches
                    WHERE profile_id = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM batch_members
                          WHERE batch_members.batch_id =
                                execution_batches.batch_id
                      )
                    """,
                    (self.profile.profile_id,),
                )
        for row in artifact_rows:
            with contextlib.suppress(FileNotFoundError):
                Path(row["path"]).unlink()
        for path in output_spool_paths:
            with contextlib.suppress(FileNotFoundError):
                Path(path).unlink()
        return PruneResult(
            dry_run=False,
            matched=len(execution_ids),
            deleted=len(execution_ids),
            execution_ids=execution_ids,
            artifact_bytes=artifact_bytes,
        )

    def list_active_executions(self) -> list[ExecutionRecord]:
        rows = self.connection.execute(
            """
            SELECT execution_id FROM executions
            WHERE profile_id = ?
              AND state IN ('dispatching', 'running', 'disconnected')
            ORDER BY execution_id
            """,
            (self.profile.profile_id,),
        )
        return [
            self.get_execution(row["execution_id"])
            for row in rows
        ]

    def force_uncertain_active(self, *, reason: str) -> list[str]:
        affected: list[str] = []
        for record in self.list_active_executions():
            execution_id = record.execution_id
            with self.transaction() as connection:
                connection.execute(
                    """
                    UPDATE executions SET output_complete = 0, updated_at = ?
                    WHERE execution_id = ?
                    """,
                    (self._time(), execution_id),
                )
            current = self.get_execution(execution_id)
            if current.state is ExecutionState.RUNNING:
                self.transition_execution(
                    execution_id,
                    ExecutionState.DISCONNECTED,
                    reason=reason,
                )
                current = self.get_execution(execution_id)
            if current.state in {
                ExecutionState.DISPATCHING,
                ExecutionState.DISCONNECTED,
            }:
                self.finalize_output(execution_id)
                self.transition_execution(
                    execution_id,
                    ExecutionState.UNKNOWN,
                    reason=reason,
                )
                affected.append(execution_id)
        return affected

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def __enter__(self) -> DurableStore:
        return self

    def __exit__(self, *_args) -> None:
        self.close()
