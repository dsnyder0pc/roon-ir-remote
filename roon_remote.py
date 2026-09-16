"""
Implement a Roon Remote extension that reads keyboard events
from a FLIRC device and converts those events into transport
commands towards a certain _Zone_ in Roon.
"""
# !/usr/bin/env python
import logging
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import evdev
from evdev import InputDevice, ecodes, categorize

from app import RoonController, RoonOutput, RemoteConfig, RemoteConfigE, RemoteKeycodeMapping, RoonControllerE
# Import the specific exception for handling zone errors
from app.output import RoonOutputE

# Set default logging to INFO. DEBUG messages will be hidden.
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(module)s: %(message)s')
logger = logging.getLogger('roon_remote')

# how long the event loop may sit idle before it wakes up to check on the
# Roon session; nobody presses a button overnight, so without this the loop
# blocks in evdev forever and never notices a dead socket
HEARTBEAT_INTERVAL = 30

# the loop ticks every HEARTBEAT_INTERVAL, so silence for this long means it
# is wedged somewhere it cannot report from
WATCHDOG_TIMEOUT = 180


class Watchdog:
    """
    Guards the event loop against going silent.

    On 2026-09-14 the Roon session dropped and the process sat there for two
    days, logging nothing and answering no buttons, while systemd still saw a
    healthy service. Nothing inside the process noticed. This notices: the
    loop ticks as it goes round, and if the ticks stop the process ends so
    that Restart=on-failure gives us a fresh one.
    """

    def __init__(self, timeout: int = WATCHDOG_TIMEOUT, on_expire: Optional[Callable[[], None]] = None):
        self._timeout = timeout
        self._on_expire = on_expire or (lambda: os._exit(1))
        self._lock = threading.Lock()
        self._last_tick = time.monotonic()

    def tick(self) -> None:
        """Record that the event loop is still going round."""
        with self._lock:
            self._last_tick = time.monotonic()

    def silent_for(self) -> float:
        """Seconds since the last tick."""
        with self._lock:
            return time.monotonic() - self._last_tick

    def expired(self) -> bool:
        """True once the event loop has been silent for longer than the timeout."""
        return self.silent_for() > self._timeout

    def start(self) -> threading.Thread:
        """Run the watch in the background for the life of the process."""
        thread = threading.Thread(target=self.run, name='watchdog', daemon=True)
        thread.start()
        return thread

    def run(self) -> None:
        """Watch the event loop, and end the process once it stops ticking."""
        interval = max(self._timeout / 4, 0.05)
        while True:
            time.sleep(interval)
            if self.expired():
                logger.critical("event loop silent for %.0fs, exiting so systemd restarts the service",
                                self.silent_for())
                # the default on_expire is os._exit, which runs no handlers, so
                # push the line above out to the journal first
                for handler in logging.getLogger().handlers:
                    handler.flush()
                self._on_expire()
                return


def read_key_events(dev: InputDevice, timeout: float = HEARTBEAT_INTERVAL):
    """
    Yield input events, and None every `timeout` seconds while nothing is
    pressed. evdev's own read_loop() blocks until the next key, which leaves
    the caller no chance to check anything in between.
    """
    while True:
        readable, _, _ = select.select([dev.fd], [], [], timeout)
        if not readable:
            yield None
            continue
        try:
            yield from dev.read()
        except BlockingIOError:
            continue


def exit_handler(_received_signal, _frame):
    """Handle SIGINT and SIGTERM signals"""
    logger.info("Signaling internal jobs to stop...")
    sys.exit(0)


def get_event_device_for_string(dev_name: str):
    """Scan the Input Device tree for Flirc unit and return the device"""
    dev = None
    logger.debug('looking for input device "%s"', dev_name)
    devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
    if not devices:
        logger.error('no device found, invalid permissions?')
        return None
    for device in devices:
        logger.debug("name: %s" % device.name)
        if dev_name in device.name:
            dev = device
            logger.debug('found device with name: "%s" on path: %s', device.name, device.path)
            break
    return dev


def is_dac_present():
    """Check if the OS can see any soundcards using aplay."""
    try:
        # Run 'aplay -l', capturing stdout and redirecting stderr into it.
        result = subprocess.run(
            ['aplay', '-l'],
            stdout=subprocess.PIPE,      # Capture the standard output
            stderr=subprocess.STDOUT,   # Redirect stderr into stdout
            text=True,                  # Decode the output as text
            check=False
        )
        # Now result.stdout contains the combined output stream.
        if "no soundcards found" in result.stdout:
            logger.debug("aplay -l output indicates no soundcards.")
            return False
        else:
            logger.debug("aplay -l output indicates one or more soundcards.")
            return True
    except FileNotFoundError:
        logger.error("'aplay' command not found. Cannot check for DAC presence.")
        return True
    except Exception as e:
        logger.error("An error occurred while checking for DAC: %s", e)
        return True


def key_names_for(code: int) -> List[str]:
    """
    Return every evdev name for a key code. Codes that several names share
    (KEY_MUTE is also KEY_MIN_INTERESTING) come back from evdev as a list.
    """
    names = ecodes.KEY[code]
    if isinstance(names, (list, tuple)):
        return list(names)
    return [names]


def build_transport_actions(zone: RoonOutput) -> Dict[str, Callable[[], None]]:
    """Map every transport action name onto the call that performs it."""

    def play_pause():
        if zone.state == "playing":
            zone.pause()
        else:
            zone.repeat(False)
            zone.play()

    return {
        'prev': zone.previous,
        'skip': zone.skip,
        'stop': zone.stop,
        'play_pause': play_pause,
        'vol_up': lambda: zone.volume_up(2),
        'vol_down': lambda: zone.volume_down(2),
        'mute': lambda: zone.mute(not zone.is_muted()),
        'fall_asleep': lambda: zone.play_playlist('wellenrauschen'),
        'play_radio': lambda: zone.play_radio_station(station_name="Radio Paradise (320k aac)"),
    }


def monitor_remote(zone: RoonOutput, dev: InputDevice, mapping: RemoteKeycodeMapping, zone_name: str,
                   *, controller: Optional[RoonController] = None, watchdog: Optional[Watchdog] = None):
    """start an event loop on InputDevice"""
    logger.info("Starting event monitor for zone: '%s'", zone_name)

    if not dev:
        raise RuntimeError('could not open DEV')

    transport_actions = build_transport_actions(zone)

    logger.debug('opening exclusively InputDevice: %s', dev.path)
    for event in read_key_events(dev, HEARTBEAT_INTERVAL):
        if watchdog:
            watchdog.tick()

        if event is None:
            # idle tick: nobody pressed anything, so check the session instead
            if controller and not controller.check_alive():
                raise RoonControllerE('Roon session is no longer alive')
            continue

        if event.value != 1:
            # ignore everything that is not KEY_DOWN
            continue

        key_names = key_names_for(event.code)
        logging.debug(str(categorize(event)))

        action = next((a for a in map(mapping.to_action, key_names) if a), None)
        if action is None:
            # a remote has more buttons than we bind; pressing one is not an error
            logger.debug("ignoring unmapped key %s (code %s)", key_names, event.code)
            continue

        handler = transport_actions.get(action)
        if handler is None:
            logger.warning("no handler for transport action '%s'", action)
            continue

        try:
            handler()
            logger.debug("Received Code: %s", repr(event.code))

        except RoonOutputE as e:
            logger.warning("Lost connection to zone '%s'.", zone_name)
            raise e
        except Exception as exception:
            logging.error("Caught non-critical exception in monitor loop: %s (%s)", exception, type(exception))
    logger.info("Job monitorRemote stopped")


def main():
    """main function, initiate InputDevice and runs the forever loop"""
    logger.info("starting %s", __file__)
    signal.signal(signal.SIGINT, exit_handler)
    signal.signal(signal.SIGTERM, exit_handler)

    try:
        config = RemoteConfig(Path('app_info.json'))
    except RemoteConfigE as ex:
        logging.error(ex.msg)
        sys.exit(1)

    mapping = config.key_mapping
    logging.info(mapping.edge)

    device_names_to_try = ["flirc Keyboard", "gpio_ir_recv"]
    event_dev = None
    input_dev_name = None

    for name in device_names_to_try:
        device = get_event_device_for_string(name)
        if device:
            event_dev = device
            input_dev_name = name
            logging.info('Found InputDevice: "%s"', input_dev_name)
            break

    if not event_dev:
        logging.error('Could not find a valid InputDevice. Tried: %s', device_names_to_try)
        sys.exit(1)

    watchdog = Watchdog()
    watchdog.start()

    # --- Outer loop for Core connection resiliency ---
    while True:
        controller = None
        # 1. Establish connection to the Roon Core
        while not controller:
            watchdog.tick()
            try:
                controller = RoonController(config.app_info, Path('.roon-token'))
                logging.info("Successfully connected to Roon Core.")
            except RoonControllerE as ex:
                logging.error("Failed to connect to Roon Core: %s. Retrying in 60 seconds...", ex.msg)
                time.sleep(60)
            except Exception as ex:
                # anything the controller or the Roon API throws on the way up
                # must keep us in this loop, never take the process down
                logging.error("Unexpected error connecting to Roon Core: %s (%s). Retrying in 60 seconds...",
                              ex, type(ex).__name__)
                time.sleep(60)

        # --- Inner loop for Zone discovery and event monitoring ---
        has_logged_zone_warning = False
        while True:
            output = None
            watchdog.tick()
            try:
                # 2. Find the specified Zone
                output = controller.get_output(config.zone)
                logging.info('Successfully found zone: "%s"', config.zone)
                has_logged_zone_warning = False  # Reset flag on success

                # 3. Monitor for remote control events
                monitor_remote(output, event_dev, mapping, config.zone,
                               controller=controller, watchdog=watchdog)
                logging.warning("Event monitor stopped unexpectedly. Re-checking for zone...")
                has_logged_zone_warning = False

            except RoonOutputE:
                if not has_logged_zone_warning:
                    if not is_dac_present():
                        logging.warning('Zone "%s" not found. No audio device detected by OS. Is the DAC powered on? Waiting for zone to appear...', config.zone)
                    else:
                        logging.warning('Zone "%s" not found, but an audio device IS detected. Check for a typo in the configured zone name. Waiting for zone to appear...', config.zone)
                    has_logged_zone_warning = True
                else:
                    logging.debug("Still waiting for zone '%s'...", config.zone)
                time.sleep(30)
                # Continue in the inner loop to retry finding the zone without full reconnection

            except (RoonControllerE, ConnectionError) as e:
                logging.error("Connection to Roon Core lost: %s. Reconnecting...", e)
                break  # Break inner loop to trigger outer loop's reconnection logic

            except Exception as e:
                logging.error("A critical error occurred: %s. Reconnecting...", e)
                break  # Break inner loop to trigger outer loop's reconnection logic

        # If we break from the inner loop, shutdown and wait before retrying the Core connection
        if controller:
            controller.shutdown()
        time.sleep(10)


if __name__ == '__main__':
    main()
