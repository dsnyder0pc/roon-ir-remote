"""Shared fakes for the event-loop tests."""
import contextlib

import pytest

from evdev import InputEvent, ecodes

import roon_remote
from app.config import RemoteKeycodeMapping

# the theater mapping: KEY_UP and KEY_DOWN are deliberately absent, which is
# what /etc/rc_keymaps/argon.toml sends and what used to crash the service
THEATER_CODES = {
    "play_pause": "KEY_ENTER",
    "stop": "KEY_ESC",
    "skip": "KEY_RIGHT",
    "prev": "KEY_LEFT",
    "vol_up": "KEY_VOLUMEUP",
    "vol_down": "KEY_VOLUMEDOWN",
    "mute": "KEY_MUTE",
}

# the office mapping binds the arrows instead
OFFICE_CODES = dict(THEATER_CODES, vol_up="KEY_UP", vol_down="KEY_DOWN")


def key_event(code, value=1) -> InputEvent:
    """A real InputEvent, so evdev.categorize() works on it as in production."""
    return InputEvent(sec=0, usec=0, type=ecodes.EV_KEY, code=code, value=value)


class FakeDevice:
    """Stands in for an evdev InputDevice."""
    path = '/dev/input/fake0'
    fd = -1


class FakeZone:
    """Records the transport calls the loop makes."""

    def __init__(self, state="playing"):
        self.state = state
        self.calls = []

    def _record(self, name):
        self.calls.append(name)

    def previous(self): self._record('previous')
    def skip(self): self._record('skip')
    def stop(self): self._record('stop')
    def pause(self): self._record('pause')
    def play(self): self._record('play')
    def repeat(self, enabled): self._record('repeat(%s)' % enabled)
    def volume_up(self, step): self._record('volume_up(%d)' % step)
    def volume_down(self, step): self._record('volume_down(%d)' % step)
    def mute(self, enabled): self._record('mute(%s)' % enabled)
    def is_muted(self): return False
    def play_playlist(self, name): self._record('play_playlist(%s)' % name)
    def play_radio_station(self, station_name): self._record('play_radio(%s)' % station_name)


@contextlib.contextmanager
def canned_events(key_names, value=1):
    """
    Feed monitor_remote a fixed event stream. A key name of None stands for an
    idle tick, which is what read_key_events yields when nothing is pressed.
    """
    events = [None if name is None else key_event(ecodes.ecodes[name], value)
              for name in key_names]
    original = roon_remote.read_key_events
    roon_remote.read_key_events = lambda dev, timeout=None: iter(events)
    try:
        yield
    finally:
        roon_remote.read_key_events = original


@pytest.fixture(name='dispatch')
def dispatch_fixture():
    """Run the real event loop over a canned event stream, return the calls made."""

    def run(key_names, codes=None, state="playing", value=1, controller=None, watchdog=None):
        zone = FakeZone(state=state)
        mapping = RemoteKeycodeMapping({'codes': dict(codes or THEATER_CODES)})
        with canned_events(key_names, value):
            roon_remote.monitor_remote(zone, FakeDevice(), mapping, 'Test Zone',
                                       controller=controller, watchdog=watchdog)
        return zone.calls

    return run
