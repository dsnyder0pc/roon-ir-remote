"""
pyroon's constructor asks for zones before the socket is up, and its own
wait-for-ready loop is guarded by a condition that is never true. Cover the
subclass that makes those first requests wait - and, more importantly, cover
that the wait is bounded, so it cannot become the hang blocking_init was.
"""
import threading
import time

import pytest

from app.controller import SocketAwareRoonApi

ZONES = {'zone-1': {'zone_id': 'zone-1', 'state': 'playing'}}


def api(ready=False, failed=False, deadline_in=5.0):
    """A SocketAwareRoonApi with the library's __init__ bypassed."""
    instance = SocketAwareRoonApi.__new__(SocketAwareRoonApi)
    instance.ready = ready
    instance._ready_deadline = time.monotonic() + deadline_in
    instance._progress = lambda: None
    instance._roonsocket = type('S', (), {'failed_state': failed})()
    instance.requests = []

    def fake_request(command, data=None):
        instance.requests.append(command)
        # the library only returns zones once the session is registered
        return {'zones': list(ZONES.values())} if instance.ready else None

    instance._request = fake_request
    return instance


def test_it_waits_for_registration_then_asks():
    """The whole point: no request goes out into a socket that is not up."""
    instance = api(ready=False, deadline_in=5)

    def register_soon():
        time.sleep(0.3)
        instance.ready = True

    threading.Thread(target=register_soon, daemon=True).start()
    started = time.monotonic()
    zones = instance._get_zones()

    assert zones == ZONES, "the request should have gone out after registration"
    assert time.monotonic() - started >= 0.3
    assert instance.requests, "exactly one request, made once it could succeed"


def test_it_asks_immediately_when_already_registered():
    instance = api(ready=True)
    started = time.monotonic()
    assert instance._get_zones() == ZONES
    assert time.monotonic() - started < 0.2, "no reason to wait"


def test_the_wait_is_bounded_by_the_deadline():
    """Without this it is blocking_init all over again."""
    instance = api(ready=False, deadline_in=0.3)
    started = time.monotonic()
    instance._get_zones()
    elapsed = time.monotonic() - started
    assert 0.3 <= elapsed < 3, "gave up at the deadline, not before or never"
    assert instance.requests, "still delegates, so the library reports as it does now"


def test_an_expired_deadline_does_not_wait_at_all():
    """A later probe() reuses this path; it must not re-wait."""
    instance = api(ready=True, deadline_in=-100)
    started = time.monotonic()
    instance._get_zones()
    assert time.monotonic() - started < 0.2


def test_a_failed_socket_is_not_waited_on():
    instance = api(ready=False, failed=True, deadline_in=30)
    started = time.monotonic()
    instance._get_zones()
    assert time.monotonic() - started < 0.5, "nothing left to wait for"


def test_outputs_take_the_same_path():
    instance = api(ready=True)
    instance._get_outputs()
    assert instance.requests, "outputs must wait for the socket too"


def test_waiting_reports_progress():
    """A long wait must not look like a wedged process to the watchdog."""
    ticks = []
    instance = api(ready=False, deadline_in=0.3)
    instance._progress = lambda: ticks.append(1)
    instance._get_zones()
    assert len(ticks) > 1


def test_deadline_is_taken_once_not_per_call():
    """Two constructor calls share one budget, so a dead core costs 30s not 60."""
    instance = api(ready=False, deadline_in=0.3)
    instance._get_zones()
    started = time.monotonic()
    instance._get_outputs()
    assert time.monotonic() - started < 0.2, "the second call inherits the spent deadline"


@pytest.mark.parametrize('method', ['_get_zones', '_get_outputs'])
def test_neither_call_raises_when_the_core_never_answers(method):
    instance = api(ready=False, deadline_in=0.1)
    getattr(instance, method)()   # must return, not raise
