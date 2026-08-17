"""Bridges retained interactive commands to controller-owned session safety."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from better_colab.durable_commands import _client_from_cli_state


@contextlib.contextmanager
def compatibility_session_lease(
    name: str,
    *,
    endpoint: str | None = None,
    reconnect: bool = True,
) -> Iterator[None]:
    """Temporarily hand a controller-owned kernel to legacy code when known."""
    with _client_from_cli_state() as client:
        if endpoint is None:
            lease_name = name if client.store.get_session(name) is not None else None
        else:
            lease_name = next(
                (
                    session.name
                    for session in client.store.list_sessions()
                    if session.endpoint == endpoint
                ),
                None,
            )
        if lease_name is None:
            yield
            return
        with client.session_lease(lease_name, reconnect=reconnect):
            yield
