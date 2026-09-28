"""Status, snapshot and the streaming send/receive actions.

A fake ``zfs`` script on ``PATH`` stands in for the real binary, so the
file-descriptor plumbing – subprocess stdio, ``SCM_RIGHTS`` over the agent
socket, real pipes – runs for real; only ZFS itself is simulated.
"""

import os
import signal
import socket
import stat
import threading
import time
from unittest.mock import patch

import pytest

from zfs_agent import zfs_abort, zfs_receive, zfs_send, zfs_snapshot, zfs_status
from zfs_agent.agent import ACTIONS, handle_connection, handle_request
from zfs_agent.cli import cli
from zfs_agent.client import zfs_create_socket
from zfs_agent.server import serve
from zfs_agent.zfs import (
    RECEIVE_EXCLUDE,
    zfs_abort_local,
    zfs_receive_local,
    zfs_send_local,
    zfs_status_local,
)

# Logs its argv, then acts on it: ``send`` writes a stream naming its
# arguments (a dry run ``-n -v -t`` prints the token's target instead),
# ``receive`` copies stdin into the file its dataset names.
FAKE_ZFS = """#!/bin/sh
echo "$*" >> "$FAKE_ZFS_DIR/calls"
case "$1" in
  send)
    case "$*" in
      "send -n -v -t 1-gone")
        echo "cannot resume send: 'tank/ds@b' used in the initial send no longer exists" >&2
        exit 1 ;;
      "send -n -v -t 1-other")
        printf 'resume token contents:\\nnvlist version: 0\\n\\ttoguid = 0x1\\n\\ttoname = tank/other@x\\n' ;;
      "send -n -v -t "*)
        printf 'resume token contents:\\nnvlist version: 0\\n\\ttoguid = 0x1\\n\\ttoname = tank/ds@b\\n' ;;
      *) printf 'stream:%s' "$*" ;;
    esac ;;
  receive) eval last=\\${$#}; cat > "$FAKE_ZFS_DIR/$(echo "$last" | tr / _)" ;;
  list)
    case "$*" in
      *missing*) echo "dataset does not exist" >&2; exit 1 ;;
      *snapshot*) printf 'tank/ds@a\\t111\\t5\\ntank/ds@b\\t18446744073709551615\\t9\\n' ;;
      *) echo "$*" ;;
    esac ;;
  get)
    case "$*" in
      *%recv*) echo "1-abc-def" ;;
      *) echo "-" ;;
    esac ;;
  snapshot) ;;
  fail) echo "boom" >&2; exit 1 ;;
esac
"""


@pytest.fixture
def fake_zfs(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "zfs"
    script.write_text(FAKE_ZFS)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_ZFS_DIR", str(tmp_path))
    monkeypatch.delenv("ZFS_SOCKET", raising=False)
    return tmp_path


def calls(fake_zfs):
    return (fake_zfs / "calls").read_text().splitlines()


def read_all(fd):
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    return b"".join(chunks)


def send_to_bytes(send, *args, **kwargs):
    """Run ``send`` into a pipe and return what came out of it."""
    r, w = os.pipe()
    try:
        errors = []

        def _run():
            try:
                send(*args, w, **kwargs)
            except Exception as e:
                errors.append(e)
            finally:
                os.close(w)

        thread = threading.Thread(target=_run)
        thread.start()
        data = read_all(r)
        thread.join()
    finally:
        os.close(r)
    if errors:
        raise errors[0]
    return data


def receive_from_bytes(receive, dataset, data, **kwargs):
    """Feed ``data`` through a pipe into ``receive``."""
    r, w = os.pipe()
    writer = threading.Thread(target=lambda: (os.write(w, data), os.close(w)))
    writer.start()
    try:
        receive(dataset, r, **kwargs)
    finally:
        writer.join()
        os.close(r)


class TestLocal:
    def test_send_incremental(self, fake_zfs):
        data = send_to_bytes(zfs_send_local, "tank/ds", snapshot="b", since="a")
        assert data == b"stream:send -c -L -e -w -i tank/ds@a tank/ds@b"

    def test_send_full(self, fake_zfs):
        data = send_to_bytes(zfs_send_local, "tank/ds", snapshot="b")
        assert data == b"stream:send -c -L -e -w tank/ds@b"

    def test_send_resume(self, fake_zfs):
        data = send_to_bytes(zfs_send_local, "tank/ds", token="1-abc")
        assert data == b"stream:send -t 1-abc"

    def test_send_resume_checks_the_token_target(self, fake_zfs):
        """``zfs send -t`` ignores the dataset – the token alone decides what
        is sent, so a token for another dataset must be refused."""
        with pytest.raises(RuntimeError, match="resume token is for tank/other"):
            send_to_bytes(zfs_send_local, "tank/ds", token="1-other")
        assert "send -t 1-other" not in calls(fake_zfs)

    def test_send_resume_of_a_gone_snapshot(self, fake_zfs):
        with pytest.raises(RuntimeError, match="no longer exists"):
            send_to_bytes(zfs_send_local, "tank/ds", token="1-gone")

    def test_abort(self, fake_zfs):
        zfs_abort_local("tank/ds")
        assert "receive -A tank/ds" in calls(fake_zfs)

    def test_send_needs_snapshot_or_token(self, fake_zfs):
        with pytest.raises(ValueError):
            zfs_send_local("tank/ds", 1)

    def test_receive(self, fake_zfs):
        receive_from_bytes(
            zfs_receive_local,
            "tank/ds",
            b"payload",
            props={"recordsize": "1M"},
            force=True,
            resumable=True,
        )
        assert (fake_zfs / "tank_ds").read_bytes() == b"payload"
        excludes = " ".join(f"-x {p}" for p in RECEIVE_EXCLUDE)
        assert f"receive -s -F -o recordsize=1M {excludes} tank/ds" in calls(fake_zfs)

    def test_receive_props_not_also_excluded(self, fake_zfs):
        """``-o`` and ``-x`` of the same property would be a zfs error."""
        receive_from_bytes(
            zfs_receive_local, "tank/ds", b"", props={"canmount": "noauto"}
        )
        received = [c for c in calls(fake_zfs) if c.startswith("receive")][0]
        assert "-o canmount=noauto" in received
        assert "-x canmount" not in received
        assert "-x mountpoint" in received

    @patch("zfs_agent.zfs._chown_mountpoint")
    def test_receive_chowns_only_new_datasets(self, mock_chown, fake_zfs):
        receive_from_bytes(zfs_receive_local, "tank/ds", b"", owner="1000:1000")
        mock_chown.assert_not_called()
        receive_from_bytes(zfs_receive_local, "tank/missing", b"", owner="1000:1000")
        mock_chown.assert_called_once_with("tank/missing", "1000:1000")

    def test_status(self, fake_zfs):
        assert zfs_status_local("tank/ds") == {
            "exists": True,
            "snapshots": [
                {"name": "a", "guid": "111", "createtxg": 5},
                {"name": "b", "guid": "18446744073709551615", "createtxg": 9},
            ],
            "resume_token": None,
        }

    def test_status_missing_dataset_reports_partial_receive(self, fake_zfs):
        """A new dataset's interrupted receive lives on under ``%recv``."""
        assert zfs_status_local("tank/missing") == {
            "exists": False,
            "snapshots": [],
            "resume_token": "1-abc-def",
        }

    def test_failure_carries_stderr(self, fake_zfs):
        from zfs_agent.zfs import _run_zfs_stream

        with pytest.raises(RuntimeError, match="zfs fail failed: boom"):
            _run_zfs_stream(["fail"])

    def test_missing_binary(self, monkeypatch):
        monkeypatch.setenv("PATH", "/nonexistent")
        with pytest.raises(RuntimeError, match="cannot run zfs"):
            send_to_bytes(zfs_send_local, "tank/ds", snapshot="b")


@pytest.fixture
def agent(fake_zfs, monkeypatch):
    """A real agent thread per connection, serving every action."""
    sock_path = str(fake_zfs / "agent.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(5)
    stop = threading.Event()

    def _serve():
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(
                target=handle_connection,
                args=(conn, "tank"),
                kwargs={"allowed_uid": os.getuid(), "actions": ACTIONS},
            ).start()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    monkeypatch.setenv("ZFS_SOCKET", sock_path)
    yield fake_zfs
    stop.set()
    server.close()


class TestOverSocket:
    """The dispatchers against a real agent: descriptors cross the socket."""

    def test_send(self, agent):
        data = send_to_bytes(zfs_send, "tank/ds", snapshot="b", since="a")
        assert data == b"stream:send -c -L -e -w -i tank/ds@a tank/ds@b"

    def test_send_resume(self, agent):
        data = send_to_bytes(zfs_send, "tank/ds", token="1-abc")
        assert data == b"stream:send -t 1-abc"

    def test_receive(self, agent):
        payload = os.urandom(1024 * 1024)  # larger than a pipe buffer
        receive_from_bytes(zfs_receive, "tank/ds", payload, force=True)
        assert (agent / "tank_ds").read_bytes() == payload

    def test_status(self, agent):
        status = zfs_status("tank/ds")
        assert [s["name"] for s in status["snapshots"]] == ["a", "b"]

    def test_snapshot(self, agent):
        zfs_snapshot("tank/ds@n", "tank/ds/archive@n")
        assert "snapshot tank/ds@n tank/ds/archive@n" in calls(agent)

    def test_abort(self, agent):
        zfs_abort("tank/ds")
        assert "receive -A tank/ds" in calls(agent)

    def test_forged_token_refused(self, agent):
        with pytest.raises(RuntimeError, match="resume token is for tank/other"):
            send_to_bytes(zfs_send, "tank/ds", token="1-other")

    def test_error_forwarded(self, agent):
        with pytest.raises(RuntimeError, match="not under pool"):
            send_to_bytes(zfs_send, "other/ds", snapshot="b")

    def test_reader_gone_fails_send(self, agent):
        """Closing the reading end must end the send, not wedge it."""
        r, w = os.pipe()
        os.close(r)
        try:
            with pytest.raises(RuntimeError, match="zfs send failed"):
                zfs_send("tank/ds", w, snapshot="b")
        finally:
            os.close(w)

    def test_agent_closes_received_descriptors(self, fake_zfs):
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(5):
            client, server_conn = socket.socketpair()
            r, w = os.pipe()
            socket.send_fds(client, [b'{"action":"status","dataset":"tank/ds"}\n'], [w])
            handle_connection(server_conn, "tank", allowed_uid=os.getuid())
            client.close()
            os.close(r)
            os.close(w)
        assert len(os.listdir("/proc/self/fd")) == before


class TestRequestValidation:
    def req(self, data, fd=None, actions=ACTIONS):
        return handle_request(data, "tank", fd=fd, actions=actions)

    def test_streaming_off_by_default(self):
        resp = handle_request({"action": "send", "dataset": "tank/ds"}, "tank", fd=5)
        assert resp == {"ok": False, "error": "action not allowed: 'send'"}

    @pytest.mark.parametrize("action", ["send", "receive"])
    def test_stream_actions_need_fd(self, action):
        resp = self.req({"action": action, "dataset": "tank/ds", "snapshot": "a"})
        assert "file descriptor" in resp["error"]

    @pytest.mark.parametrize("action", ["create", "status"])
    def test_other_actions_refuse_fd(self, action):
        resp = self.req({"action": action, "dataset": "tank/ds"}, fd=5)
        assert "file descriptor" in resp["error"]

    @pytest.mark.parametrize(
        "extra",
        [
            {"snapshot": "a; rm -rf /"},
            {"snapshot": None},
            {"snapshot": "b", "since": "../a"},
            {"token": "1-abc; rm"},
            {"token": 5},
        ],
    )
    @patch("zfs_agent.agent.zfs_send_local")
    def test_bad_send_rejected(self, mock_send, extra):
        resp = self.req({"action": "send", "dataset": "tank/ds", **extra}, fd=5)
        assert resp["ok"] is False
        mock_send.assert_not_called()

    @pytest.mark.parametrize(
        "extra",
        [
            {"force": "yes"},
            {"resumable": 1},
            {"props": {"mountpoint": "/etc"}},
        ],
    )
    @patch("zfs_agent.agent.zfs_receive_local")
    def test_bad_receive_rejected(self, mock_receive, extra):
        resp = self.req({"action": "receive", "dataset": "tank/ds", **extra}, fd=5)
        assert resp["ok"] is False
        mock_receive.assert_not_called()

    @patch("zfs_agent.agent.zfs_snapshot_local")
    @patch("zfs_agent.agent.zfs_send_local")
    def test_colons_in_snapshot_names(self, mock_send, mock_snapshot):
        """sanoid / zrepl timestamps: a common base may be named like this."""
        name = "autosnap_2026-09-28_11:00:00_hourly"
        resp = self.req(
            {"action": "send", "dataset": "tank/ds", "snapshot": "b", "since": name},
            fd=5,
        )
        assert resp == {"ok": True}
        mock_send.assert_called_once_with("tank/ds", 5, snapshot="b", since=name)
        resp = self.req({"action": "snapshot", "snapshots": [f"tank/ds@{name}"]})
        assert resp == {"ok": True}

    def test_colons_stay_out_of_dataset_names(self):
        resp = self.req({"action": "status", "dataset": "tank/a:b"})
        assert "invalid path component" in resp["error"]

    @patch("zfs_agent.agent.zfs_abort_local")
    def test_abort_off_by_default(self, mock_abort):
        resp = handle_request({"action": "abort", "dataset": "tank/ds"}, "tank")
        assert resp == {"ok": False, "error": "action not allowed: 'abort'"}
        resp = self.req({"action": "abort", "dataset": "other/ds"})
        assert "not under pool" in resp["error"]
        mock_abort.assert_not_called()

    @patch("zfs_agent.agent.zfs_receive_local")
    def test_receive_forwarded(self, mock_receive):
        resp = self.req(
            {
                "action": "receive",
                "dataset": "tank/ds",
                "props": {"recordsize": "1M"},
                "force": True,
                "resumable": True,
            },
            fd=5,
        )
        assert resp == {"ok": True}
        mock_receive.assert_called_once_with(
            "tank/ds",
            5,
            {"recordsize": "1M"},
            force=True,
            resumable=True,
            owner=None,
        )

    @pytest.mark.parametrize(
        "snapshots",
        [[], "tank/ds@a", ["other/ds@a"], ["tank/ds"], ["tank/ds@a@b"], ["tank/ds@"]],
    )
    @patch("zfs_agent.agent.zfs_snapshot_local")
    def test_bad_snapshot_rejected(self, mock_snapshot, snapshots):
        resp = self.req({"action": "snapshot", "snapshots": snapshots})
        assert resp["ok"] is False
        mock_snapshot.assert_not_called()

    @patch("zfs_agent.agent.zfs_status_local")
    def test_status_dataset_validated(self, mock_status):
        resp = self.req({"action": "status", "dataset": "other/ds"})
        assert "not under pool" in resp["error"]
        mock_status.assert_not_called()


class TestServer:
    def test_slow_request_does_not_block_others(self, tmp_path):
        """A connection in a long send must not hold up a create.

        ``serve`` runs on the main thread and is stopped by a real SIGTERM:
        closing the listener from another thread would not interrupt a
        blocked ``accept()`` on Linux – the signal does.
        """
        sock_path = str(tmp_path / "agent.sock")
        release = threading.Event()
        results = {}

        def fake_create(dataset, props, exist_ok, owner):
            if dataset == "tank/slow":
                assert release.wait(5)

        def client():
            try:
                for _ in range(100):
                    if os.path.exists(sock_path):
                        break
                    time.sleep(0.02)
                slow = threading.Thread(
                    target=zfs_create_socket, args=(sock_path, "tank/slow")
                )
                slow.start()
                zfs_create_socket(sock_path, "tank/fast")
                results["slow_pending"] = slow.is_alive()
                release.set()
                slow.join(5)
                results["slow_done"] = not slow.is_alive()
            finally:
                release.set()
                os.kill(os.getpid(), signal.SIGTERM)

        handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        thread = threading.Thread(target=client)
        with patch("zfs_agent.agent.zfs_create_local", fake_create):
            thread.start()
            try:
                serve(sock_path, "tank", allowed_uid=os.getuid())
            finally:
                for signum, handler in handlers.items():
                    signal.signal(signum, handler)
        thread.join(5)

        assert results == {"slow_pending": True, "slow_done": True}
        assert not os.path.exists(sock_path)


class TestCliActions:
    def test_unknown_action_is_usage_error(self, monkeypatch):
        monkeypatch.setenv("ZFS_SOCKET", "/tmp/x.sock")
        monkeypatch.setenv("ZFS_POOL", "tank")
        with pytest.raises(SystemExit) as exc:
            cli(["--actions", "create,destroy"])
        assert exc.value.code == 2

    @pytest.mark.parametrize(
        "argv,env,expected",
        [
            ([], None, {"create"}),
            (["--actions", "create,send"], None, {"create", "send"}),
            ([], "status, receive", {"status", "receive"}),
            (["--actions", "send"], "status", {"send"}),
        ],
    )
    @patch("zfs_agent.cli.serve")
    def test_actions_resolved(self, mock_serve, monkeypatch, argv, env, expected):
        monkeypatch.setenv("ZFS_SOCKET", "/tmp/x.sock")
        monkeypatch.setenv("ZFS_POOL", "tank")
        if env is None:
            monkeypatch.delenv("ZFS_ACTIONS", raising=False)
        else:
            monkeypatch.setenv("ZFS_ACTIONS", env)
        cli(argv)
        assert mock_serve.call_args.kwargs["actions"] == frozenset(expected)
