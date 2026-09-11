"""Systemd-owned broker listener: restart without unlinking evidence.

Activation environment selects an FD, never authenticates it. Require root,
PID1 parent, exact system service identity, kernel socket properties and the
protected pathname. No bind, chmod, unlink or fallback occurs here.
"""
import os
import socket
import stat
import subprocess

from api.display_bootstrap_policy import BootstrapRejected

UNIT = 'hermes-bootstrap-quota.service'
PATH = '/etc/hermes-display-bootstrap/quota.sock'


def activated_listener(root_fd, creator_gid):
    listener = None
    try:
        if (os.geteuid() != 0 or os.getppid() != 1
                or os.environ.get('LISTEN_PID') != str(os.getpid())
                or os.environ.get('LISTEN_FDS') != '1'
                or os.environ.get('LISTEN_FDNAMES') != 'quota'):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        keys = ('Id', 'MainPID', 'User', 'ControlGroup', 'FragmentPath')
        result = subprocess.run(['/usr/bin/systemctl', '--system', 'show', UNIT,
            '--no-pager', *['--property='+key for key in keys]],
            capture_output=True, text=True, check=True, timeout=5,
            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        values = dict(line.split('=', 1) for line in result.stdout.splitlines())
        if (set(values) != set(keys) or values['Id'] != UNIT
                or values['MainPID'] != str(os.getpid()) or values['User'] != 'root'
                or values['ControlGroup'] != '/system.slice/'+UNIT
                or values['FragmentPath'] != '/etc/systemd/system/'+UNIT):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        # Take ownership of the inherited FD, not a duplicate that leaks on stop.
        os.set_inheritable(3, False)
        listener = socket.socket(fileno=3)
        if (listener.family != socket.AF_UNIX
                or listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_SEQPACKET
                or listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1
                or listener.getsockname() != PATH):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        named = os.stat('quota.sock', dir_fd=root_fd, follow_symlinks=False)
        if (not stat.S_ISSOCK(named.st_mode) or named.st_uid != 0
                or named.st_gid != creator_gid or stat.S_IMODE(named.st_mode) != 0o660
                or any(n.startswith('system.posix_acl_') for n in os.listxattr(PATH, follow_symlinks=False))):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        for key in ('LISTEN_PID', 'LISTEN_FDS', 'LISTEN_FDNAMES'):
            os.environ.pop(key, None)
        listener.settimeout(5)
        owned, listener = listener, None
        return owned, named
    except BootstrapRejected:
        raise
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN') from exc
    finally:
        if listener is not None:
            listener.close()
