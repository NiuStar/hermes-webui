"""Strict SCM_RIGHTS helpers for the fixed launcher/broker boundary."""
import array
import fcntl
import os
import socket
import stat


def receive_fds(connection, *, max_bytes, count):
    if (type(max_bytes) is not int or not 1 <= max_bytes <= 65536
            or type(count) is not int or not 0 <= count <= 32):
        raise ValueError('INVALID_INPUT')
    received = []
    try:
        raw, ancillary, flags, address = connection.recvmsg(
            max_bytes + 1, socket.CMSG_SPACE(33 * array.array('i').itemsize),
            socket.MSG_CMSG_CLOEXEC)
        invalid = False
        rights_messages = 0
        for level, kind, data in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                invalid = True
                continue
            rights_messages += 1
            items = array.array('i')
            if len(data) % items.itemsize:
                invalid = True
            items.frombytes(data[:len(data) - len(data) % items.itemsize])
            received.extend(items)
        if (invalid or rights_messages != (1 if count else 0)
                or flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC) or address
                or not 0 < len(raw) <= max_bytes or len(received) != count):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        for fd in received:
            if not fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC:
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        return raw, tuple(received)
    except BaseException:
        for fd in received:
            os.close(fd)
        raise


def verify_business_fds(fds, *, directory_identity, lock_identities):
    """Exactly one directory and authority/deployment locks, in that order.

    Caller authenticates the peer first and closes all received FDs on exit.
    Successful locking is not itself a growth lease or quota authorization.
    """
    if len(fds) != 3 or len(lock_identities) != 2:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    info = os.fstat(fds[0])
    actual = {k: getattr(info, 'st_' + k) for k in ('dev', 'ino', 'uid', 'gid', 'mode')}
    if not stat.S_ISDIR(info.st_mode) or actual != directory_identity:
        raise ValueError('IDENTITY_CHANGED')
    for fd, expected in zip(fds[1:], lock_identities):
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or (info.st_dev, info.st_ino) != expected):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('LOCK_BUSY') from exc
    # Deliberately no LOCK_UN: these are shared operation open descriptions.
