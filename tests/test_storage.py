import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from better_colab import BetterColabError, ExecutionState
from better_colab.storage import DurableStore, ProfileSpec, StatePaths


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


@pytest.fixture
def paths(tmp_path):
    return StatePaths(
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
    )


@pytest.fixture
def profile(tmp_path):
    return ProfileSpec.from_values(
        config_path=tmp_path / "legacy" / "sessions.json",
        auth_provider="oauth2",
        oauth_config_path=tmp_path / "oauth.json",
    )


@pytest.fixture
def store(paths, profile):
    value = DurableStore(paths=paths, profile=profile)
    yield value
    value.close()


def _create_execution(
    store: DurableStore,
    *,
    execution_id: str = "00000000-0000-4000-8000-000000000001",
    source: bytes = b"print('hello')",
    idempotency_key: str | None = None,
):
    return store.create_execution(
        execution_id=execution_id,
        session_name="training",
        source=source,
        provenance={"kind": "stdin"},
        request={
            "session": "training",
            "source_sha256": store.sha256(source),
        },
        idempotency_key=idempotency_key,
    )


def _finish_execution(store: DurableStore, execution_id: str):
    store.transition_execution(execution_id, ExecutionState.DISPATCHING)
    store.confirm_dispatch(execution_id)
    store.transition_execution(execution_id, ExecutionState.FINISHED)


def test_database_schema_pragmas_and_private_modes(store, paths):
    pragmas = {
        "journal_mode": store.connection.execute("PRAGMA journal_mode").fetchone()[0],
        "foreign_keys": store.connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "busy_timeout": store.connection.execute("PRAGMA busy_timeout").fetchone()[0],
        "synchronous": store.connection.execute("PRAGMA synchronous").fetchone()[0],
        "user_version": store.connection.execute("PRAGMA user_version").fetchone()[0],
    }
    tables = {
        row[0]
        for row in store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }

    assert pragmas == {
        "journal_mode": "wal",
        "foreign_keys": 1,
        "busy_timeout": 5000,
        "synchronous": 2,
        "user_version": 1,
    }
    assert {
        "profiles",
        "sessions",
        "executions",
        "execution_transitions",
        "execution_batches",
        "batch_members",
        "output_chunks",
        "artifacts",
        "kernel_connections",
    } <= tables
    assert stat.S_IMODE(paths.state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.artifacts_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.database.stat().st_mode) == 0o600


def test_profile_namespace_is_normalized_and_isolated(tmp_path):
    first = ProfileSpec.from_values(
        config_path=tmp_path / "a" / ".." / "sessions.json",
        auth_provider="oauth2",
        oauth_config_path=tmp_path / "oauth.json",
    )
    equivalent = ProfileSpec.from_values(
        config_path=tmp_path / "sessions.json",
        auth_provider="OAUTH2",
        oauth_config_path=tmp_path / "." / "oauth.json",
    )
    adc = ProfileSpec.from_values(
        config_path=tmp_path / "sessions.json",
        auth_provider="adc",
        oauth_config_path=tmp_path / "oauth.json",
    )

    assert first.profile_id == equivalent.profile_id
    assert len(first.profile_id) == 64
    assert adc.profile_id != first.profile_id


def test_legacy_import_is_one_time_non_destructive_and_detects_changes(
    paths, profile
):
    profile.config_path.parent.mkdir(parents=True)
    original = {
        "training": {
            "name": "training",
            "token": "secret",
            "url": "https://runtime.example",
            "endpoint": "endpoint-one",
            "variant": "GPU",
            "accelerator": "T4",
            "kernel_id": "kernel-one",
            "session_id": "jupyter-one",
            "keep_alive_pid": 123,
        }
    }
    profile.config_path.write_text(json.dumps(original), encoding="utf-8")

    first = DurableStore(paths=paths, profile=profile)
    imported = first.get_session("training")
    first.close()

    assert imported.endpoint == "endpoint-one"
    assert imported.runtime_token == "secret"
    assert json.loads(profile.config_path.read_text(encoding="utf-8")) == original

    changed = {
        **original,
        "training": {**original["training"], "endpoint": "endpoint-two"},
    }
    profile.config_path.write_text(json.dumps(changed), encoding="utf-8")

    reopened = DurableStore(paths=paths, profile=profile)
    assert reopened.get_session("training").endpoint == "endpoint-one"
    assert [item.code for item in reopened.diagnostics] == ["LEGACY_STATE_CHANGED"]
    row = reopened.connection.execute(
        "SELECT legacy_import_hash, legacy_imported_at FROM profiles "
        "WHERE profile_id = ?",
        (profile.profile_id,),
    ).fetchone()
    reopened.close()

    assert row["legacy_import_hash"]
    assert row["legacy_imported_at"]


def test_transactions_roll_back_at_crash_boundaries(store):
    with pytest.raises(RuntimeError, match="simulated crash"):
        with store.transaction() as connection:
            connection.execute(
                "INSERT INTO execution_batches "
                "(batch_id, profile_id, session_name, state, "
                "continue_on_error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "batch-crash",
                    store.profile.profile_id,
                    "training",
                    "queued",
                    0,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )
            raise RuntimeError("simulated crash")

    assert store.get_batch("batch-crash") is None


def test_execution_insert_crash_rolls_back_row_journal_and_source(
    mocker, store
):
    mocker.patch.object(
        store,
        "_insert_transition",
        side_effect=RuntimeError("simulated transition crash"),
    )

    with pytest.raises(RuntimeError, match="simulated transition crash"):
        _create_execution(store)

    assert (
        store.get_execution("00000000-0000-4000-8000-000000000001") is None
    )
    assert list(store.paths.sources_dir.iterdir()) == []


def test_execution_creation_atomically_queues_source_and_transitions(store):
    record = _create_execution(store)
    transitions = store.list_transitions(record.execution_id)
    source_path = Path(record.source_spool_path)

    assert record.state is ExecutionState.QUEUED
    assert record.source_sha256 == store.sha256(b"print('hello')")
    assert source_path.read_bytes() == b"print('hello')"
    assert stat.S_IMODE(source_path.stat().st_mode) == 0o600
    assert [(item.from_state, item.to_state) for item in transitions] == [
        (None, ExecutionState.CREATED),
        (ExecutionState.CREATED, ExecutionState.QUEUED),
    ]


def test_idempotency_reuses_identical_request_and_rejects_conflict(store):
    first = _create_execution(store, idempotency_key="stable-key")
    repeated = _create_execution(
        store,
        execution_id="00000000-0000-4000-8000-000000000002",
        idempotency_key="stable-key",
    )

    assert repeated.execution_id == first.execution_id

    with pytest.raises(BetterColabError) as conflict:
        _create_execution(
            store,
            execution_id="00000000-0000-4000-8000-000000000003",
            source=b"print('different')",
            idempotency_key="stable-key",
        )
    assert conflict.value.error.code == "IDEMPOTENCY_CONFLICT"
    assert conflict.value.exit_code == 5


def test_invalid_state_transition_is_a_conflict(store):
    record = _create_execution(store)

    with pytest.raises(BetterColabError) as conflict:
        store.transition_execution(record.execution_id, ExecutionState.FINISHED)

    assert conflict.value.error.code == "INVALID_EXECUTION_TRANSITION"
    assert store.get_execution(record.execution_id).state is ExecutionState.QUEUED


def test_source_is_destroyed_after_confirmed_dispatch_or_ambiguity(store):
    confirmed = _create_execution(store)
    confirmed_path = Path(confirmed.source_spool_path)
    store.transition_execution(confirmed.execution_id, ExecutionState.DISPATCHING)
    running = store.confirm_dispatch(confirmed.execution_id)

    assert running.state is ExecutionState.RUNNING
    assert running.dispatch_confirmed is True
    assert running.source_spool_path is None
    assert not confirmed_path.exists()

    ambiguous = _create_execution(
        store,
        execution_id="00000000-0000-4000-8000-000000000004",
    )
    ambiguous_path = Path(ambiguous.source_spool_path)
    store.transition_execution(ambiguous.execution_id, ExecutionState.DISPATCHING)
    unknown = store.transition_execution(
        ambiguous.execution_id,
        ExecutionState.UNKNOWN,
        reason="disconnect_before_confirmation",
    )

    assert unknown.source_spool_path is None
    assert not ambiguous_path.exists()


def test_execution_history_survives_session_deletion(store):
    store.upsert_session(
        name="training",
        endpoint="endpoint-one",
        backend_url="https://runtime.example",
        runtime_token="secret",
        hardware="T4",
    )
    execution = _create_execution(store)

    store.delete_session("training")

    assert store.get_session("training") is None
    assert store.get_execution(execution.execution_id) is not None


def test_profiles_remain_isolated_in_one_database(paths, tmp_path):
    oauth_profile = ProfileSpec.from_values(
        config_path=tmp_path / "oauth-sessions.json",
        auth_provider="oauth2",
        oauth_config_path=tmp_path / "oauth.json",
    )
    adc_profile = ProfileSpec.from_values(
        config_path=tmp_path / "adc-sessions.json",
        auth_provider="adc",
        oauth_config_path=tmp_path / "oauth.json",
    )
    oauth_store = DurableStore(paths=paths, profile=oauth_profile)
    adc_store = DurableStore(paths=paths, profile=adc_profile)
    oauth_store.upsert_session(
        name="same-name",
        endpoint="oauth-endpoint",
        backend_url="https://oauth.example",
        runtime_token="oauth-token",
        hardware="CPU",
    )
    adc_store.upsert_session(
        name="same-name",
        endpoint="adc-endpoint",
        backend_url="https://adc.example",
        runtime_token="adc-token",
        hardware="T4",
    )

    assert oauth_store.get_session("same-name").endpoint == "oauth-endpoint"
    assert adc_store.get_session("same-name").endpoint == "adc-endpoint"
    oauth_store.close()
    adc_store.close()


def test_batches_keep_one_ordered_child_per_member(store):
    first = _create_execution(store)
    second = _create_execution(
        store,
        execution_id="00000000-0000-4000-8000-000000000005",
    )

    batch = store.create_batch(
        batch_id="10000000-0000-4000-8000-000000000001",
        session_name="training",
        execution_ids=[first.execution_id, second.execution_id],
        continue_on_error=False,
    )

    assert batch.state == "queued"
    assert store.list_batch_members(batch.batch_id) == [
        first.execution_id,
        second.execution_id,
    ]


def test_prune_is_dry_run_by_default_and_never_matches_nonterminal(store):
    terminal = _create_execution(store)
    _finish_execution(store, terminal.execution_id)
    queued = _create_execution(
        store,
        execution_id="00000000-0000-4000-8000-000000000006",
    )
    artifact = store.create_artifact(
        execution_id=terminal.execution_id,
        data=b"artifact bytes",
        media_type="application/octet-stream",
        purpose="test",
    )
    future = _utc(2099, 1, 1)

    preview = store.prune_executions(before=future)

    assert preview.dry_run is True
    assert preview.execution_ids == [terminal.execution_id]
    assert preview.artifact_bytes == len(b"artifact bytes")
    assert store.get_execution(terminal.execution_id) is not None
    assert store.get_execution(queued.execution_id) is not None
    assert Path(artifact.path).exists()

    deleted = store.prune_executions(before=future, confirm=True)

    assert deleted.deleted == 1
    assert store.get_execution(terminal.execution_id) is None
    assert store.get_execution(queued.execution_id) is not None
    assert not Path(artifact.path).exists()


def test_default_paths_follow_xdg_state_and_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))

    paths = StatePaths.discover()

    assert paths.database == (
        tmp_path / "xdg-state" / "better-colab" / "controller.sqlite3"
    )
    assert paths.runtime_dir.parent == tmp_path / "tmp"
    assert paths.runtime_dir.name == f"better-colab-{os.getuid()}"


def test_prune_before_accepts_timezone_aware_datetimes_only(store):
    with pytest.raises(BetterColabError) as error:
        store.prune_executions(before=datetime(2026, 1, 1))

    assert error.value.error.code == "INVALID_TIMESTAMP"

    # Aware values are accepted even when no records match.
    result = store.prune_executions(
        before=_utc(2026, 1, 1) + timedelta(seconds=1)
    )
    assert result.matched == 0
