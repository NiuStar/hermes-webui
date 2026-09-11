"""V11 contract regressions. Run only in the isolated unified test stage."""
import os
import socket
import time
import pytest
from api.display_bootstrap_observer_wire import frame, validate, expect
from api.display_bootstrap_launch_control import send, receive
from api.display_bootstrap_credential_stream import CredentialStream
from api.display_bootstrap_growth_lease import issue, revoke


def common():
    return dict(version=3, batch_id='1'*32, connection_nonce='2'*32, nonce='3'*32,
                deployment_sha='4'*64, resource_sha='5'*64, role='creator',
                runner_profile_id='6'*32, runner_profile_sha='7'*64,
                volume_profile_id='8'*32, volume_profile_sha='9'*64,
                observer_profile_id='a'*32, observer_profile_sha='b'*64)


def bound():
    return dict(common(), server_nonce='c'*32, candidate_id='d'*32,
                operation='create', request_sha='e'*64)


@pytest.mark.parametrize('key', ['version', 'seq'])
def test_observe_boolean_integers_rejected(key):
    value = frame('OBSERVE', bound(), seq=1, previous_result_sha=None)
    value[key] = True
    with pytest.raises(ValueError):
        validate(value)


def test_initial_and_later_digest_rules():
    with pytest.raises(ValueError):
        frame('OBSERVE', bound(), seq=1, previous_result_sha='f'*64)
    with pytest.raises(ValueError):
        frame('OBSERVE', bound(), seq=2, previous_result_sha=None)
    for seq in (1, 1024):
        validate(frame('OBSERVE', bound(), seq=seq, previous_result_sha=None if seq == 1 else 'f'*64))
    with pytest.raises(ValueError):
        frame('OBSERVE', bound(), seq=1025, previous_result_sha='f'*64)


def test_operation_cannot_cross_role():
    with pytest.raises(ValueError):
        frame('BEGIN', dict(bound(), role='approver'))
    validate(frame('BEGIN', dict(bound(), role='approver', operation='approve')))


def test_binding_and_extra_fields_rejected():
    value = frame('OBSERVE', bound(), seq=1, previous_result_sha=None)
    with pytest.raises(ValueError):
        expect(value, 'OBSERVE', dict(bound(), candidate_id='f'*32))
    with pytest.raises(ValueError):
        validate(dict(value, ignored=True))


def control(kind):
    return dict(version=1, type=kind, batch_id='a'*32, connection_nonce='b'*32,
                role='observer', pid=os.getpid(), starttime=1, policy_sha='c'*64)


def test_control_roundtrip_and_type_confusion():
    for kind in ('READY', 'GO', 'ARMED'):
        read, write = os.pipe2(os.O_CLOEXEC)
        deadline = time.monotonic() + 1
        send(write, control(kind), deadline=deadline)
        assert receive(read, control(kind), deadline=deadline) == control(kind)
    read, write = os.pipe2(os.O_CLOEXEC)
    deadline = time.monotonic() + 1
    send(write, control('READY'), deadline=deadline)
    with pytest.raises(ValueError, match='APPROVAL_MISMATCH'):
        receive(read, control('ARMED'), deadline=deadline)
    with pytest.raises(OSError):
        os.fstat(read)


def test_strict_stream_requires_preconfigured_passcred():
    left, right = socket.socketpair()
    with left, right:
        with pytest.raises(ValueError, match='ACCESS_BOUNDARY_UNPROVEN'):
            CredentialStream(left, time.monotonic() + 1, strict=True)


def test_lease_does_not_restart_age_at_issue():
    now = time.monotonic_ns()
    with pytest.raises(ValueError, match='RESOURCE_LIMIT'):
        issue(owner=object(), candidate_id='1'*32, operation='create', request_sha='2'*64,
              bindings={}, observation={}, deadline=time.monotonic()+10,
              verify_owner=lambda: None, request_started_ns=now-1_000_000_000)
