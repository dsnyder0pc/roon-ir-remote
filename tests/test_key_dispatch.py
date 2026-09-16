"""
Cover the event loop's key dispatch: a button the config does not bind must
be ignored, not crash the process (the failure seen on 2026-09-16, where an
arrow key on the Argon remote raised RemoteConfigE out of main()).
"""
import pytest

from evdev import ecodes

import roon_remote
from app.config import RemoteConfigE, RemoteKeycodeMapping

from conftest import OFFICE_CODES, THEATER_CODES

# the keys /etc/rc_keymaps/argon.toml can emit but the theater config never binds
UNMAPPED = ("KEY_UP", "KEY_DOWN")


@pytest.mark.parametrize('key_name', UNMAPPED)
def test_unmapped_key_is_ignored(dispatch, key_name):
    """The old code raised RemoteConfigE('no fall_asleep key found') here."""
    assert dispatch([key_name]) == []


def test_unmapped_key_does_not_stop_the_loop(dispatch):
    assert dispatch(['KEY_UP', 'KEY_RIGHT', 'KEY_DOWN', 'KEY_LEFT']) == ['skip', 'previous']


@pytest.mark.parametrize('key_name,expected', [
    ('KEY_LEFT', ['previous']),
    ('KEY_RIGHT', ['skip']),
    ('KEY_ESC', ['stop']),
    ('KEY_VOLUMEUP', ['volume_up(2)']),
    ('KEY_VOLUMEDOWN', ['volume_down(2)']),
    ('KEY_MUTE', ['mute(True)']),
])
def test_mapped_keys_still_dispatch(dispatch, key_name, expected):
    assert dispatch([key_name]) == expected


def test_mute_key_code_has_several_evdev_names():
    """KEY_MUTE shares its code with KEY_MIN_INTERESTING; both must resolve."""
    names = roon_remote.key_names_for(ecodes.ecodes['KEY_MUTE'])
    assert len(names) > 1
    assert 'KEY_MUTE' in names


def test_play_pause_pauses_when_playing(dispatch):
    assert dispatch(['KEY_ENTER'], state="playing") == ['pause']


def test_play_pause_plays_when_stopped(dispatch):
    assert dispatch(['KEY_ENTER'], state="stopped") == ['repeat(False)', 'play']


def test_office_mapping_binds_arrows_to_volume(dispatch):
    """The office config uses KEY_UP/KEY_DOWN for volume, the theater one does not."""
    assert dispatch(['KEY_UP', 'KEY_DOWN'], codes=OFFICE_CODES) == ['volume_up(2)', 'volume_down(2)']


@pytest.mark.parametrize('value', [0, 2])
def test_only_key_down_dispatches(dispatch, value):
    """value 0 is key-up and 2 is auto-repeat; neither should act."""
    assert dispatch(['KEY_RIGHT'], value=value) == []


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
    """All must be caught by 'except Exception', not escape as BaseException."""
    from app.controller import RoonControllerE
    from app.output import RoonOutputE
    from app.amplifier import AmplifierE
    for cls in (RemoteConfigE, RoonControllerE, RoonOutputE, AmplifierE):
        assert issubclass(cls, Exception), cls.__name__
