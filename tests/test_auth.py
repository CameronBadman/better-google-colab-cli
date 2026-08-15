# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import contextlib
import os
from unittest.mock import MagicMock, mock_open, patch

import pytest

from colab_cli.auth import (
    REMOTE_REDIRECT_URI,
    TOKEN_CONFIG_PATH,
    AuthProvider,
    _get_google_auth_credentials,
    get_credentials,
)
from colab_cli.private_files import PrivatePathError


@pytest.fixture
def mock_deps(mocker):
    m_exists = mocker.patch("os.path.exists")
    m_makedirs = mocker.patch("os.makedirs")
    m_creds_cls = mocker.patch("colab_cli.auth.Credentials")
    m_flow_cls = mocker.patch("colab_cli.auth.InstalledAppFlow")
    m_request = mocker.patch("colab_cli.auth.Request")
    m_session = mocker.patch("colab_cli.auth.requests.AuthorizedSession")
    m_resources = mocker.patch("colab_cli.auth.resources")
    m_ensure_file = mocker.patch("colab_cli.auth.ensure_private_file")
    m_ensure_dir = mocker.patch("colab_cli.auth.ensure_private_directory")
    m_atomic_write = mocker.patch("colab_cli.auth.atomic_write_private_text")
    m_lock = MagicMock()
    m_lock.write_lock.return_value = contextlib.nullcontext()
    mocker.patch("colab_cli.auth.filelock.ReadWriteLock", return_value=m_lock)

    # By default, pretend oauth config doesn't exist anywhere
    m_exists.return_value = False
    m_resources.files.return_value.joinpath.return_value.is_file.return_value = False

    return {
        "exists": m_exists,
        "makedirs": m_makedirs,
        "creds_cls": m_creds_cls,
        "flow_cls": m_flow_cls,
        "request": m_request,
        "session": m_session,
        "resources": m_resources,
        "ensure_file": m_ensure_file,
        "ensure_dir": m_ensure_dir,
        "atomic_write": m_atomic_write,
        "lock": m_lock,
    }


def test_get_credentials_no_config(mock_deps):
    with pytest.raises(
        FileNotFoundError,
        match="Client OAuth config not found.*and no inlined config available",
    ):
        get_credentials("missing_config.json", provider=AuthProvider.OAUTH2)


def test_get_credentials_valid_token(mock_deps):
    # Setup token exists
    def exists_side_effect(path):
        return path in ["dummy_config.json", TOKEN_CONFIG_PATH]

    mock_deps["exists"].side_effect = exists_side_effect

    # Valid creds
    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_deps["creds_cls"].from_authorized_user_file.return_value = mock_creds

    # Mock open for config
    m_open = mock_open(read_data='{"web":{"client_id":"id"}}')
    with patch("builtins.open", m_open):
        res = get_credentials("dummy_config.json", provider=AuthProvider.OAUTH2)

    mock_deps["creds_cls"].from_authorized_user_file.assert_called_once()
    mock_deps["session"].assert_called_once_with(mock_creds)
    assert res == mock_deps["session"].return_value


def test_get_credentials_expired_token_refresh(mock_deps):
    def exists_side_effect(path):
        return path in ["dummy_config.json", TOKEN_CONFIG_PATH]

    mock_deps["exists"].side_effect = exists_side_effect

    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "some_token"
    mock_creds.to_json.return_value = '{"token":"refreshed"}'
    mock_deps["creds_cls"].from_authorized_user_file.return_value = mock_creds

    m_open = mock_open(read_data='{"web":{"client_id":"id"}}')
    with patch("builtins.open", m_open):
        res = get_credentials("dummy_config.json", provider=AuthProvider.OAUTH2)

    mock_creds.refresh.assert_called_once()
    mock_deps["atomic_write"].assert_called_once_with(
        TOKEN_CONFIG_PATH, '{"token":"refreshed"}'
    )
    assert res == mock_deps["session"].return_value


def test_get_credentials_no_token(mock_deps, mocker):
    """With no cached token, the remote copy-paste flow runs and exchanges code."""
    mock_deps["exists"].side_effect = lambda path: path == "dummy_config.json"

    mock_flow = MagicMock()
    mock_creds_new = MagicMock()
    mock_creds_new.to_json.return_value = '{"token":"new"}'
    mock_flow.authorization_url.return_value = ("https://auth.example/url", "state")
    mock_flow.credentials = mock_creds_new
    mock_deps["flow_cls"].from_client_config.return_value = mock_flow

    # User pastes the authorization code at the prompt.
    mocker.patch("colab_cli.auth.input", create=True, return_value="pasted-code")

    m_open = mock_open(read_data='{"web":{"client_id":"id"}}')
    with patch("builtins.open", m_open):
        get_credentials("dummy_config.json", provider=AuthProvider.OAUTH2)

    mock_deps["flow_cls"].from_client_config.assert_called_once()
    # No localhost server should ever be started.
    mock_flow.run_local_server.assert_not_called()
    # Remote flow: OOB-free redirect + token_usage=remote consent param.
    assert mock_flow.redirect_uri == REMOTE_REDIRECT_URI
    _, kwargs = mock_flow.authorization_url.call_args
    assert kwargs.get("token_usage") == "remote"
    # The pasted code is exchanged for a token.
    mock_flow.fetch_token.assert_called_once_with(code="pasted-code")


def test_remote_redirect_is_not_oob():
    """Guard against regressing to the dead OOB redirect URI."""
    assert REMOTE_REDIRECT_URI.startswith("https://")
    assert "oob" not in REMOTE_REDIRECT_URI


def test_get_credentials_fallback_config(mock_deps):
    # Setup: config_path doesn't exist, but fallback file does
    mock_deps["exists"].return_value = False
    m_file = mock_deps["resources"].files.return_value.joinpath.return_value
    m_file.is_file.return_value = True
    m_file.read_text.return_value = '{"installed":{"client_id":"fallback_id"}}'

    # Valid creds in token
    mock_deps["exists"].side_effect = lambda path: path == TOKEN_CONFIG_PATH
    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_deps["creds_cls"].from_authorized_user_file.return_value = mock_creds

    res = get_credentials("missing_config.json", provider=AuthProvider.OAUTH2)

    mock_deps["resources"].files.assert_called_once_with("colab_cli")
    m_file.is_file.assert_called_once()
    m_file.read_text.assert_called_once()
    mock_deps["creds_cls"].from_authorized_user_file.assert_called_once()
    assert res == mock_deps["session"].return_value


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_new_token_cache_and_lock_are_private_under_permissive_umask(
    monkeypatch, mocker, tmp_path
):
    import colab_cli.auth as auth_module

    config = tmp_path / "oauth.json"
    config.write_text('{"installed":{"client_id":"id"}}', encoding="utf-8")
    token = tmp_path / "token.json"
    monkeypatch.setattr(auth_module, "TOKEN_CONFIG_PATH", str(token))

    credentials = MagicMock()
    credentials.valid = True
    credentials.to_json.return_value = '{"refresh_token":"secret"}'
    mocker.patch(auth_module.__name__ + "._run_remote_flow", return_value=credentials)

    previous = os.umask(0)
    try:
        assert _get_google_auth_credentials(str(config)) is credentials
    finally:
        os.umask(previous)

    assert token.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "token.json.lock").stat().st_mode & 0o777 == 0o600
    assert token.read_text(encoding="utf-8") == '{"refresh_token":"secret"}'


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_token_cache_rejects_symlink(monkeypatch, tmp_path):
    import colab_cli.auth as auth_module

    config = tmp_path / "oauth.json"
    config.write_text('{"installed":{"client_id":"id"}}', encoding="utf-8")
    target = tmp_path / "target.json"
    target.write_text('{"refresh_token":"sentinel"}', encoding="utf-8")
    token = tmp_path / "token.json"
    token.symlink_to(target)
    monkeypatch.setattr(auth_module, "TOKEN_CONFIG_PATH", str(token))

    with pytest.raises(RuntimeError, match="unsafe|symbolic|symlink"):
        _get_google_auth_credentials(str(config))

    assert target.read_text(encoding="utf-8") == '{"refresh_token":"sentinel"}'


def test_token_cache_rejects_unsafe_path_during_atomic_save(mock_deps):
    mock_deps["exists"].side_effect = lambda path: path == "dummy_config.json"
    credentials = MagicMock()
    credentials.to_json.return_value = '{"refresh_token":"secret"}'
    mock_deps["flow_cls"].from_client_config.return_value.credentials = credentials
    mock_deps[
        "flow_cls"
    ].from_client_config.return_value.authorization_url.return_value = (
        "https://auth.example/url",
        "state",
    )
    mock_deps["atomic_write"].side_effect = PrivatePathError("unsafe")

    with (
        patch("builtins.open", mock_open(read_data='{"web":{"client_id":"id"}}')),
        patch("colab_cli.auth.input", return_value="pasted-code"),
        pytest.raises(RuntimeError, match="unsafe OAuth token cache path"),
    ):
        _get_google_auth_credentials("dummy_config.json")
