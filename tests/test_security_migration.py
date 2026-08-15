import json
import os
import threading

import pytest

from colab_cli.security_migration import scrub_legacy_history
from colab_cli.history import HistoryLogger


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_scrub_legacy_history_is_private_preserving_and_idempotent(tmp_path):
    history_dir = tmp_path / "history"
    history_dir.mkdir(mode=0o777)
    history_dir.chmod(0o777)
    path = history_dir / "session.jsonl"
    events = [
        {
            "timestamp": "2026-08-15T00:00:00+00:00",
            "event_type": "drive_auth_needed",
            "uri": "https://accounts.google.com/o/oauth2/v2/auth?state=secret",
            "keep": "value",
        },
        {
            "timestamp": "2026-08-15T00:00:01+00:00",
            "event_type": "stdin_request",
            "prompt": "paste secret",
        },
        {
            "timestamp": "2026-08-15T00:00:02+00:00",
            "event_type": "input_reply",
            "value": "plaintext",
        },
        {
            "timestamp": "2026-08-15T00:00:03+00:00",
            "event_type": "keep_alive_error",
            "response_body": "credential body",
            "status_code": 403,
        },
        {
            "timestamp": "2026-08-15T00:00:04+00:00",
            "event_type": "keep_alive_stopped",
            "last_error": {"response_body": "nested credential", "status_code": 403},
        },
    ]
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    path.chmod(0o666)

    assert scrub_legacy_history(history_dir) == 5
    first = path.read_text(encoding="utf-8")
    assert scrub_legacy_history(history_dir) == 0
    assert path.read_text(encoding="utf-8") == first

    migrated = [json.loads(line) for line in first.splitlines()]
    assert migrated[0]["uri"] == "<redacted>"
    assert migrated[0]["keep"] == "value"
    assert migrated[1]["prompt"] == "<redacted>"
    assert migrated[2]["value"] == "<redacted>"
    assert "response_body" not in migrated[3]
    assert "response_body" not in migrated[4]["last_error"]
    assert [event["timestamp"] for event in migrated] == [
        event["timestamp"] for event in events
    ]
    assert history_dir.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


def test_scrub_legacy_history_preserves_malformed_file(tmp_path):
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    path = history_dir / "session.jsonl"
    path.write_text('{"event_type":"event"}\nnot json\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="malformed"):
        scrub_legacy_history(history_dir)

    assert path.read_text(encoding="utf-8") == '{"event_type":"event"}\nnot json\n'


def test_scrub_legacy_history_serializes_with_concurrent_append(monkeypatch, tmp_path):
    history_dir = tmp_path / "history"
    logger = HistoryLogger(str(history_dir))
    logger.log_event("session", "stdin_request", {"prompt": "secret"})

    import colab_cli.security_migration as migration

    original_read = migration.read_private_text
    migration_read_started = threading.Event()
    release_migration = threading.Event()

    def blocking_read(path):
        contents = original_read(path)
        migration_read_started.set()
        assert release_migration.wait(timeout=2)
        return contents

    monkeypatch.setattr(migration, "read_private_text", blocking_read)
    migration_thread = threading.Thread(
        target=scrub_legacy_history, args=(history_dir,)
    )
    migration_thread.start()
    assert migration_read_started.wait(timeout=2)

    append_thread = threading.Thread(
        target=logger.log_event,
        args=("session", "automation", {"op": "install"}),
    )
    append_thread.start()
    release_migration.set()
    migration_thread.join(timeout=2)
    append_thread.join(timeout=2)

    assert not migration_thread.is_alive()
    assert not append_thread.is_alive()
    assert [event["event_type"] for event in logger.get_history("session")] == [
        "stdin_request",
        "automation",
    ]
