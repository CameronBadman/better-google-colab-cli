"""Bridges retained interactive commands to controller-owned session safety."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from better_colab.durable_commands import _client_from_cli_state


@contextlib.contextmanager
def compatibility_session_lease(
    name: str,
    *,
    reconnect: bool = True,
) -> Iterator[None]:
    """Temporarily hand one kernel from the controller to legacy code."""
    with _client_from_cli_state() as client:
        with client.session_lease(name, reconnect=reconnect):
            yield
