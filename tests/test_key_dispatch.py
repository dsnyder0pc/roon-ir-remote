"""
Cover the event loop's key dispatch: a button the config does not bind must
be ignored, not crash the process (the failure seen on 2026-09-16, where an
arrow key on the Argon remote raised RemoteConfigE out of main()).
"""
import pytest

from evdev import InputEvent, ecodes

import roon_remote
from app.config import RemoteConfigE, RemoteKeycodeMapping

THEATER_CODES = {
    "play_pause": "KEY_ENTER",
    "stop": "KEY_ESC",
    "skip": "KEY_RIGHT",
    "prev": "KEY_LEFT",
    "vol_up": "KEY_VOLUMEUP",
    "vol_down": "KEY_VOLUMEDOWN",
    "mute": "KEY_MUTE",
}

# the keys /etc/rc_keymaps/argon.toml can emit but the config above never binds
UNMAPPED = ("KEY_UP", "KEY_DOWN")


def key_event(code, value=1) -> InputEvent:
    """A real InputEvent, so evdev.categorize() works on it as in production."""
    return InputEvent(sec=0, usec=0, type=ecodes.EV_KEY, code=code, value=value)


class FakeDevice:
    """Stands in for an evdev InputDevice with a canned event stream."""
    path = '/dev/input/fake0'

    def __init__(self, key_names, value=1):
        self._events = [(ecodes.ecodes[name], value) for name in key_names]

    def read_loop(self):
        for code, value in self._events:
            yield key_event(code, value)


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


def run(key_names, codes=None, state="playing", value=1):
    zone = FakeZone(state=state)
    mapping = RemoteKeycodeMapping({'codes': dict(codes or THEATER_CODES)})
    roon_remote.monitor_remote(zone, FakeDevice(key_names, value), mapping, 'Test Zone')
    return zone.calls


@pytest.mark.parametrize('key_name', UNMAPPED)
def test_unmapped_key_is_ignored(key_name):
    """The old code raised RemoteConfigE('no fall_asleep key found') here."""
    assert run([key_name]) == []


def test_unmapped_key_does_not_stop_the_loop():
    calls = run(['KEY_UP', 'KEY_RIGHT', 'KEY_DOWN', 'KEY_LEFT'])
    assert calls == ['skip', 'previous']


@pytest.mark.parametrize('key_name,expected', [
    ('KEY_LEFT', ['previous']),
    ('KEY_RIGHT', ['skip']),
    ('KEY_ESC', ['stop']),
    ('KEY_VOLUMEUP', ['volume_up(2)']),
    ('KEY_VOLUMEDOWN', ['volume_down(2)']),
    ('KEY_MUTE', ['mute(True)']),
])
def test_mapped_keys_still_dispatch(key_name, expected):
    assert run([key_name]) == expected


def test_mute_key_code_has_several_evdev_names():
    """KEY_MUTE shares its code with KEY_MIN_INTERESTING; both must resolve."""
    names = roon_remote.key_names_for(ecodes.ecodes['KEY_MUTE'])
    assert len(names) > 1
    assert 'KEY_MUTE' in names


def test_play_pause_pauses_when_playing():
    assert run(['KEY_ENTER'], state="playing") == ['pause']


def test_play_pause_plays_when_stopped():
    assert run(['KEY_ENTER'], state="stopped") == ['repeat(False)', 'play']


def test_office_mapping_binds_arrows_to_volume():
    """The office config uses KEY_UP/KEY_DOWN for volume, the theater one does not."""
    office_codes = dict(THEATER_CODES, vol_up="KEY_UP", vol_down="KEY_DOWN")
    assert run(['KEY_UP', 'KEY_DOWN'], codes=office_codes) == ['volume_up(2)', 'volume_down(2)']


@pytest.mark.parametrize('value', [0, 2])
def test_only_key_down_dispatches(value):
    """value 0 is key-up and 2 is auto-repeat; neither should act."""
    assert run(['KEY_RIGHT'], value=value) == []


def test_to_action_is_the_reverse_of_to_key_code():
    mapping = RemoteKeycodeMapping({'codes': dict(THEATER_CODES)})
    for action, key_code in THEATER_CODES.items():
        assert mapping.to_action(key_code) == action
        assert mapping.to_key_code(action) == key_code
    assert mapping.to_action('KEY_UP') is None


def test_to_key_code_still_raises_for_unknown_action():
    mapping = RemoteKeycodeMapping({'codes': dict(THEATER_CODES)})
    with pytest.raises(RemoteConfigE):
        mapping.to_key_code('fall_asleep')


def test_exceptions_are_catchable_as_exception():
    """All three must be caught by 'except Exception', not escape as BaseException."""
    from app.controller import RoonControllerE
    from app.output import RoonOutputE
    from app.amplifier import AmplifierE
    for cls in (RemoteConfigE, RoonControllerE, RoonOutputE, AmplifierE):
        assert issubclass(cls, Exception), cls.__name__
