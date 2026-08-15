import logging
import os
from pathlib import Path

import pytest

from colab_cli.common import setup_logging
from colab_cli.private_files import PrivatePathError


def _close_colab_handlers():
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_colab_cli_handler", False):
            root.removeHandler(handler)
            handler.close()


@pytest.fixture(autouse=True)
def cleanup_handlers():
    _close_colab_handlers()
    yield
    _close_colab_handlers()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_setup_logging_is_private_idempotent_and_redacts(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    canary = "CANARY-secret-value-456"

    setup_logging(False)
    setup_logging(False)
    handlers = [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_colab_cli_handler", False)
    ]
    assert len(handlers) == 1

    logging.getLogger("colab_cli.test").debug(
        "Authorization: Bearer %s access_token=%s", canary, canary
    )
    handlers[0].flush()

    log_path = Path(tmp_path) / ".config" / "colab-cli" / "colab.log"
    assert log_path.parent.stat().st_mode & 0o777 == 0o700
    assert log_path.stat().st_mode & 0o777 == 0o600
    text = log_path.read_text(encoding="utf-8")
    assert canary not in text
    assert "<redacted>" in text

    handlers[0].doRollover()
    assert log_path.stat().st_mode & 0o777 == 0o600
    assert Path(f"{log_path}.1").stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_setup_logging_rejects_symlink_sink(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    log_dir = Path(tmp_path) / ".config" / "colab-cli"
    log_dir.mkdir(parents=True)
    target = tmp_path / "target.log"
    target.write_text("sentinel", encoding="utf-8")
    (log_dir / "colab.log").symlink_to(target)

    with pytest.raises(PrivatePathError):
        setup_logging(False)

    assert target.read_text(encoding="utf-8") == "sentinel"
