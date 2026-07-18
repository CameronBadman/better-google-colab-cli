"""Detached SQLite-backed keep-alive process for Better Colab sessions."""

from __future__ import annotations

import argparse
import signal
import threading
from pathlib import Path

from colab_cli.auth import AuthProvider, get_credentials
from colab_cli.client import Client, Prod

from better_colab.storage import DurableStore, ProfileSpec, StatePaths


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="better-colab-keep-alive")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--auth", choices=("oauth2", "adc"), required=True)
    parser.add_argument("--oauth-config", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--interval", type=float, default=60)
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    paths = StatePaths(
        state_dir=Path(args.state_dir),
        runtime_dir=Path(args.runtime_dir),
    )
    profile = ProfileSpec.from_values(
        config_path=args.config,
        auth_provider=args.auth,
        oauth_config_path=args.oauth_config,
    )
    stop = threading.Event()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signal_name, lambda *_args: stop.set())
    credentials = get_credentials(
        args.oauth_config,
        provider=AuthProvider(args.auth),
    )
    client = Client(Prod(), credentials)
    consecutive_client_errors = 0

    while not stop.is_set():
        with DurableStore(paths=paths, profile=profile) as store:
            session = store.get_session(args.session)
        if session is None or session.endpoint != args.endpoint:
            return 0
        try:
            client.keep_alive_assignment(args.endpoint)
            consecutive_client_errors = 0
        except Exception as error:
            response = getattr(error, "response", None)
            status = getattr(response, "status_code", None)
            if isinstance(status, int) and 400 <= status < 500:
                consecutive_client_errors += 1
                if consecutive_client_errors >= 2:
                    return 1
        stop.wait(max(1, args.interval))
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
