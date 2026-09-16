"""
When we give up on a connect that is still in flight, websocket-client has
already built the WebSocket but has not finished connect(), so pyroon's stop()
nulls its own reference without being able to close anything. If the connect
lands afterwards the connection stays ESTABLISHED forever. Cover the reaper.
"""
import pytest

from app import controller as controller_mod
from app.controller import close_abandoned_connects, remember_abandoned_connect


class FakeWebSocket:
    """websocket-client's WebSocket: exists before connect() returns."""

    def __init__(self, connected=False, raw=None):
        self.connected = connected
        self.sock = raw
        self.closed = False

    def close(self):
        self.closed = True
        self.connected = False
        self.sock = None


class FakeApp:
    """websocket-client's WebSocketApp, held as RoonApiWebSocket._socket."""

    def __init__(self, websocket):
        self.sock = websocket


class FakeSocketThread:
    """pyroon's RoonApiWebSocket, which is a Thread."""

    def __init__(self, websocket, alive=True):
        self._socket = FakeApp(websocket)
        self._alive = alive

    def is_alive(self):
        return self._alive


@pytest.fixture(autouse=True)
def empty_registry():
    controller_mod._abandoned_connects.clear()
    yield
    controller_mod._abandoned_connects.clear()


def test_nothing_abandoned_is_a_no_op():
    assert close_abandoned_connects() == 0


def test_a_connect_that_lands_later_gets_closed():
    """The leak seen on office: two ESTABLISHED sockets instead of one."""
    pending = FakeWebSocket(connected=False)          # still inside connect()
    thread = FakeSocketThread(pending)
    remember_abandoned_connect(thread)

    assert close_abandoned_connects() == 0            # nothing to close yet
    assert pending.closed is False

    pending.connected = True                          # the connect lands
    assert close_abandoned_connects() == 1
    assert pending.closed is True
    assert controller_mod._abandoned_connects == []


def test_a_raw_socket_without_the_connected_flag_also_counts():
    pending = FakeWebSocket(connected=False, raw=object())
    remember_abandoned_connect(FakeSocketThread(pending))

    assert close_abandoned_connects() == 1
    assert pending.closed is True


def test_a_connect_that_never_lands_is_forgotten():
    """Otherwise the registry grows once per retry for the whole outage."""
    pending = FakeWebSocket(connected=False)
    thread = FakeSocketThread(pending, alive=True)
    remember_abandoned_connect(thread)

    assert close_abandoned_connects() == 0
    assert len(controller_mod._abandoned_connects) == 1   # still connecting

    thread._alive = False                                  # gave up
    assert close_abandoned_connects() == 0
    assert controller_mod._abandoned_connects == []
    assert pending.closed is False


def test_a_dead_thread_that_did_connect_is_still_closed():
    """The real case: the thread ends without releasing the socket."""
    pending = FakeWebSocket(connected=True)
    remember_abandoned_connect(FakeSocketThread(pending, alive=False))

    assert close_abandoned_connects() == 1
    assert pending.closed is True


def test_nothing_is_remembered_when_no_websocket_exists_yet():
    thread = FakeSocketThread(None)
    remember_abandoned_connect(thread)
    assert controller_mod._abandoned_connects == []


def test_a_close_that_raises_does_not_stop_the_sweep():
    class Stubborn(FakeWebSocket):
        def close(self):
            raise OSError('already gone')

    stubborn = Stubborn(connected=True)
    good = FakeWebSocket(connected=True)
    remember_abandoned_connect(FakeSocketThread(stubborn))
    remember_abandoned_connect(FakeSocketThread(good))

    assert close_abandoned_connects() == 1
    assert good.closed is True
    assert controller_mod._abandoned_connects == []
