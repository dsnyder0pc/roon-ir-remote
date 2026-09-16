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

# Connects abandoned in flight, as (thread, websocket) pairs.
#
# websocket-client assigns the WebSocket to the app before connect() returns,
# so RoonApiWebSocket.stop() drops its own reference while the OS socket is
# still inside connect(): nothing there can close it. If the connect lands
# afterwards, the connection stays ESTABLISHED and we are the only ones left
# holding the object that can release it. Swept from the main thread only.
_abandoned_connects = []


def remember_abandoned_connect(socket) -> None:
    """Hold on to a connect we are walking away from, so it can be closed."""
    # pylint: disable=protected-access
    pending = getattr(getattr(socket, '_socket', None), 'sock', None)
    if pending is not None:
        _abandoned_connects.append((socket, pending))


def close_abandoned_connects() -> int:
    """
    Close any abandoned connect that has since landed, and forget the ones
    that never will. Returns the number of sockets closed.
    """
    closed = 0
    for entry in list(_abandoned_connects):
        thread, pending = entry
        if getattr(pending, 'connected', False) or getattr(pending, 'sock', None) is not None:
            try:
                pending.close()
                closed += 1
            except OSError as ex:
                logger.debug('closing an abandoned connect: %s' % ex)
            _abandoned_connects.remove(entry)
        elif not thread.is_alive():
            # the connect failed and the thread is gone; nothing was left open
            _abandoned_connects.remove(entry)
    if closed:
        logger.info('closed %d abandoned connect(s) that landed after we gave up' % closed)
    return closed


class SocketAwareRoonApi(RoonApi):
    """
    A RoonApi whose first requests wait for the websocket instead of failing
    into it.

    With blocking_init=False the constructor asks for zones and outputs
    straight away, before the socket has connected. send_request logs an
    error and returns False, and _request then spins 2.5s waiting on a reply
    that was never sent - twice, so every startup costs five seconds and two
    misleading errors in the journal.

    The library means to handle this: _request has a wait-for-ready loop. It
    guards it with `if not self._roonsocket`, which _server_setup has already
    made truthy, so the loop never runs.

    The wait is bounded by a deadline taken at construction, so this cannot
    become the hang that blocking_init was. On expiry, or against a socket
    that has already failed, it falls through to the library's own behaviour
    and _wait_until_ready reports the failed connect as it does now.
    """

    SOCKET_WAIT = 30

    def __init__(self, *args, on_progress=None, **kwargs):
        self._ready_deadline = time.monotonic() + self.SOCKET_WAIT
        self._progress = on_progress or (lambda: None)
        super().__init__(*args, **kwargs)

    def _await_ready(self) -> None:
        """Wait for registration, but never past the deadline."""
        while not self.ready and time.monotonic() < self._ready_deadline:
            socket = getattr(self, '_roonsocket', None)
            if socket is not None and getattr(socket, 'failed_state', False):
                return  # the connection is already lost; nothing to wait for
            self._progress()
            time.sleep(0.05)

    def _get_zones(self):
        self._await_ready()
        return super()._get_zones()

    def _get_outputs(self):
        self._await_ready()
        return super()._get_outputs()


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

    # RoonApi's own blocking init waits for the core forever, and its socket
    # watcher only starts once the constructor returns, so a core that is not
    # answering parks the process with nothing left to recover it. Normally
    # the api's own deadline governs; this is the fallback.
    CONNECT_TIMEOUT = SocketAwareRoonApi.SOCKET_WAIT

    # connected but unregistered means Roon is waiting for someone to enable
    # the extension in Settings > Extensions, so give that a person's patience
    AUTHORIZE_TIMEOUT = 300

    # zones and outputs arrive on the subscription just after registration
    SUBSCRIBE_TIMEOUT = 10

    def __init__(self, app_info: Dict, token: Path = '.roon-token', on_progress=None):
        super(RoonController, self).__init__()
        self._info = app_info
        self._token_path = token
        self._token = None
        self._zone = None
        self._last_probe = time.monotonic()
        # called while we wait, so a caller watching for a wedged process can
        # tell "still waiting for the core" from "stopped running"
        self._on_progress = on_progress or (lambda: None)

        close_abandoned_connects()

        server = self._discover_server()
        if not server or not server[0]:
            logger.error("failed to discover a server")
            raise RoonControllerE("failed to discover a Roon server")

        logger.debug("Received: %s" % server[0])

        if token:
            self._token = RoonToken(token)

        token = None if self._token.is_empty() else self._token.to_string()
        self._api = SocketAwareRoonApi(self._info, token=token, host=server[0], port=server[1],
                                       blocking_init=False, on_progress=self._on_progress)
        self._wait_until_ready(server[0], server[1])

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
        return RoonOutput(self._api, name)

    @staticmethod
    def _read_as_json(path) -> Dict:
        _data = {}
        with open(path) as f:
            _data = json.load(f)
        return _data

    def _wait_until(self, timeout: float, is_done) -> bool:
        """Poll is_done until it is true or the timeout runs out."""
        deadline = time.monotonic() + timeout
        while not is_done() and time.monotonic() < deadline:
            self._on_progress()
            time.sleep(0.05)
        return is_done()

    def _socket(self):
        """The RoonApi websocket, which is private to the library."""
        # pylint: disable=protected-access
        return getattr(self._api, '_roonsocket', None)

    def _socket_is_open(self) -> bool:
        socket = self._socket()
        if socket is None or getattr(socket, 'failed_state', False):
            return False
        return bool(getattr(socket, 'connected', False))

    def _wait_for_socket(self, host, port) -> None:
        """
        Wait for the websocket, giving up early if it has already failed.

        Shares the deadline the api took at construction, which it has
        already spent some of waiting for this same thing. Two independent
        timeouts for one condition only doubles how long a dead core takes
        to report.
        """
        deadline = getattr(self._api, '_ready_deadline', None)
        if deadline is None:
            deadline = time.monotonic() + self.CONNECT_TIMEOUT
        while time.monotonic() < deadline:
            if self._socket_is_open():
                return
            socket = self._socket()
            if socket is None or getattr(socket, 'failed_state', False):
                raise RoonControllerE("websocket to %s:%s failed" % (host, port))
            self._on_progress()
            time.sleep(0.05)
        raise RoonControllerE("no websocket to %s:%s after %ds" % (host, port, self.CONNECT_TIMEOUT))

    def _wait_until_ready(self, host, port) -> None:
        """
        Wait for the socket and the registration, but not forever.

        RoonApi's blocking init has no timeout: point it at a core that is not
        answering and the constructor never returns, which parks the caller in
        a place where even the library's own socket watcher has not started.
        """
        try:
            self._wait_for_socket(host, port)

            if not getattr(self._api, 'ready', False):
                logger.info("connected to %s, waiting for the extension to be enabled in Roon" % host)
            if not self._wait_until(self.AUTHORIZE_TIMEOUT, lambda: bool(getattr(self._api, 'ready', False))):
                raise RoonControllerE("%s did not register us within %ds; is the extension enabled in Roon?"
                                      % (host, self.AUTHORIZE_TIMEOUT))
        except RoonControllerE:
            self._abandon()
            raise

        if not self._wait_until(self.SUBSCRIBE_TIMEOUT, lambda: bool(self._api.outputs)):
            logger.warning("no outputs from %s yet, carrying on" % host)

    def _abandon(self) -> None:
        """Stop the half-built api, keeping hold of a connect still in flight."""
        remember_abandoned_connect(self._socket())
        self._api.stop()

    def is_connected(self) -> bool:
        """Passive check of the websocket state. Cheap, safe to call often."""
        return self._socket_is_open() and bool(getattr(self._api, 'ready', False))

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

        close_abandoned_connects()

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
