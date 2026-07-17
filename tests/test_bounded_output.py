import base64
import hashlib
import json
import stat
from pathlib import Path

import pytest

from better_colab import BetterColabError, ExecutionState
from better_colab.protocol import render_success_bytes
from better_colab.storage import (
    OUTPUT_CHUNK_BYTES,
    DurableStore,
    ProfileSpec,
    StatePaths,
)


@pytest.fixture
def store(tmp_path):
    paths = StatePaths(
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "run",
    )
    profile = ProfileSpec.from_values(
        config_path=tmp_path / "sessions.json",
        auth_provider="oauth2",
        oauth_config_path=tmp_path / "oauth.json",
    )
    value = DurableStore(paths=paths, profile=profile)
    value.upsert_session(
        name="training",
        endpoint="endpoint",
        backend_url="https://runtime.example",
        runtime_token="secret",
        hardware="CPU",
    )
    execution = value.create_execution(
        execution_id="00000000-0000-4000-8000-000000000501",
        session_name="training",
        source=b"pass",
        provenance={"kind": "stdin"},
        request={"session": "training", "source_sha256": value.sha256(b"pass")},
    )
    value.begin_dispatch(
        execution.execution_id,
        kernel_message_id="message-1",
        session_endpoint="endpoint",
        kernel_id="kernel",
        jupyter_session_id="jupyter",
    )
    value.confirm_dispatch(execution.execution_id)
    yield value
    value.close()


def test_large_unicode_stream_uses_spool_ranges_and_bounded_stable_cursors(
    store,
):
    text = ("🙂 αβγ output\n" * 2000) + "tail"
    execution_id = "00000000-0000-4000-8000-000000000501"

    store.append_output_event(
        execution_id,
        {
            "event_type": "stream",
            "stream": "stdout",
            "text": text,
        },
    )

    record = store.get_execution(execution_id)
    spool = Path(record.output_spool_path)
    rows = list(
        store.connection.execute(
            """
            SELECT sequence, spool_offset, byte_length, metadata_json
            FROM output_chunks WHERE execution_id = ? ORDER BY sequence
            """,
            (execution_id,),
        )
    )
    assert spool.read_bytes() == text.encode("utf-8")
    assert stat.S_IMODE(spool.stat().st_mode) == 0o600
    assert record.output_byte_size == len(text.encode("utf-8"))
    assert len(rows) > 1
    assert all(0 < row["byte_length"] <= OUTPUT_CHUNK_BYTES for row in rows)
    assert all("text" not in json.loads(row["metadata_json"]) for row in rows)

    cursor = None
    seen = []
    page_sizes = []
    while True:
        page = store.read_output_page(
            execution_id,
            cursor=cursor,
            max_bytes=2048,
        )
        replay = store.read_output_page(
            execution_id,
            cursor=cursor,
            max_bytes=2048,
        )
        assert replay == page
        encoded, exit_code = render_success_bytes(page)
        assert exit_code == 0
        page_sizes.append(len(encoded))
        seen.extend(event.text or "" for event in page.events)
        if not page.has_more:
            break
        assert page.next_cursor
        cursor = page.next_cursor

    assert "".join(seen) == text
    assert max(page_sizes) < 4096


def test_binary_mime_becomes_immutable_checksum_artifact(store):
    execution_id = "00000000-0000-4000-8000-000000000501"
    png = b"\x89PNG\r\n\x1a\nbinary-payload"
    store.append_output_event(
        execution_id,
        {
            "event_type": "display_data",
            "data": {"image/png": base64.b64encode(png).decode()},
            "metadata": {},
            "display_id": "display-1",
        },
    )

    page = store.read_output_page(
        execution_id,
        cursor=None,
        max_bytes=4096,
    )

    assert len(page.events) == 1
    event = page.events[0]
    assert event.event_type == "display_data"
    assert event.mime_type == "image/png"
    assert event.display_id == "display-1"
    assert event.artifact.byte_size == len(png)
    assert event.artifact.sha256 == f"sha256:{hashlib.sha256(png).hexdigest()}"
    artifact = Path(event.artifact.path)
    assert artifact.read_bytes() == png
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600


def test_large_text_mime_is_artifact_but_small_text_stays_cursor_readable(
    store,
):
    execution_id = "00000000-0000-4000-8000-000000000501"
    large_html = "<p>" + ("large" * 20_000) + "</p>"
    store.append_output_event(
        execution_id,
        {
            "event_type": "display_data",
            "data": {
                "text/plain": "small representation",
                "text/html": large_html,
            },
            "metadata": {},
        },
    )

    page = store.read_output_page(
        execution_id,
        cursor=None,
        max_bytes=4096,
    )

    assert [event.mime_type for event in page.events] == [
        "text/plain",
        "text/html",
    ]
    assert page.events[0].text == "small representation"
    assert page.events[0].artifact is None
    assert page.events[1].text is None
    assert Path(page.events[1].artifact.path).read_text() == large_html
    assert page.events[1].artifact.media_type == "text/html"


def test_error_clear_and_update_display_are_ordered_normalized_events(store):
    execution_id = "00000000-0000-4000-8000-000000000501"
    store.append_output_event(
        execution_id,
        {
            "event_type": "error",
            "error_name": "ValueError",
            "error_value": "bad",
            "traceback": ["line one", "line two"],
        },
    )
    store.append_output_event(
        execution_id,
        {"event_type": "clear_output", "wait": True},
    )
    store.append_output_event(
        execution_id,
        {
            "event_type": "update_display_data",
            "data": {"text/plain": "updated"},
            "metadata": {"text/plain": {"isolated": True}},
            "display_id": "display-1",
        },
    )

    page = store.read_output_page(
        execution_id,
        cursor=None,
        max_bytes=4096,
    )

    assert [event.event_type for event in page.events] == [
        "error",
        "clear_output",
        "update_display_data",
    ]
    assert page.events[0].error_name == "ValueError"
    assert page.events[0].error_value == "bad"
    assert page.events[0].traceback == ["line one", "line two"]
    assert page.events[1].wait is True
    assert page.events[2].display_id == "display-1"
    assert page.events[2].text == "updated"
    assert page.events[2].metadata == {"text/plain": {"isolated": True}}


def test_terminal_finalization_hashes_spool_and_promotes_large_output(store):
    execution_id = "00000000-0000-4000-8000-000000000501"
    text = "x" * 80_000
    store.append_output_event(
        execution_id,
        {"event_type": "stream", "stream": "stdout", "text": text},
    )

    first = store.finalize_output(execution_id)
    second = store.finalize_output(execution_id)
    record = store.get_execution(execution_id)

    assert first == second
    assert record.output_finalized_at
    assert record.output_sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert len(first) == 1
    artifact = first[0]
    assert artifact.purpose == "complete_text_output"
    assert artifact.byte_size == len(text)
    assert Path(artifact.path).read_bytes() == text.encode()
    events = store.read_output_page(
        execution_id,
        cursor=None,
        max_bytes=131_072,
    ).events
    promoted = [
        event
        for event in events
        if event.event_type == "artifact"
        and event.artifact
        and event.artifact.purpose == "complete_text_output"
    ]
    assert [event.artifact for event in promoted] == [artifact]


def test_small_or_empty_output_finalizes_without_promoted_artifact(store):
    execution_id = "00000000-0000-4000-8000-000000000501"
    assert store.finalize_output(execution_id) == []
    assert store.get_execution(execution_id).output_finalized_at


def test_page_budget_too_small_is_stable_usage_error(store):
    execution_id = "00000000-0000-4000-8000-000000000501"
    store.append_output_event(
        execution_id,
        {"event_type": "stream", "stream": "stdout", "text": "hello"},
    )

    with pytest.raises(BetterColabError) as error:
        store.read_output_page(execution_id, cursor=None, max_bytes=10)

    assert error.value.error.code == "OUTPUT_PAGE_BUDGET_TOO_SMALL"


def test_terminal_transition_requires_finalized_output(store):
    execution_id = "00000000-0000-4000-8000-000000000501"

    with pytest.raises(BetterColabError) as error:
        store.transition_execution(execution_id, ExecutionState.FINISHED)

    assert error.value.error.code == "OUTPUT_NOT_FINALIZED"

    store.finalize_output(execution_id)
    finished = store.transition_execution(execution_id, ExecutionState.FINISHED)
    assert finished.state is ExecutionState.FINISHED
