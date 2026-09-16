"""
Cover the liveness machinery added after the 2026-09-14 outage, where the
Roon websocket died, the process kept running, and nothing noticed for two
days: the idle tick, the session check, and the watchdog.
"""
import os
import time

import pytest

from evdev import InputEvent, ecodes

import roon_remote
from app.controller import RoonController, RoonControllerE


# --- read_key_events ------------------------------------------------------

class PipeDevice:
    """An InputDevice whose fd is a real pipe, so select() behaves for real."""
    path = '/dev/input/pipe0'

    def __init__(self):
        self.fd, self._write_fd = os.pipe()
        self._queued = []

    def press(self, key_name):
        self._queued.append(InputEvent(0, 0, ecodes.EV_KEY, ecodes.ecodes[key_name], 1))
        os.write(self._write_fd, b'x')

    def read(self):
        os.read(self.fd, 1)
        queued, self._queued = self._queued, []
        return iter(queued)

    def close(self):
        os.close(self.fd)
        os.close(self._write_fd)


@pytest.fixture(name='pipe_device')
def pipe_device_fixture():
    dev = PipeDevice()
    yield dev
    dev.close()


def test_idle_reader_yields_none_on_timeout(pipe_device):
    """The whole point: the loop wakes up even when no button is pressed."""
    events = roon_remote.read_key_events(pipe_device, timeout=0.05)
    started = time.monotonic()
    assert next(events) is None
    assert next(events) is None
    assert time.monotonic() - started >= 0.1


def test_reader_yields_queued_key_events(pipe_device):
    pipe_device.press('KEY_RIGHT')
    events = roon_remote.read_key_events(pipe_device, timeout=5)
    event = next(events)
    assert event is not None
    assert event.code == ecodes.ecodes['KEY_RIGHT']


# --- the idle tick drives the session check -------------------------------

class FakeController:
    def __init__(self, alive=True):
        self.alive = alive
        self.checks = 0

    def check_alive(self, probe_interval=None):
        self.checks += 1
        return self.alive


def test_idle_tick_checks_the_session(dispatch):
    controller = FakeController(alive=True)
    assert dispatch([None, 'KEY_RIGHT', None], controller=controller) == ['skip']
    assert controller.checks == 2


def test_dead_session_breaks_out_of_the_loop(dispatch):
    """A dead session must raise, so main() rebuilds the controller."""
    controller = FakeController(alive=False)
    with pytest.raises(RoonControllerE):
        dispatch([None], controller=controller)


def test_a_key_press_also_feeds_the_watchdog(dispatch):
    watchdog = roon_remote.Watchdog(timeout=60, on_expire=lambda: None)
    watchdog.tick()
    assert dispatch(['KEY_RIGHT'], watchdog=watchdog) == ['skip']
    assert watchdog.silent_for() < 1


def test_idle_tick_feeds_the_watchdog(dispatch):
    """Overnight there are no key presses, so the idle tick is the only feeder."""
    watchdog = roon_remote.Watchdog(timeout=60, on_expire=lambda: None)
    watchdog.tick()
    watchdog._last_tick -= 30
    assert watchdog.silent_for() >= 30
    dispatch([None], watchdog=watchdog)
    assert watchdog.silent_for() < 1


# --- the watchdog ---------------------------------------------------------

def test_watchdog_does_not_fire_while_ticking():
    fired = []
    watchdog = roon_remote.Watchdog(timeout=0.4, on_expire=lambda: fired.append(True))
    watchdog.start()
    for _ in range(8):
        watchdog.tick()
        time.sleep(0.05)
    assert fired == []


def test_watchdog_fires_when_the_loop_goes_silent():
    fired = []
    watchdog = roon_remote.Watchdog(timeout=0.2, on_expire=lambda: fired.append(True))
    watchdog.start()
    deadline = time.monotonic() + 5
    while not fired and time.monotonic() < deadline:
        time.sleep(0.05)
    assert fired, "watchdog should have fired after the loop went silent"


def test_watchdog_expiry_is_based_on_the_last_tick():
    watchdog = roon_remote.Watchdog(timeout=10, on_expire=lambda: None)
    assert not watchdog.expired()
    watchdog._last_tick = time.monotonic() - 11
    assert watchdog.expired()
    watchdog.tick()
    assert not watchdog.expired()


# --- RoonController liveness ---------------------------------------------

class FakeSocket:
    def __init__(self, connected=True, failed_state=False):
        self.connected = connected
        self.failed_state = failed_state


class FakeApi:
    stopped = False

    def stop(self):
        self.stopped = True

    def __init__(self, socket=None, ready=True, zones=None, raises=None):
        self._roonsocket = socket
        self.ready = ready
        self._zones = zones if zones is not None else {'z1': {}}
        self._raises = raises

    def _get_zones(self):
        if self._raises:
            raise self._raises
        return self._zones


def controller_with(api):
    controller = RoonController.__new__(RoonController)
    controller._api = api
    controller._last_probe = time.monotonic()
    controller._on_progress = lambda: None
    return controller


@pytest.mark.parametrize('socket,ready,expected', [
    (FakeSocket(), True, True),
    (FakeSocket(connected=False), True, False),
    (FakeSocket(failed_state=True), True, False),
    (FakeSocket(), False, False),
    (None, True, False),
])
def test_is_connected_reads_the_socket_state(socket, ready, expected):
    assert controller_with(FakeApi(socket, ready=ready)).is_connected() is expected


def test_probe_is_true_when_the_core_answers():
    assert controller_with(FakeApi(FakeSocket())).probe() is True


def test_probe_is_false_when_the_core_returns_nothing():
    """A half-open socket answers with nothing, which is the case we missed."""
    assert controller_with(FakeApi(FakeSocket(), zones={})).probe() is False


def test_probe_is_false_when_the_request_raises():
    assert controller_with(FakeApi(FakeSocket(), raises=OSError('boom'))).probe() is False


def test_check_alive_skips_the_probe_until_the_interval_is_up():
    api = FakeApi(FakeSocket(), zones={})  # a probe here would say "dead"
    controller = controller_with(api)
    assert controller.check_alive(probe_interval=300) is True   # too soon to probe
    controller._last_probe = time.monotonic() - 301
    assert controller.check_alive(probe_interval=300) is False  # probe runs, and fails


def test_check_alive_is_false_as_soon_as_the_socket_drops():
    controller = controller_with(FakeApi(FakeSocket(connected=False)))
    assert controller.check_alive() is False


def test_exception_messages_survive_str():
    """main() logs these; an empty str(e) is how the first diagnosis went blind."""
    from app.config import RemoteConfigE
    from app.output import RoonOutputE
    for cls in (RemoteConfigE, RoonControllerE, RoonOutputE):
        assert str(cls('boom')) == 'boom', cls.__name__


# --- the real roonapi objects, no Roon core involved ----------------------

def test_is_connected_against_a_real_dead_roonapi_socket():
    """
    is_connected() reads private roonapi state, so pin the contract against the
    real class: a socket that cannot connect must report connected False and
    failed_state True, and the controller must call that dead.
    """
    from roonapi.roonapisocket import RoonApiWebSocket

    socket = RoonApiWebSocket("ws://127.0.0.1:1/api")  # nothing listens on port 1
    socket.start()
    deadline = time.monotonic() + 10
    while socket.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    socket.join(timeout=5)

    assert socket.connected is False
    assert socket.failed_state is True

    controller = controller_with(FakeApi(socket, ready=True))
    assert controller.is_connected() is False
    assert controller.check_alive() is False


def test_watchdog_default_action_really_ends_the_process():
    """
    The other watchdog tests inject on_expire. This one runs the default,
    os._exit(1), in a real subprocess: the exit code is what tells systemd's
    Restart=on-failure to give us a fresh process.
    """
    import pathlib
    import subprocess
    import sys

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    script = (
        "import time, roon_remote;"
        "w = roon_remote.Watchdog(timeout=1);"
        "w.start();"
        "time.sleep(30)"  # never ticks, so the watchdog should end us first
    )
    result = subprocess.run([sys.executable, '-c', script], cwd=repo_root,
                            capture_output=True, text=True, timeout=25, check=False)
    assert result.returncode == 1, result.stderr
    assert 'event loop silent' in result.stderr
