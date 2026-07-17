"""Synchronous public facade for Better Colab."""

from __future__ import annotations

import os
import platform
import sys
from importlib.metadata import PackageNotFoundError, version

from better_colab.capabilities import get_capabilities
from better_colab.models import CapabilitiesResult, DoctorResult, PruneResult
from better_colab.protocol import DEFAULT_EXECUTION_LIMIT, SCHEMA_VERSION
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

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def __enter__(self) -> BetterColabClient:
        return self

    def __exit__(self, *_args) -> None:
        self.close()
