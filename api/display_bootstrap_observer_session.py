"""V3 same-process observer session; launcher supplies authenticated GO gates.

No reconnect, authority defaults, background work, or lease issuance here.
"""
import os
import secrets
import socket
import time
from contextlib import ExitStack
from api.display_bootstrap_credential_stream import CredentialStream
from api.display_bootstrap_manifest import canonical_bytes, digest
from api.display_bootstrap_observer_wire import frame, expect, validate_sample, validate
from api.display_bootstrap_growth_lease import _starttime


class ObserverSession:
    def __init__(self, sock, common, *, deadline, hold_observer, wait_go,
                 validate_storage, verify_capacity, verify_clock):
        self.sock = sock
        self.stack = ExitStack()
        self.closed = False
        self.pid, self.starttime = os.getpid(), _starttime()
        self.seq, self.previous = 0, None
        self.deadline = deadline
        self.validate_storage = validate_storage
        self.verify_capacity = verify_capacity
        self.verify_clock = verify_clock
        try:
            self.stream = CredentialStream(sock, deadline, strict=True)
            self.common = dict(common, version=3, nonce=secrets.token_hex(16))
            self.verify = self.stack.enter_context(hold_observer(self.stream.peer, common, deadline))
            self.check()
            self.send('HELLO')
            ready = self.stream.receive()
            validate(ready)
            self.common['server_nonce'] = ready.get('server_nonce')
            expect(ready, 'READY', self.common)
            self.check()
            operation = wait_go()  # Trusted control record, never an RPC declaration.
            self.common.update(operation)
            self.send('BEGIN')
            expect(self.stream.receive(), 'BOUND', self.common)
            self.check()
        except BaseException:
            self.abort()
            raise

    def __reduce__(self):
        raise TypeError('observer session cannot be serialized')

    def check(self):
        if (self.closed or os.getpid() != self.pid or _starttime() != self.starttime):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        self.stream.timeout()
        self.verify()
        self.verify_clock()
        self.stream.timeout()

    def send(self, kind, **extra):
        self.check()
        self.stream.send(frame(kind, self.common, **extra))
        self.check()

    def observe(self):
        try:
            self.check()
            if self.seq >= 1024:
                raise ValueError('RESOURCE_LIMIT')
            seq = self.seq + 1
            t0 = time.monotonic_ns()
            self.send('OBSERVE', seq=seq, previous_result_sha=self.previous)
            result = expect(self.stream.receive(), 'RESULT', self.common,
                            seq=seq, previous_result_sha=self.previous)
            self.check()
            sample = validate_sample(result['sample'], self.validate_storage)
            self.verify_capacity(sample['capacity'])
            result_sha = digest(canonical_bytes(result))
            self.send('ACK', seq=seq, result_sha=result_sha)
            expect(self.stream.receive(), 'COMMITTED', self.common, seq=seq, result_sha=result_sha)
            self.check()
            t1 = time.monotonic_ns()
            if not t0 <= sample['started_ns'] <= sample['finished_ns'] <= t1:
                raise ValueError('IDENTITY_CHANGED')
            if t1 - t0 >= 1_000_000_000 or t1 / 1e9 >= self.deadline:
                raise ValueError('RESOURCE_LIMIT')
            self.seq, self.previous = seq, result_sha
            return sample, t0
        except BaseException:
            self.abort()
            raise

    def close(self):
        if self.closed:
            return
        try:
            self.send('CLOSE', last_seq=self.seq, last_result_sha=self.previous)
            self.sock.shutdown(socket.SHUT_WR)
            expect(self.stream.receive(), 'CLOSED', self.common,
                   last_seq=self.seq, last_result_sha=self.previous)
            self.stream.eof()
            self.check()
        finally:
            self.abort()

    def abort(self):
        if not self.closed:
            self.closed = True
            try:
                self.sock.close()
            finally:
                self.stack.close()


def serve(sock, common, *, deadline, hold_worker, arm_and_wait_go,
          observe, validate_storage, verify_capacity, verify_clock):
    """Serve one launcher-bound connection until CLOSE. Owns and closes sock."""
    try:
        stream = CredentialStream(sock, deadline, strict=True)
        with hold_worker(stream.peer, common, deadline) as verify:
            def check():
                stream.timeout()
                verify()
                verify_clock()
                stream.timeout()
            def send(kind, **extra):
                check()
                stream.send(frame(kind, bound, **extra))
                check()
            check()
            hello = stream.receive()
            validate(hello)
            bound = dict(common, version=3, nonce=hello.get('nonce'))
            expect(hello, 'HELLO', bound)
            bound['server_nonce'] = secrets.token_hex(16)
            send('READY')
            operation = arm_and_wait_go()  # Verifies GO, then emits ARMED.
            bound.update(operation)
            expect(stream.receive(), 'BEGIN', bound)
            check()
            send('BOUND')
            seq, previous = 0, None
            while True:
                check()
                request = stream.receive()
                check()
                validate(request)
                if request['type'] == 'CLOSE':
                    expect(request, 'CLOSE', bound, last_seq=seq, last_result_sha=previous)
                    stream.eof()
                    check()
                    send('CLOSED', last_seq=seq, last_result_sha=previous)
                    sock.shutdown(socket.SHUT_WR)
                    return
                if seq >= 1024:
                    raise ValueError('RESOURCE_LIMIT')
                expect(request, 'OBSERVE', bound, seq=seq + 1, previous_result_sha=previous)
                check()
                sample = observe()
                validate_sample(sample, validate_storage)
                verify_capacity(sample['capacity'])
                check()
                result = frame('RESULT', bound, seq=seq + 1, previous_result_sha=previous,
                               sample=sample, sample_sha=digest(canonical_bytes(sample)))
                result_sha = digest(canonical_bytes(result))
                stream.send(result)
                check()
                expect(stream.receive(), 'ACK', bound, seq=seq + 1, result_sha=result_sha)
                check()
                send('COMMITTED', seq=seq + 1, result_sha=result_sha)
                seq, previous = seq + 1, result_sha
    finally:
        sock.close()
