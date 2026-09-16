import json
import time
from pathlib import Path
from typing import Dict, Tuple, Type
from typing import List
from typing import Tuple
import logging

from roonapi import RoonApi, RoonDiscovery

from .token import RoonToken
from .output import RoonOutput

logger = logging.getLogger('roon-controller')
logger.setLevel(logging.DEBUG)


class RoonControllerE(Exception):
    def __init__(self, msg):
        super(RoonControllerE, self).__init__(msg)
        self.msg = msg


class RoonController(object):
    """
    Roon Controller, initiate buttons, display and RoonAPI
    """

    # a round trip to the core is the only way to catch a socket that died
    # without saying so, but it is not free, so it runs on its own cadence
    PROBE_INTERVAL = 300

    def __init__(self, app_info: Dict, token: Path = '.roon-token'):
        super(RoonController, self).__init__()
        self._info = app_info
        self._token_path = token
        self._token = None
        self._zone = None
        self._last_probe = time.monotonic()

        server = self._discover_server()
        if not server or not server[0]:
            logger.error("failed to discover a server")
            raise RoonControllerE("failed to discover a Roon server")

        logger.debug("Received: %s" % server[0])

        if token:
            self._token = RoonToken(token)

        if self._token.is_empty():
            self._api = RoonApi(self._info, token=None, host=server[0], port=server[1])
        else:
            self._api = RoonApi(self._info, token=self._token.to_string(), host=server[0], port=server[1])

        if self._api:
            self._token.set(self._api.token)
            logger.debug("Connected to API: %s, %s, %s" % (self._api.host, self._api.core_name, self._api.core_id))

        logger.debug('instantiated a Roon controller on: %s' % self._api.core_name)

    @staticmethod
    def _discover_server() -> Tuple:
        """
        Run the discovery that allows us to detect the server,
        return the very first server discovered.
        """
        discover = RoonDiscovery(None)
        try:
            servers = discover.all()
        except OSError as ex:
            # the network may not be up yet, e.g. when started at boot
            raise RoonControllerE("discovery failed: %s" % ex) from ex
        finally:
            discover.stop()
        logger.debug("Discovery found: %s" % repr(servers))
        if not servers:
            logger.debug('failed to discover Roon server')
            return None, 0

        return servers[0]

    def zones(self) -> List:
        return self._api.zones.keys()

    def get_output(self, name: str) -> RoonOutput:
        return RoonOutput(self._api, name, register_callback=False)

    @staticmethod
    def _read_as_json(path) -> Dict:
        _data = {}
        with open(path) as f:
            _data = json.load(f)
        return _data

    def is_connected(self) -> bool:
        """Passive check of the websocket state. Cheap, safe to call often."""
        # pylint: disable=protected-access
        socket = getattr(self._api, '_roonsocket', None)
        if socket is None:
            return False
        if getattr(socket, 'failed_state', False):
            return False
        return bool(getattr(socket, 'connected', False)) and bool(getattr(self._api, 'ready', False))

    def probe(self) -> bool:
        """
        Ask the core for its zones. Returns False when the answer does not
        come back, which is what a half-open socket looks like from here.
        """
        # pylint: disable=protected-access
        try:
            zones = self._api._get_zones()
        except Exception as ex:
            logger.warning('probe failed: %s (%s)', ex, type(ex).__name__)
            return False
        if not zones:
            logger.warning('probe returned no zones, the session looks dead')
            return False
        logger.debug('probe returned %d zone(s)', len(zones))
        return True

    def check_alive(self, probe_interval: int = None) -> bool:
        """
        True while the session to the core is usable. Passive on every call,
        with a real round trip at most every probe_interval seconds.
        """
        if probe_interval is None:
            probe_interval = self.PROBE_INTERVAL

        if not self.is_connected():
            logger.warning('websocket to the core is no longer connected')
            return False

        now = time.monotonic()
        if now - self._last_probe < probe_interval:
            return True

        self._last_probe = now
        return self.probe()

    def shutdown(self):
        """
        Shutdown the infinite loop, save the token
        """
        self._api.stop()

    def __del__(self):
        # __init__ may have raised before _api was assigned, and the interpreter
        # clears instance dicts at shutdown, so this cannot assume the attribute
        api = getattr(self, '_api', None)
        if api:
            api.stop()
