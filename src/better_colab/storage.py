"""SQLite-backed durable state and protected local artifact storage."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence


from better_colab.errors import ExitCode, api_error
from better_colab.models import (
    Artifact,
    ExecutionState,
    PruneResult,
    PublicModel,
)


DATABASE_SCHEMA_VERSION = 1
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
    execution_deadline: str | None = None
    cancel_requested: bool
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
            self.connection.execute(
                f"PRAGMA user_version={DATABASE_SCHEMA_VERSION}"
            )

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
    ) -> ExecutionRecord:
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
                        state, created_at, queued_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
                   completion_source, output_complete, execution_deadline,
                   cancel_requested, reconnect_count, error_name, error_value,
                   traceback_json, created_at, queued_at, started_at,
                   completed_at, updated_at
            FROM executions
            WHERE execution_id = ? AND profile_id = ?
            """,
            (execution_id, self.profile.profile_id),
        ).fetchone()
        return ExecutionRecord.model_validate(dict(row)) if row else None

    def get_execution(self, execution_id: str) -> ExecutionRecord | None:
        return self._get_execution(self.connection, execution_id)

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
                    completed_at = COALESCE(?, completed_at), updated_at = ?
                WHERE execution_id = ?
                """,
                (
                    to_state.value,
                    int(discard_source),
                    completed_at,
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
            connection.execute(
                """
                UPDATE executions
                SET state = ?, dispatch_confirmed = 1,
                    source_spool_path = NULL, started_at = COALESCE(started_at, ?),
                    updated_at = ?
                WHERE execution_id = ?
                """,
                (
                    ExecutionState.RUNNING.value,
                    now,
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
                SELECT execution_id FROM executions
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
        return PruneResult(
            dry_run=False,
            matched=len(execution_ids),
            deleted=len(execution_ids),
            execution_ids=execution_ids,
            artifact_bytes=artifact_bytes,
        )

    def close(self) -> None:
        with self._lock:
            self.connection.close()
