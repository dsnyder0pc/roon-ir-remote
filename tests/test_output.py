"""
RoonOutput is the piece that actually issues Roon commands. Everything else in
the test suite fakes it, so a bug in here would go unnoticed by all of it.

The distinction worth protecting: transport commands address the ZONE, while
volume and mute address the OUTPUT. They are different ids on the same object
and swapping them is the obvious refactor mistake.
"""
import pytest

from app.output import RoonOutput, RoonOutputE

OID = 'output-1'
ZID = 'zone-1'
NAME = "David's Office"


class FakeApi:
    """Records what RoonOutput asks the Roon API to do."""

    def __init__(self, known=True, zone_state='playing', is_muted=False, volume=True):
        self.outputs = {}
        self.zones = {}
        self.calls = []
        self.callbacks = []
        if known:
            output = {'output_id': OID, 'zone_id': ZID, 'display_name': NAME}
            if volume:
                output['volume'] = {'is_muted': is_muted}
            self.outputs = {OID: output}
            self.zones = {ZID: {'zone_id': ZID, 'state': zone_state}}

    def output_by_name(self, name):
        for output in self.outputs.values():
            if output['display_name'] == name:
                return output
        return None

    def playback_control(self, zone_or_output_id, control="play"):
        self.calls.append(('playback_control', zone_or_output_id, control))

    def seek(self, zone_or_output_id, seconds, method="absolute"):
        self.calls.append(('seek', zone_or_output_id, seconds))

    def repeat(self, zone_or_output_id, repeat=True):
        self.calls.append(('repeat', zone_or_output_id, repeat))

    def mute(self, output_id, mute=True):
        self.calls.append(('mute', output_id, mute))

    def change_volume(self, output_id, value, method="absolute"):
        self.calls.append(('change_volume', output_id, value, method))

    def register_state_callback(self, callback, event_filter=None, id_filter=None):
        self.callbacks.append((callback, event_filter, id_filter))


@pytest.fixture(name='api')
def api_fixture():
    return FakeApi()


@pytest.fixture(name='output')
def output_fixture(api):
    return RoonOutput(api, NAME)


# --- finding the output ---------------------------------------------------

def test_unknown_output_raises(api):
    """What main() catches to wait for a DAC that is switched off."""
    with pytest.raises(RoonOutputE) as caught:
        RoonOutput(api, 'No Such Zone')
    assert 'No Such Zone' in caught.value.msg


def test_known_output_resolves_to_its_id(api):
    assert RoonOutput(api, NAME)._oid == OID


def test_no_callback_is_registered_by_default(api):
    """get_output() passes register_callback=False; nothing should subscribe."""
    RoonOutput(api, NAME)
    assert api.callbacks == []


def test_callback_registration_uses_the_event_filter(api):
    output = RoonOutput(api, NAME, register_callback=True)
    assert len(api.callbacks) == 1
    callback, event_filter, _ = api.callbacks[0]
    assert callback == output._callback
    assert event_filter == RoonOutput.EVENT_FILTER


# --- reading state --------------------------------------------------------

def test_zone_id_comes_from_the_output(output):
    assert output.zone_id == ZID


def test_zone_id_is_none_once_the_output_disappears(api, output):
    api.outputs.clear()                 # the DAC was switched off
    assert output.zone_id is None


def test_state_comes_from_the_zone(api):
    assert RoonOutput(FakeApi(zone_state='paused'), NAME).state == 'paused'


def test_state_is_none_once_the_zone_disappears(api, output):
    api.zones.clear()
    assert output.state is None


@pytest.mark.parametrize('muted', [True, False])
def test_is_muted_reads_the_output_volume(muted):
    assert RoonOutput(FakeApi(is_muted=muted), NAME).is_muted() is muted


def test_is_muted_is_false_when_the_output_has_no_volume():
    """A fixed-volume output has no volume block at all."""
    assert RoonOutput(FakeApi(volume=False), NAME).is_muted() is False


def test_is_muted_is_false_once_the_output_disappears(api, output):
    api.outputs.clear()
    assert output.is_muted() is False


# --- transport addresses the zone ----------------------------------------

@pytest.mark.parametrize('method,control', [
    ('play', 'play'),
    ('pause', 'pause'),
    ('skip', 'next'),
    ('previous', 'previous'),
])
def test_transport_commands_address_the_zone(api, output, method, control):
    getattr(output, method)()
    assert api.calls == [('playback_control', ZID, control)]


def test_stop_also_rewinds(api, output):
    """Stop is stop plus seek(0); losing the seek would be a silent change."""
    output.stop()
    assert api.calls == [('playback_control', ZID, 'stop'), ('seek', ZID, 0)]


@pytest.mark.parametrize('enabled', [True, False])
def test_repeat_addresses_the_zone(api, output, enabled):
    output.repeat(enabled)
    assert api.calls == [('repeat', ZID, enabled)]


# --- volume and mute address the output ----------------------------------

def test_volume_up_unmutes_first_then_steps_up(api, output):
    """Stepping volume on a muted output would otherwise do nothing audible."""
    output.volume_up(2)
    assert api.calls == [
        ('mute', OID, False),
        ('change_volume', OID, 2, 'relative_step'),
    ]


def test_volume_down_steps_negative(api, output):
    output.volume_down(2)
    assert api.calls == [
        ('mute', OID, False),
        ('change_volume', OID, -2, 'relative_step'),
    ]


@pytest.mark.parametrize('method,expected', [('volume_up', 5), ('volume_down', -5)])
def test_volume_step_defaults_to_five(api, output, method, expected):
    getattr(output, method)()
    assert api.calls[-1] == ('change_volume', OID, expected, 'relative_step')


@pytest.mark.parametrize('enabled', [True, False])
def test_mute_addresses_the_output_not_the_zone(api, output, enabled):
    output.mute(enabled)
    assert api.calls == [('mute', OID, enabled)]


def test_mute_defaults_to_unmuting(api, output):
    output.mute()
    assert api.calls == [('mute', OID, False)]


# --- current behaviour worth noticing ------------------------------------

def test_transport_on_a_vanished_output_sends_a_none_zone(api, output):
    """
    Characterisation, not endorsement: zone_id returns None and the command
    goes out anyway. Flagged rather than changed, so a refactor that tightens
    it fails here loudly instead of quietly.
    """
    api.outputs.clear()
    output.pause()
    assert api.calls == [('playback_control', None, 'pause')]
