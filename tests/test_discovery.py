"""
Every roonapi install sends the same discovery query, transaction id and all,
and a core ignores a transaction id it has just answered. Cover the query we
build instead, and that each one is new.
"""
import os
import socket

import roonapi.discovery
import pytest

from app import controller
from app.controller import discover_servers, sood_query

LIBRARY_TID = "c64e3888-f2f2-4c4a-9f89-2093ae4217a6"


def response(port="9330"):
    """A core's answer, cut down to what discovery reads."""
    def prop(key, value):
        k, v = key.encode(), value.encode()
        return bytes([len(k)]) + k + len(v).to_bytes(2, 'big') + v
    return b"SOOD\x02R" + prop("name", "Core") + prop("http_port", port) + prop("_tid", "x")


class FakeSocket:
    """Records what was sent, then answers from a script."""
    sent = []

    def __init__(self, replies):
        self._replies = list(replies)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def setsockopt(self, *args):
        pass

    def settimeout(self, timeout):
        pass

    def sendto(self, data, address):
        FakeSocket.sent.append(data)

    def recvfrom(self, size):
        if not self._replies:
            raise socket.timeout()
        return self._replies.pop(0)


@pytest.fixture
def replies(monkeypatch):
    FakeSocket.sent = []
    script = []
    monkeypatch.setattr(controller.socket, 'socket', lambda *args: FakeSocket(script))
    return script


def test_the_query_is_the_librarys_apart_from_the_tid():
    """Built from the spec, so prove it matches what cores already answer."""
    path = os.path.join(os.path.dirname(roonapi.discovery.__file__), ".soodmsg")
    with open(path, "rb") as f:
        assert sood_query(LIBRARY_TID) == f.read()


def test_every_discovery_sends_a_new_tid(replies):
    discover_servers()
    discover_servers()
    first, _, second, _ = FakeSocket.sent
    assert first != second
    assert LIBRARY_TID.encode() not in first + second


def test_answers_are_collected_until_the_timeout(replies):
    replies += [(response("9330"), ("192.0.2.1", 9003)), (response("9331"), ("192.0.2.2", 9003))]
    assert discover_servers() == [("192.0.2.1", "9330"), ("192.0.2.2", "9331")]
