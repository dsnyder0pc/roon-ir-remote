
import json
from json import JSONDecodeError
from typing import Dict, List, Optional
from pathlib import Path
import logging

logger = logging.getLogger('RemoteConfig')


class RemoteConfigE(Exception):
    """ implement a basic exception for config related issues"""

    def __init__(self, msg):
        super(RemoteConfigE, self).__init__(msg)
        self.msg = msg


class RemoteKeycodeMapping:
    """
    contains the configured mapping from keyboard codes to Roon transport actions
    """
    EDGE_UP = "UP"
    EDGE_DOWN = "DOWN"

    # transport actions the event loop knows how to perform, in the order they
    # win when two of them are bound to the same key code
    ACTIONS = ('prev', 'skip', 'stop', 'play_pause', 'vol_up', 'vol_down', 'mute')

    def __init__(self, mapping_dict: Dict):
        if 'codes' not in mapping_dict.keys():
            raise RemoteConfigE('codes not found')
        self._dict = mapping_dict
        self._by_key_code = None
        if 'edge' not in mapping_dict.keys():
            logger.info('no "edge" config detected, setting default UP ')
            self._dict['edge'] = self.EDGE_UP

    @property
    def edge(self) -> str:
        key_name = "edge"
        if key_name not in self._dict.keys():
            raise RemoteConfigE('no key "edge" detected in config')
        return self._dict[key_name]

    def to_key_code(self, transport_action: str) -> List[int]:
        """
        convert transport action into key codes

        Args:
            transport_action (str): one of 'play', 'stop', 'skip', 'prev', 'playpause'
        """
        if 'codes' not in self._dict.keys():
            raise RemoteConfigE('no "codes" key found')

        if transport_action not in self._dict['codes'].keys():
            raise RemoteConfigE(f'no {transport_action} key found')

        return self._dict['codes'][transport_action]

    def has_key_code(self, transport_action: str) -> bool:
        """Return True if the config binds a key to this transport action."""
        return transport_action in self._dict.get('codes', {})

    def to_action(self, key_code: str) -> Optional[str]:
        """
        Reverse of to_key_code: return the transport action bound to a key
        code, or None when the key is not configured. Unmapped keys are a
        normal thing to see from a remote, so they must not raise.

        Args:
            key_code (str): an evdev key name, e.g. 'KEY_ENTER'
        """
        if self._by_key_code is None:
            self._by_key_code = self._build_reverse_map()
        return self._by_key_code.get(key_code)

    def _build_reverse_map(self) -> Dict[str, str]:
        """Index the configured codes by key code, once."""
        if 'codes' not in self._dict.keys():
            raise RemoteConfigE('no "codes" key found')

        codes = self._dict['codes']
        unknown = sorted(set(codes.keys()) - set(self.ACTIONS))
        if unknown:
            logger.warning('ignoring unknown transport action(s) in config: %s',
                           ', '.join(unknown))

        reverse = {}
        for action in self.ACTIONS:
            if action not in codes:
                continue
            key_codes = codes[action]
            if not isinstance(key_codes, (list, tuple)):
                key_codes = [key_codes]
            for key_code in key_codes:
                reverse.setdefault(str(key_code), action)
        logger.debug('key code mapping: %s', reverse)
        return reverse


class RemoteConfig:
    """
    Provides an interface to the config file structure
    """

    def __init__(self, path_config_file: Path):
        super(RemoteConfig).__init__()
        logger.debug(path_config_file.absolute())
        if not path_config_file.exists():
            raise RemoteConfigE('RemoteConfig, invalid path given: {}'.format(path_config_file))
        self._config = RemoteConfig._read_as_json(path_config_file)['roon']
        logger.debug('successfully read config from %s', path_config_file)

    @staticmethod
    def _read_as_json(path: Path) -> Dict:
        """Open a file and return JSON content"""
        data = {}
        with path.open(mode='r') as f:
            try:
                data = json.load(f)
            except JSONDecodeError as ex:
                pass
        return data

    @property
    def app_info(self):
        return self._config['app_info']

    @property
    def zone(self):
        return self._config['zone']['name']

    @property
    def key_mapping(self) -> RemoteKeycodeMapping:
        """return a keycode mapping object"""
        return RemoteKeycodeMapping(self._config['event_mapping'])
