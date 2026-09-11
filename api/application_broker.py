"""Same-host CLI broker. OS peer credentials, not JSON, select the principal.

The socket's parent is owned by the service (0711 permits traversal only),
socket access uses its inherited OS group (0660). No user/group/unit creation.
The configured client pins the broker UID; this is not a network RPC service.
"""
import json
import os
import pwd
import socket
import socketserver
import stat
import struct
from pathlib import Path

from api.application_commands import dispatch
from api.application_operation_issue import ApplicationIssue, envelope, issue
from api.application_protocol import canonical, _unique
from api.application_task_service import ApplicationService

MAX_COMMAND = 20000
MAX_RESPONSE = 2 * 1024 * 1024


def _read(stream, maximum):
    raw = stream.readline(maximum + 1)
    if not raw.endswith(b'\n') or len(raw) > maximum:
        raise issue('INVALID_REQUEST', message='invalid broker frame')
    try:
        return json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, UnicodeError) as exc:
        raise issue('INVALID_REQUEST', message='invalid broker JSON') from exc


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(90)
        try:
            _, uid, _ = struct.unpack('3i', self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid == 0:
                raise issue('PERMISSION_DENIED', message='root CLI peer is not allowed')
            principal = self.server.config.principal(pwd.getpwuid(uid).pw_name)
            command = _read(self.rfile, MAX_COMMAND)
            response = dispatch(ApplicationService(self.server.config), principal, command)
        except ApplicationIssue as exc:
            response = envelope(None, problem=exc)
        except Exception:
            response = envelope(None, problem=issue('INTERNAL_ERROR', message='broker request failed'))
        try:
            self.wfile.write(canonical(response) + b'\n')
        except (BrokenPipeError, ConnectionError, TimeoutError):
            pass  # A disconnected client must query its task ID, never infer rollback.


class ApplicationBroker(socketserver.ThreadingUnixStreamServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, config, path):
        if os.geteuid() == 0:
            raise issue('PERMISSION_DENIED', message='broker requires non-root execution')
        self.config = config
        self.path = Path(path)
        from api.application_storage_trust import trusted_path
        trusted_path(self.path.parent, directory=True)
        if os.path.lexists(self.path):
            raise issue('CONFLICT', message='socket already exists; no automatic unlink')
        super().__init__(str(self.path), _Handler, bind_and_activate=False)
        old_umask = os.umask(0o077)
        try:
            self.server_bind()
        finally:
            os.umask(old_umask)
        os.chmod(self.path, 0o660)
        info = self.path.lstat()
        self.identity = (info.st_dev, info.st_ino)
        self.server_activate()

    def server_close(self):
        super().server_close()
        info = self.path.lstat() if os.path.lexists(self.path) else None
        if info and (info.st_dev, info.st_ino) == getattr(self, "identity", None) and stat.S_ISSOCK(info.st_mode):
            self.path.unlink()


def client(path, expected_uid, command):
    """A failed response may follow a committed write; preserve OUTCOME_UNKNOWN."""
    from api.application_storage_trust import trusted_path
    path = Path(path)
    # Shared client access is read/traverse only; socket is the sole writable object.
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, expected_uid}
                or info.st_mode & 0o022):
            raise issue('PERMISSION_DENIED', message='untrusted broker socket parent')
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != expected_uid or info.st_mode & 0o007:
        raise issue('PERMISSION_DENIED', message='untrusted broker socket')
    raw = canonical(command) + b'\n'
    if len(raw) > MAX_COMMAND:
        raise issue('INVALID_REQUEST', message='command too large')
    submitted = False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(90)
            connection.connect(str(path))
            _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != expected_uid:
                raise issue('PERMISSION_DENIED', message='broker UID mismatch')
            submitted = True
            connection.sendall(raw)
            with connection.makefile('rb') as stream:
                try:
                    result = _read(stream, MAX_RESPONSE)
                    if type(result) is not dict:
                        raise issue('INVALID_REQUEST', message='invalid broker response')
                    return result
                except ApplicationIssue as exc:
                    code = 'OUTCOME_UNKNOWN' if command['action'] in {'submit', 'init'} else 'DEPENDENCY_UNAVAILABLE'
                    raise issue(code, message='broker response invalid; inspect task before retry') from exc
    except OSError as exc:
        code = 'OUTCOME_UNKNOWN' if submitted and command['action'] in {'submit', 'init'} else 'DEPENDENCY_UNAVAILABLE'
        raise issue(code, message='broker connection failed; inspect task before retry') from exc
