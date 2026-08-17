from unittest.mock import MagicMock

from better_colab.compatibility import compatibility_session_lease


def _client_context(mocker):
    context = MagicMock()
    client = context.__enter__.return_value
    mocker.patch(
        "better_colab.compatibility._client_from_cli_state",
        return_value=context,
    )
    return client


def test_legacy_only_session_does_not_require_a_controller_lease(mocker):
    client = _client_context(mocker)
    client.store.list_sessions.return_value = []

    with compatibility_session_lease("legacy", endpoint="legacy-endpoint"):
        pass

    client.session_lease.assert_not_called()


def test_matching_endpoint_leases_the_controller_session(mocker):
    client = _client_context(mocker)
    durable = MagicMock(name="durable_session")
    durable.name = "durable-name"
    durable.endpoint = "shared-endpoint"
    client.store.list_sessions.return_value = [durable]

    with compatibility_session_lease("legacy-name", endpoint="shared-endpoint"):
        pass

    client.session_lease.assert_called_once_with("durable-name", reconnect=True)
    client.session_lease.return_value.__enter__.assert_called_once_with()
    client.session_lease.return_value.__exit__.assert_called_once()


def test_name_fallback_leases_a_known_controller_session(mocker):
    client = _client_context(mocker)
    client.store.get_session.return_value = MagicMock()

    with compatibility_session_lease("durable-name", reconnect=False):
        pass

    client.session_lease.assert_called_once_with("durable-name", reconnect=False)
