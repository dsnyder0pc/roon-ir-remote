"""
Implement a Roon Remote extension that reads keyboard events
from a FLIRC device and converts those events into transport
commands towards a certain _Zone_ in Roon.
"""
# !/usr/bin/env python
import logging
import signal
import sys
import time
from pathlib import Path

import evdev
from evdev import InputDevice, ecodes, categorize

from app import RoonController, RoonOutput, RemoteConfig, RemoteConfigE, RemoteKeycodeMapping, RoonControllerE
# Import the specific exception for handling zone errors
from app.output import RoonOutputE

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(module)s: %(message)s')
logger = logging.getLogger('roon_remote')


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


def monitor_remote(zone: RoonOutput, dev: InputDevice, mapping: RemoteKeycodeMapping, zone_name: str):
    """start an event loop on InputDevice"""
    logger.info("Starting event monitor for zone: '%s'", zone_name)

    if not dev:
        raise BaseException('could not open DEV')

    logger.debug('opening exclusively InputDevice: %s', dev.path)
    for event in dev.read_loop():
        if event.value != 1:
            # ignore everything that is not KEY_DOWN
            continue

        event_name = ecodes.KEY[event.code]
        logging.debug(str(categorize(event)))
        try:
            if event_name == mapping.to_key_code('prev'):
                zone.previous()
            elif event_name == mapping.to_key_code('skip'):
                zone.skip()
            elif event_name == mapping.to_key_code('stop'):
                zone.stop()
            elif event_name == mapping.to_key_code('play_pause'):
                if zone.state == "playing":
                    zone.pause()
                else:
                    zone.repeat(False)
                    zone.play()
            elif event_name == mapping.to_key_code('vol_up'):
                zone.volume_up(2)
            elif event_name == mapping.to_key_code('vol_down'):
                zone.volume_down(2)
            elif mapping.to_key_code('mute') in event_name:
                zone.mute(not zone.is_muted())
            elif event_name == mapping.to_key_code('fall_asleep'):
                zone.play_playlist('wellenrauschen')
            elif event_name == mapping.to_key_code('play_radio'):
                zone.play_radio_station(station_name="Radio Paradise (320k aac)")

            logger.debug("Received Code: %s", repr(event.code))

        except RoonOutputE as e:
            # The zone is gone. Re-raise the exception to be handled by the main loop.
            logger.warning("Lost connection to zone '%s'.", zone_name)
            raise e
        except Exception as exception:
            # For other, non-critical errors, just log them and continue monitoring.
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

    # List of device names to try, in order of preference
    device_names_to_try = ["flirc Keyboard", "gpio_ir_recv"]
    event_dev = None
    input_dev_name = None

    # Loop through the names until a device is found
    for name in device_names_to_try:
        device = get_event_device_for_string(name)
        if device:
            event_dev = device
            input_dev_name = name  # Preserves the name of the found device
            logging.info('Found InputDevice: "%s"', input_dev_name)
            break  # Exit the loop on first success

    if not event_dev:
        logging.error('Could not find a valid InputDevice. Tried: %s', device_names_to_try)
        sys.exit(1)

    # --- Main Application Resiliency Loop ---
    # This loop runs forever. If the connection to the Roon Core or the Zone
    # is lost, it will be caught and the loop will restart the connection process.
    while True:
        controller = None
        output = None

        # 1. Connect to the Roon Core
        try:
            if not controller:
                controller = RoonController(config.app_info, Path('.roon-token'))
                logging.info("Successfully connected to Roon Core.")
        except RoonControllerE as ex:
            logging.error("Failed to connect to Roon Core: %s. Retrying in 30 seconds...", ex.msg)
            time.sleep(30)
            continue

        # 2. Find the specified Zone
        try:
            if not output:
                output = controller.get_output(config.zone)
                logging.info('Successfully found zone: "%s"', config.zone)
        except RoonOutputE:
            logging.warning('Zone "%s" not found. Is the device powered on? Retrying in 30 seconds...', config.zone)
            time.sleep(30)
            continue

        # 3. Monitor for remote control events
        try:
            # This function will block until an error occurs (e.g., zone disappears)
            monitor_remote(output, event_dev, mapping, config.zone)
            logging.warning("Event monitor stopped unexpectedly. Restarting...")
        except RoonOutputE as e:
            # This is expected if the zone disappears during operation
            logging.warning("Zone communication error: %s. Re-establishing connection...", e)
        except Exception as e:
            # This catches other critical errors, like the input device disconnecting
            logging.error("A critical error occurred: %s. Restarting...", e)

        # Wait a bit before restarting the whole process from the top
        controller.shutdown()
        time.sleep(10)


if __name__ == '__main__':
    main()
