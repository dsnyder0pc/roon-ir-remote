"""
RoonApi's blocking init waits for the core forever, and its socket watcher only
starts once the constructor returns, so an unreachable core parks the process
somewhere nothing can recover it. Cover the bounded wait that replaces it.
"""
import time

import pytest

from app.controller import RoonController, RoonControllerE


class FakeSocket:
    def __init__(self, connected=False, failed_state=False):
        self.connected = connected
        self.failed_state = failed_state


class FakeApi:
    """A RoonApi that never gets anywhere unless the test says so."""

    def __init__(self, socket=None, ready=False, outputs=None):
        self._roonsocket = socket if socket is not None else FakeSocket()
        self.ready = ready
        self._outputs = outputs or {}
        self.stopped = False

    @property
    def outputs(self):
        return self._outputs

    def stop(self):
        self.stopped = True


def controller_with(api, connect=0.3, authorize=0.3, subscribe=0.1):
    controller = RoonController.__new__(RoonController)
    controller._api = api
    controller._last_probe = time.monotonic()
    controller._on_progress = lambda: None
    controller.CONNECT_TIMEOUT = connect
    controller.AUTHORIZE_TIMEOUT = authorize
    controller.SUBSCRIBE_TIMEOUT = subscribe
    return controller


def test_gives_up_when_the_socket_never_connects():
    """The hang: a core that accepts nothing. Must raise, not block."""
    api = FakeApi(FakeSocket(connected=False))
    controller = controller_with(api)

    started = time.monotonic()
    with pytest.raises(RoonControllerE) as caught:
        controller._wait_until_ready('192.0.2.1', 9330)

    assert time.monotonic() - started < 5
    assert 'no websocket to 192.0.2.1:9330' in str(caught.value)
    assert api.stopped, "the half-built api must be stopped, not leaked"


def test_gives_up_at_once_when_the_socket_has_already_failed():
    """A refused or reset connection is known bad; do not sit out the timeout."""
    api = FakeApi(FakeSocket(failed_state=True))
    controller = controller_with(api, connect=30)

    started = time.monotonic()
    with pytest.raises(RoonControllerE) as caught:
        controller._wait_until_ready('192.0.2.1', 9330)

    assert time.monotonic() - started < 1, "should not wait out CONNECT_TIMEOUT"
    assert 'failed' in str(caught.value)
    assert api.stopped


def test_gives_up_when_the_extension_is_never_authorized():
    """Connected but unregistered: someone has to enable it in Roon."""
    api = FakeApi(FakeSocket(connected=True), ready=False)
    controller = controller_with(api)

    with pytest.raises(RoonControllerE) as caught:
        controller._wait_until_ready('192.0.2.1', 9330)

    assert 'did not register us' in str(caught.value)
    assert api.stopped


def test_returns_once_connected_and_registered():
    api = FakeApi(FakeSocket(connected=True), ready=True, outputs={'o1': {}})
    controller = controller_with(api)

    controller._wait_until_ready('192.0.2.1', 9330)  # must not raise

    assert api.stopped is False


def test_late_registration_is_still_accepted():
    """ready arrives while we are waiting, as it does on a real core."""
    import threading

    api = FakeApi(FakeSocket(connected=True), ready=False, outputs={'o1': {}})
    controller = controller_with(api, authorize=5)

    def register_soon():
        time.sleep(0.3)
        api.ready = True

    threading.Thread(target=register_soon, daemon=True).start()
    controller._wait_until_ready('192.0.2.1', 9330)

    assert api.stopped is False


def test_missing_outputs_is_a_warning_not_a_failure():
    """The subscription may lag; get_output()'s own retry covers that."""
    api = FakeApi(FakeSocket(connected=True), ready=True, outputs={})
    controller = controller_with(api)

    controller._wait_until_ready('192.0.2.1', 9330)  # must not raise

    assert api.stopped is False


@pytest.mark.parametrize('socket,ready,expected', [
    (FakeSocket(connected=True), True, True),
    (FakeSocket(connected=True), False, False),
    (FakeSocket(connected=False), True, False),
    (FakeSocket(failed_state=True), True, False),
])
def test_is_connected_still_needs_both_socket_and_registration(socket, ready, expected):
    assert controller_with(FakeApi(socket, ready=ready)).is_connected() is expected


def test_waiting_feeds_the_progress_hook():
    """
    Authorization can take minutes; without this the watchdog would kill the
    process while it waits for someone to enable the extension in Roon.
    """
    ticks = []
    api = FakeApi(FakeSocket(connected=True), ready=False)
    controller = controller_with(api, authorize=0.3)
    controller._on_progress = lambda: ticks.append(1)

    with pytest.raises(RoonControllerE):
        controller._wait_until_ready('192.0.2.1', 9330)

    assert len(ticks) > 1, "the wait must report progress, not go silent"


def test_waiting_for_the_socket_feeds_the_progress_hook():
    ticks = []
    api = FakeApi(FakeSocket(connected=False))
    controller = controller_with(api, connect=0.3)
    controller._on_progress = lambda: ticks.append(1)

    with pytest.raises(RoonControllerE):
        controller._wait_until_ready('192.0.2.1', 9330)

    assert len(ticks) > 1


def test_the_socket_wait_shares_the_api_deadline():
    """
    SocketAwareRoonApi already waited for the same thing during construction.
    Waiting a second full timeout here would double how long an unreachable
    core takes to report.
    """
    import time as _time
    api = FakeApi(FakeSocket(connected=False))
    api._ready_deadline = _time.monotonic() - 1      # the api spent the budget
    controller = controller_with(api, connect=30)

    started = _time.monotonic()
    with pytest.raises(RoonControllerE):
        controller._wait_until_ready('192.0.2.1', 9330)

    assert _time.monotonic() - started < 1, "should not start a second 30s wait"


def test_an_open_socket_is_not_failed_by_a_spent_deadline():
    """
    The api spends its deadline on construction; a socket that is open by
    then must go on to the authorization wait, not be reported missing.
    """
    import time as _time
    api = FakeApi(FakeSocket(connected=True), ready=False)
    api._ready_deadline = _time.monotonic() - 1
    controller = controller_with(api, authorize=0.3)

    with pytest.raises(RoonControllerE) as caught:
        controller._wait_until_ready('192.0.2.1', 9330)

    assert 'did not register us' in str(caught.value)
