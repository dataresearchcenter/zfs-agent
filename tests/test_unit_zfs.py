import os
import socket
import threading
from unittest.mock import patch

import orjson
import pytest

from zfs_agent.agent import (
    ANY_UID,
    get_peer_uid,
    handle_connection,
    handle_request,
    validate_dataset,
)
from zfs_agent.client import zfs_create, zfs_create_socket

# --- Validation tests ---


class TestValidateDataset:
    def test_valid_name(self):
        assert validate_dataset("tank/lakehouse/my_dataset/archive", None) is None

    def test_valid_with_pool(self):
        assert validate_dataset("tank/lakehouse/ds", "tank/lakehouse") is None

    def test_empty_name(self):
        assert validate_dataset("", None) is not None

    def test_invalid_chars_in_leaf(self):
        assert validate_dataset("tank/ds; rm -rf /", None) is not None

    def test_invalid_chars_in_parent(self):
        assert validate_dataset("tank/ds; rm/leaf", None) is not None

    def test_path_traversal(self):
        assert validate_dataset("tank/../etc/shadow", None) is not None

    def test_pool_mismatch(self):
        err = validate_dataset("other/pool/ds", "tank/lakehouse")
        assert err is not None
        assert "not under pool" in err

    def test_hyphens_allowed_in_parents(self):
        assert validate_dataset("tank/lakehouse-dev/my_dataset", None) is None

    def test_dots_allowed_in_parents(self):
        assert validate_dataset("tank/lake.house/my_dataset", None) is None

    def test_uppercase_allowed_in_parents(self):
        assert validate_dataset("Tank/Lakehouse/my_dataset", None) is None

    def test_pool_prefix_alone_is_not_enough(self):
        """A sibling that merely shares the pool's prefix stays out."""
        assert validate_dataset("tank/database/x", "tank/data") is not None
        assert validate_dataset("tank/data-evil/x", "tank/data") is not None
        assert validate_dataset("tank/dataX", "tank/data") is not None

    def test_pool_itself_allowed(self):
        assert validate_dataset("tank/data", "tank/data") is None

    def test_trailing_newline_rejected(self):
        assert validate_dataset("tank/ds\n", "tank") is not None

    @pytest.mark.parametrize("dataset", [123, ["tank", "ds"], None, {"a": 1}, True])
    def test_non_string_rejected(self, dataset):
        err = validate_dataset(dataset, None)
        assert err is not None
        assert "must be a string" in err


# --- Request handler tests ---


class TestHandleRequest:
    @patch("zfs_agent.agent.zfs_create_local")
    def test_create_success(self, mock_create):
        resp = handle_request(
            {
                "action": "create",
                "dataset": "tank/ds",
                "props": {"compression": "zstd"},
            },
            None,
        )
        assert resp == {"ok": True}
        mock_create.assert_called_once_with(
            "tank/ds", {"compression": "zstd"}, exist_ok=True, owner=None
        )

    @patch("zfs_agent.agent.zfs_create_local")
    def test_create_with_owner(self, mock_create):
        resp = handle_request(
            {
                "action": "create",
                "dataset": "tank/ds",
                "props": {"compression": "zstd"},
            },
            None,
            owner="1000:1000",
        )
        assert resp == {"ok": True}
        mock_create.assert_called_once_with(
            "tank/ds", {"compression": "zstd"}, exist_ok=True, owner="1000:1000"
        )

    @patch("zfs_agent.agent.zfs_create_local")
    def test_create_no_props(self, mock_create):
        resp = handle_request({"action": "create", "dataset": "tank/ds"}, None)
        assert resp == {"ok": True}
        mock_create.assert_called_once_with("tank/ds", {}, exist_ok=True, owner=None)

    def test_unknown_action(self):
        resp = handle_request({"action": "destroy", "dataset": "tank/ds"}, None)
        assert resp["ok"] is False
        assert "unknown action" in resp["error"]

    def test_invalid_dataset_rejected(self):
        resp = handle_request(
            {"action": "create", "dataset": "tank/ds; rm -rf /"},
            None,
        )
        assert resp["ok"] is False
        assert "invalid path component" in resp["error"]

    def test_pool_enforced(self):
        resp = handle_request(
            {"action": "create", "dataset": "rogue/pool"},
            "tank/lakehouse",
        )
        assert resp["ok"] is False
        assert "not under pool" in resp["error"]

    @patch(
        "zfs_agent.agent.zfs_create_local",
        side_effect=RuntimeError("boom"),
    )
    def test_create_failure_forwarded(self, _mock_create):
        resp = handle_request(
            {"action": "create", "dataset": "tank/ds"},
            None,
        )
        assert resp["ok"] is False
        assert "boom" in resp["error"]


# --- Socket integration tests ---


def _make_socketpair():
    """Create a connected pair of Unix sockets."""
    return socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)


class TestHandleConnection:
    @patch("zfs_agent.agent.zfs_create_local")
    def test_roundtrip(self, _mock_create):
        client, server_conn = _make_socketpair()
        try:
            request = orjson.dumps({"action": "create", "dataset": "tank/ds"})
            client.sendall(request + b"\n")
            client.shutdown(socket.SHUT_WR)

            handle_connection(server_conn, None, allowed_uid=ANY_UID)

            response = orjson.loads(client.makefile().readline())
            assert response["ok"] is True
        finally:
            client.close()

    @patch("zfs_agent.agent.zfs_create_local")
    def test_invalid_json(self, _mock_create):
        client, server_conn = _make_socketpair()
        try:
            client.sendall(b"not json\n")
            client.shutdown(socket.SHUT_WR)

            handle_connection(server_conn, None, allowed_uid=ANY_UID)

            response = orjson.loads(client.makefile().readline())
            assert response["ok"] is False
            assert "invalid JSON" in response["error"]
        finally:
            client.close()


class TestPeerAuth:
    """SO_PEERCRED-based peer authentication on the ZFS socket."""

    def test_get_peer_uid_returns_current_uid(self):
        """A socketpair has both ends in this process, so the reported
        peer UID is ours."""
        a, b = _make_socketpair()
        try:
            assert get_peer_uid(a) == os.getuid()
            assert get_peer_uid(b) == os.getuid()
        finally:
            a.close()
            b.close()

    @patch("zfs_agent.agent.zfs_create_local")
    def test_handle_connection_accepts_matching_uid(self, _mock_create):
        client, server_conn = _make_socketpair()
        try:
            request = orjson.dumps({"action": "create", "dataset": "tank/ds"})
            client.sendall(request + b"\n")
            client.shutdown(socket.SHUT_WR)

            handle_connection(server_conn, None, allowed_uid=os.getuid())

            response = orjson.loads(client.makefile().readline())
            assert response["ok"] is True
        finally:
            client.close()

    @patch("zfs_agent.agent.zfs_create_local")
    def test_handle_connection_rejects_mismatched_uid(self, mock_create):
        """A UID different from the connecting process is rejected
        without ever reaching ``zfs_create_local``."""
        client, server_conn = _make_socketpair()
        try:
            request = orjson.dumps({"action": "create", "dataset": "tank/ds"})
            client.sendall(request + b"\n")
            client.shutdown(socket.SHUT_WR)

            # Pick a UID that is definitely not ours.
            wrong_uid = os.getuid() + 99999
            handle_connection(server_conn, None, allowed_uid=wrong_uid)

            response = orjson.loads(client.makefile().readline())
            assert response["ok"] is False
            assert "unauthorized peer uid" in response["error"]
            mock_create.assert_not_called()
        finally:
            client.close()

    @patch("zfs_agent.agent.zfs_create_local")
    def test_handle_connection_skips_auth_for_any_uid(self, _mock_create):
        """``ANY_UID`` is the explicit opt-out; there is no fail-open
        default, so forgetting the argument is a TypeError."""
        client, server_conn = _make_socketpair()
        try:
            request = orjson.dumps({"action": "create", "dataset": "tank/ds"})
            client.sendall(request + b"\n")
            client.shutdown(socket.SHUT_WR)

            handle_connection(server_conn, None, allowed_uid=ANY_UID)

            response = orjson.loads(client.makefile().readline())
            assert response["ok"] is True
        finally:
            client.close()


class _FakeAgent:
    """A one-shot agent thread that records the request it received.

    Assertions belong in the test, not in the thread: an AssertionError
    raised in a worker only reaches ``threading.excepthook`` and would be
    misattributed to whatever fails next.
    """

    def __init__(self, sock_path, response):
        self.response = response  # None: hang up without replying
        self.request = None
        self.error = None
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(sock_path)
        self.server.listen(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        try:
            conn, _ = self.server.accept()
            with conn, conn.makefile("rb") as fh:
                self.request = orjson.loads(fh.readline())
                if self.response is not None:
                    conn.sendall(orjson.dumps(self.response) + b"\n")
        except Exception as e:
            self.error = e

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, *_):
        self.thread.join(timeout=5)
        alive = self.thread.is_alive()
        self.server.close()
        if exc_type is None:
            assert not alive, "agent thread never finished"
            if self.error is not None:
                raise self.error


class TestZfsCreateSocket:
    """Test the socket client against a mock agent running in a thread."""

    def test_success(self, tmp_path):
        sock_path = str(tmp_path / "test.sock")
        with _FakeAgent(sock_path, {"ok": True}) as agent:
            zfs_create_socket(sock_path, "tank/test", compression="zstd")
        assert agent.request == {
            "action": "create",
            "dataset": "tank/test",
            "props": {"compression": "zstd"},
            "exist_ok": True,
        }

    def test_exist_ok_goes_on_the_wire(self, tmp_path):
        """Otherwise the flag means different things locally and remotely."""
        sock_path = str(tmp_path / "test.sock")
        with _FakeAgent(sock_path, {"ok": True}) as agent:
            zfs_create_socket(sock_path, "tank/test", exist_ok=False)
        assert agent.request["exist_ok"] is False

    def test_error_response(self, tmp_path):
        sock_path = str(tmp_path / "test.sock")
        with _FakeAgent(sock_path, {"ok": False, "error": "permission denied"}):
            with pytest.raises(RuntimeError, match="permission denied"):
                zfs_create_socket(sock_path, "tank/test")

    def test_agent_hangs_up_without_replying(self, tmp_path):
        sock_path = str(tmp_path / "test.sock")
        with _FakeAgent(sock_path, None):
            with pytest.raises(RuntimeError, match="no response"):
                zfs_create_socket(sock_path, "tank/test")


class TestZfsCreateDispatch:
    """Test that zfs_create dispatches to socket or local based on settings."""

    @patch("zfs_agent.zfs.zfs_create_local")
    @patch("zfs_agent.settings.Settings")
    def test_dispatch_local(self, mock_settings_cls, mock_local):
        mock_settings_cls.return_value.zfs_socket = None
        mock_settings_cls.return_value.zfs_owner = None
        zfs_create("tank/ds", **{"compression": "zstd"})
        mock_local.assert_called_once_with(
            "tank/ds", {"compression": "zstd"}, True, None
        )

    @patch("zfs_agent.zfs.zfs_create_local")
    @patch("zfs_agent.settings.Settings")
    def test_dispatch_local_with_owner(self, mock_settings_cls, mock_local):
        mock_settings_cls.return_value.zfs_socket = None
        mock_settings_cls.return_value.zfs_owner = "1000:1000"
        zfs_create("tank/ds", **{"compression": "zstd"})
        mock_local.assert_called_once_with(
            "tank/ds", {"compression": "zstd"}, True, "1000:1000"
        )

    @patch("zfs_agent.client.zfs_create_socket")
    @patch("zfs_agent.settings.Settings")
    def test_dispatch_socket(self, mock_settings_cls, mock_socket):
        mock_settings_cls.return_value.zfs_socket = "/run/zfs.sock"
        zfs_create("tank/ds", **{"compression": "zstd"})
        mock_socket.assert_called_once_with(
            "/run/zfs.sock", "tank/ds", exist_ok=True, compression="zstd"
        )


# --- Hardening regression tests ---


DANGEROUS_PROPS = [
    "mountpoint",
    "sharenfs",
    "sharesmb",
    "setuid",
    "exec",
    "devices",
    "keylocation",
    "canmount",
]


class TestPropsAllowlist:
    """Client-supplied properties must not steer the root-side create."""

    @pytest.mark.parametrize("prop", DANGEROUS_PROPS)
    @patch("zfs_agent.agent.zfs_create_local")
    def test_dangerous_prop_rejected(self, mock_create, prop):
        resp = handle_request(
            {"action": "create", "dataset": "tank/ds", "props": {prop: "/etc"}},
            None,
        )
        assert resp["ok"] is False
        assert "not allowed" in resp["error"]
        mock_create.assert_not_called()

    @patch("zfs_agent.agent.zfs_create_local")
    def test_tuning_props_allowed(self, mock_create):
        props = {"compression": "zstd-9", "recordsize": "1M", "atime": "off"}
        resp = handle_request(
            {"action": "create", "dataset": "tank/ds", "props": props}, None
        )
        assert resp == {"ok": True}
        mock_create.assert_called_once_with("tank/ds", props, exist_ok=True, owner=None)

    @patch("zfs_agent.agent.zfs_create_local")
    def test_int_value_stringified(self, mock_create):
        resp = handle_request(
            {"action": "create", "dataset": "tank/ds", "props": {"copies": 2}}, None
        )
        assert resp == {"ok": True}
        mock_create.assert_called_once_with(
            "tank/ds", {"copies": "2"}, exist_ok=True, owner=None
        )

    @patch("zfs_agent.agent.zfs_create_local")
    def test_allowlist_extended_by_env(self, mock_create, monkeypatch):
        monkeypatch.setenv("ZFS_EXTRA_PROPS", "canmount, readonly")
        resp = handle_request(
            {"action": "create", "dataset": "tank/ds", "props": {"canmount": "off"}},
            None,
        )
        assert resp == {"ok": True}

    @pytest.mark.parametrize("value", ["zstd\n", "/etc/shadow", {"a": 1}, [1], True])
    @patch("zfs_agent.agent.zfs_create_local")
    def test_bad_value_rejected(self, mock_create, value):
        resp = handle_request(
            {
                "action": "create",
                "dataset": "tank/ds",
                "props": {"compression": value},
            },
            None,
        )
        assert resp["ok"] is False
        assert "invalid value" in resp["error"]
        mock_create.assert_not_called()

    def test_props_not_a_dict(self):
        resp = handle_request(
            {"action": "create", "dataset": "tank/ds", "props": ["compression=zstd"]},
            None,
        )
        assert resp["ok"] is False
        assert "props must be a dict" in resp["error"]


class TestMalformedRequests:
    """Anything a client can send must produce a response, not an exception."""

    @pytest.mark.parametrize("data", [None, [], 5, "hi", True])
    def test_non_object_request(self, data):
        resp = handle_request(data, None)
        assert resp["ok"] is False
        assert "JSON object" in resp["error"]

    @pytest.mark.parametrize("dataset", [123, ["tank", "ds"], None, {"a": 1}])
    def test_non_string_dataset(self, dataset):
        resp = handle_request({"action": "create", "dataset": dataset}, None)
        assert resp["ok"] is False
        assert "must be a string" in resp["error"]

    def test_exist_ok_must_be_bool(self):
        resp = handle_request(
            {"action": "create", "dataset": "tank/ds", "exist_ok": "yes"}, None
        )
        assert resp["ok"] is False
        assert "exist_ok" in resp["error"]

    @patch("zfs_agent.agent.zfs_create_local")
    def test_exist_ok_forwarded(self, mock_create):
        handle_request(
            {"action": "create", "dataset": "tank/ds", "exist_ok": False}, None
        )
        mock_create.assert_called_once_with("tank/ds", {}, exist_ok=False, owner=None)

    @patch("zfs_agent.agent.zfs_create_local", side_effect=OSError("no zfs"))
    def test_unexpected_error_not_leaked(self, _mock_create):
        """Non-RuntimeError failures answer generically instead of escaping."""
        resp = handle_request({"action": "create", "dataset": "tank/ds"}, None)
        assert resp["ok"] is False
        assert resp["error"] == "internal error: OSError"


class TestConnectionRobustness:
    """A single bad request must never escape into the accept loop."""

    @patch("zfs_agent.agent.zfs_create_local")
    def test_non_utf8_input(self, mock_create):
        client, server_conn = _make_socketpair()
        try:
            client.sendall(b'{"action":"create","dataset":"a\xff"}\n')
            client.shutdown(socket.SHUT_WR)

            handle_connection(server_conn, None, allowed_uid=ANY_UID)

            response = orjson.loads(client.makefile("rb").readline())
            assert response["ok"] is False
            mock_create.assert_not_called()
        finally:
            client.close()

    @patch("zfs_agent.agent._MAX_REQUEST", 32)
    @patch("zfs_agent.agent.zfs_create_local")
    def test_oversized_request(self, mock_create):
        client, server_conn = _make_socketpair()
        try:
            client.sendall(b"x" * 200 + b"\n")
            client.shutdown(socket.SHUT_WR)

            handle_connection(server_conn, None, allowed_uid=ANY_UID)

            response = orjson.loads(client.makefile("rb").readline())
            assert response["ok"] is False
            assert "too large" in response["error"]
            mock_create.assert_not_called()
        finally:
            client.close()

    @patch("zfs_agent.agent._REQUEST_TIMEOUT", 0.05)
    @patch("zfs_agent.agent.zfs_create_local")
    def test_silent_client_times_out(self, mock_create):
        """A peer that connects and says nothing must not wedge the agent."""
        client, server_conn = _make_socketpair()
        try:
            handle_connection(server_conn, None, allowed_uid=ANY_UID)
            mock_create.assert_not_called()
        finally:
            client.close()

    @patch("zfs_agent.agent.zfs_create_local")
    def test_client_hangup_before_response(self, mock_create):
        """The create already happened; the peer just left."""
        client, server_conn = _make_socketpair()
        request = orjson.dumps({"action": "create", "dataset": "tank/ds"})
        client.sendall(request + b"\n")
        client.shutdown(socket.SHUT_RDWR)
        client.close()

        handle_connection(server_conn, None, allowed_uid=ANY_UID)

        mock_create.assert_called_once()
