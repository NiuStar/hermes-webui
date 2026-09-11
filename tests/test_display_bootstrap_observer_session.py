"""Scripted peer regressions; these do NOT replace real kernel/platform gates."""
from contextlib import contextmanager
from unittest.mock import patch
import pytest
from api.display_bootstrap_observer_session import ObserverSession
from api.display_bootstrap_observer_wire import frame
from api.display_bootstrap_manifest import canonical_bytes, digest
from tests.test_display_bootstrap_observer_v3 import common, bound


@contextmanager
def hold(*args):
    yield lambda: None


class Sock:
    closed = False
    shutdowns = 0
    def close(self):
        self.closed = True
    def shutdown(self, how):
        self.shutdowns += 1


class Peer:
    """Scripted wire, deliberately not a transport/credential substitute."""
    def __init__(self, sock, deadline, strict):
        self.peer = (123, 0, 0)
        self.sent = []
        self.pending = []
        self.n = 0
        self.binding = None
    def timeout(self):
        pass
    def send(self, value):
        self.sent.append(value)
        kind = value['type']
        if kind == 'HELLO':
            self.binding = {k: v for k, v in value.items() if k != 'type'}
            self.binding['server_nonce'] = 'c'*32
            self.pending.append(frame('READY', self.binding))
        elif kind == 'BEGIN':
            self.binding = {k: v for k, v in value.items() if k != 'type'}
            self.pending.append(frame('BOUND', self.binding))
        elif kind == 'OBSERVE':
            self.n += 1
            sample = dict(format_version=1, started_ns=100, finished_ns=110,
                          storage=dict(loop={}, host={}, views={}, observer_self={}),
                          capacity={})
            # Sample validation is separately tested; this test checks sequencing.
            self.pending.append(frame('RESULT', self.binding, seq=value['seq'],
                previous_result_sha=value['previous_result_sha'], sample=sample,
                sample_sha=digest(canonical_bytes(sample))))
        elif kind == 'ACK':
            self.pending.append(frame('COMMITTED', self.binding,
                                      seq=value['seq'], result_sha=value['result_sha']))
        elif kind == 'CLOSE':
            self.pending.append(frame('CLOSED', self.binding,
                                      last_seq=value['last_seq'], last_result_sha=value['last_result_sha']))
    def receive(self):
        return self.pending.pop(0)
    def eof(self):
        assert not self.pending


def session():
    data = common()
    data.pop('version')
    data.pop('nonce')
    operation = {k: bound()[k] for k in ('candidate_id', 'operation', 'request_sha')}
    return ObserverSession(Sock(), data, deadline=100, hold_observer=hold,
        wait_go=lambda: operation, validate_storage=lambda v: None,
        verify_capacity=lambda v: None, verify_clock=lambda: None)


def test_two_rounds_chain_and_explicit_close():
    with patch('api.display_bootstrap_observer_session.CredentialStream', Peer), \
         patch('api.display_bootstrap_observer_session.validate_sample', lambda v, check: v), \
         patch('api.display_bootstrap_observer_session.time.monotonic_ns', side_effect=[90,120,90,120]):
        value = session()
        value.observe()
        previous = value.previous
        value.observe()
        requests = [v for v in value.stream.sent if v['type'] == 'OBSERVE']
        assert [v['seq'] for v in requests] == [1, 2]
        assert requests[0]['previous_result_sha'] is None
        assert requests[1]['previous_result_sha'] == previous
        value.close()
        assert value.closed and value.sock.closed and value.sock.shutdowns == 1
        assert value.stream.n == 2


def test_failed_sample_permanently_closes_session():
    with patch('api.display_bootstrap_observer_session.CredentialStream', Peer), \
         patch('api.display_bootstrap_observer_session.validate_sample', side_effect=ValueError('INVALID_INPUT')):
        value = session()
        with pytest.raises(ValueError, match='INVALID_INPUT'):
            value.observe()
        assert value.closed and value.sock.closed
        with pytest.raises(ValueError, match='ACCESS_BOUNDARY_UNPROVEN'):
            value.observe()
