"""Synchronous public facade for Better Colab."""

from __future__ import annotations

import os
import platform
import sys
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from better_colab.capabilities import get_capabilities
from better_colab.models import CapabilitiesResult, DoctorResult
from better_colab.protocol import DEFAULT_EXECUTION_LIMIT, SCHEMA_VERSION


def _package_version() -> str:
    try:
        return version("better-google-colab-cli")
    except PackageNotFoundError:
        return "unknown"


def _state_path() -> Path:
    root = Path(
        os.environ.get(
            "XDG_STATE_HOME",
            Path.home() / ".local" / "state",
        )
    )
    return root / "better-colab" / "controller.sqlite3"


def _runtime_dir() -> Path:
    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        return Path(configured) / "better-colab"
    return Path(tempfile.gettempdir()) / f"better-colab-{os.getuid()}"


class BetterColabClient:
    """Thread-compatible synchronous client with no terminal dependencies."""

    def capabilities(
        self,
        command: str | None = None,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_EXECUTION_LIMIT,
    ) -> CapabilitiesResult:
        return get_capabilities(command=command, cursor=cursor, limit=limit)

    def doctor(self) -> DoctorResult:
        runtime_dir = _runtime_dir()
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
            state_path=str(_state_path()),
            runtime_dir=str(runtime_dir),
        )

