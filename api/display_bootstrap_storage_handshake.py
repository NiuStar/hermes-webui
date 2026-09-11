"""V2 credential-bound handshake; injected verifiers must provide live authority.

No default verifier or compatibility fallback. This module does not issue a
BootstrapContext or install a service. ACK is cleanup, not a server receipt.
"""
import secrets
from api.display_bootstrap_credential_stream import CredentialStream
from api.display_bootstrap_manifest import canonical_bytes, digest
from api.display_bootstrap_v3 import hex_value

BINDING_FIELDS = {'deployment_sha', 'resource_sha', 'role', 'runner_profile_id',
                  'runner_profile_sha', 'volume_profile_id', 'volume_profile_sha',
                  'observer_profile_id', 'observer_profile_sha'}


def validate_binding(binding):
    if type(binding) is not dict or set(binding) != BINDING_FIELDS:
        raise ValueError('INVALID_INPUT')
    if binding['role'] not in ('creator', 'publisher', 'recover'):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    for key in BINDING_FIELDS - {'role'}:
        hex_value(binding[key], 32 if key.endswith('_id') else 64)
    return binding


def frame(kind, binding, nonce, server_nonce=None, **extra):
    result = dict(binding, version=2, type=kind, nonce=nonce, **extra)
    if server_nonce is not None:
        result['server_nonce'] = server_nonce
    return result


def expect(value, kind, binding, nonce, server_nonce=None, extra=()):
    expected = frame(kind, binding, nonce, server_nonce)
    if type(value) is not dict or set(value) != set(expected) | set(extra):
        raise ValueError('INVALID_INPUT')
    if type(value['version']) is not int or any(value[k] != v for k, v in expected.items()):
        raise ValueError('APPROVAL_MISMATCH')
    return value


def client(sock, binding, *, deadline, hold_observer, validate_observation):
    validate_binding(binding)
    if not callable(hold_observer) or not callable(validate_observation):
        raise ValueError('INVALID_INPUT')
    stream = CredentialStream(sock, deadline)
    nonce = secrets.token_hex(16)
    stream.send(frame('HELLO', binding, nonce))
    ready = stream.receive()
    server_nonce = ready.get('server_nonce')
    hex_value(server_nonce, 32)
    expect(ready, 'READY', binding, nonce, server_nonce)
    if stream.peer[1:] != (0, 0):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    with hold_observer(stream.peer, binding, deadline) as verify:
        verify()
        stream.send(frame('OBSERVE', binding, nonce, server_nonce))
        result = expect(stream.receive(), 'RESULT', binding, nonce, server_nonce,
                        extra=('observation', 'observation_sha'))
        if type(result['observation']) is not dict:
            raise ValueError('INVALID_INPUT')
        sha = digest(canonical_bytes(result['observation']))
        if result['observation_sha'] != sha:
            raise ValueError('APPROVAL_MISMATCH')
        validate_observation(result['observation'])
        verify()
        stream.send(frame('ACK', binding, nonce, server_nonce, observation_sha=sha))
        stream.eof()
        return result['observation']


def server(sock, *, deadline, hold_worker, observe):
    if not callable(hold_worker) or not callable(observe):
        raise ValueError('INVALID_INPUT')
    stream = CredentialStream(sock, deadline)
    hello = stream.receive()
    binding = {k: hello[k] for k in BINDING_FIELDS if k in hello}
    validate_binding(binding)
    nonce = hello.get('nonce')
    hex_value(nonce, 32)
    expect(hello, 'HELLO', binding, nonce)
    server_nonce = secrets.token_hex(16)
    with hold_worker(stream.peer, binding, deadline) as verify:
        verify()
        stream.send(frame('READY', binding, nonce, server_nonce))
        expect(stream.receive(), 'OBSERVE', binding, nonce, server_nonce)
        verify()
        observation = observe(binding, stream.peer, deadline)
        if type(observation) is not dict:
            raise ValueError('INVALID_INPUT')
        sha = digest(canonical_bytes(observation))
        verify()
        stream.send(frame('RESULT', binding, nonce, server_nonce,
                          observation=observation, observation_sha=sha))
        ack = expect(stream.receive(), 'ACK', binding, nonce, server_nonce,
                     extra=('observation_sha',))
        if ack['observation_sha'] != sha:
            raise ValueError('APPROVAL_MISMATCH')
        # The ACK digest must be checked independently of shared binding fields.
        # Read exactly once; no extra request is accepted on this connection.
        verify()
