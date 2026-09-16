"""Class handling the logic and commands provided by an Roon output!"""
import logging
from typing import Dict

from .controller import RoonApi

logger = logging.getLogger('output')


class RoonOutputE(Exception):
    """Basic Class for Output """
    def __init__(self, msg):
        super().__init__(msg)
        self.msg = msg


class RoonOutput:
    """Implment logic for Roon Outputs"""

    def __init__(self, api: RoonApi, output_name: str):
        super(RoonOutput).__init__()
        self._api = api
        self._name = output_name
        self._oid = None
        oid = self._get_output_id(output_name)
        if not oid:
            raise RoonOutputE('could not find any output with name "{}"'.format(output_name))

        self._oid = oid
        logger.debug('instantiated RoonOutput {}'.format(output_name))

    @property
    def zone_id(self):
        if self._oid and self._oid in self._api.outputs.keys():
            return self._api.outputs[self._oid]['zone_id']
        else:
            logger.error('failed to retrieve the ZID for given OID "{}"'.format(self._oid))
            return None

    @property
    def state(self):
        z = self.zone_id
        if z and z in self._api.zones.keys():
            return self._api.zones[z]['state']
        else:
            logger.error('failed to retrieve the state for OID {}'.format(self._oid))
            return None

    def _get_output_id(self, name: str):
        """Try to find the OID based on names."""
        logger.debug('finding output "{}"'.format(name))
        o = self._api.output_by_name(name)
        if not o:
            # Changed from .error to .debug to quiet the logs while waiting for a zone
            logger.debug('could not find output "%s" (this is normal if the device is off)', name)
            return None
        oid = o['output_id']
        zid = o['zone_id']
        logger.debug('found output {} with ID {} in ZONE {}'.format(name, oid, zid))
        return oid

    def _get_output(self) -> Dict:
        """Simplify the access to the output dictionary for current OID"""
        if self._oid in self._api.outputs.keys():
            return self._api.outputs[self._oid]
        else:
            return {}

    def pause(self):
        """Next Track"""
        self._api.playback_control(self.zone_id, "pause")

    def stop(self):
        """Stop Player and Clear Playlist"""
        self._api.playback_control(self.zone_id, "stop")
        self._api.seek(self.zone_id, 0)

    def repeat(self, repeat: bool):
        logger.debug(f"{'enable' if repeat else 'disable'} for zone {self.zone_id}")
        self._api.repeat(self.zone_id, repeat)

    def play(self):
        """Start Play with current playlist"""
        self._api.playback_control(self.zone_id, "play")

    def skip(self):
        """Skip current title"""
        self._api.playback_control(self.zone_id, "next")

    def previous(self):
        self._api.playback_control(self.zone_id, "previous")

    def volume_up(self, value: int = 5):
        self._api.mute(self._oid, False)
        self._api.change_volume(self._oid, value, method="relative_step")

    def volume_down(self, value: int = 5):
        self._api.mute(self._oid, False)
        self._api.change_volume(self._oid, -1 * value, method="relative_step")

    def mute(self, enabled=False):
        self._api.mute(self._oid, enabled)

    def is_muted(self) -> bool:
        """Return True if output is muted, otherwise False."""
        o = self._get_output()
        if 'volume' in o.keys():
            return o['volume']['is_muted']
        else:
            return False
