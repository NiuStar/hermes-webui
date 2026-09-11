"""Bounded kernel state checks for a fixed test service's old cgroup."""
import os
import re
import stat


def require_stopped(state, cgroup_root_fd):
    """Caller obtains state via authenticated PID1; path comes from ControlGroup.

    A free file lock alone never establishes that old descendant writers exited.
    No cgroup is removed or killed here. Unknown/missing paths fail closed;
    an empty ControlGroup alone does not prove absence of a residual domain.
    """
    if type(state) is not dict or set(state) != {'ActiveState', 'SubState', 'ControlGroup', 'Job'}:
        raise ValueError('INVALID_INPUT')
    if (state['ActiveState'] not in ('inactive', 'failed')
            or state['SubState'] not in ('dead', 'failed') or state['Job'] != 0
            or type(state['Job']) is not int):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    path = state['ControlGroup']
    if type(path) is not str or re.fullmatch(r'/[A-Za-z0-9_.@:/-]+', path) is None:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    parts = path.split('/')[1:]
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    current = os.dup(cgroup_root_fd)
    try:
        for part in parts:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=current)
            os.close(current)
            current = next_fd
        events = os.open('cgroup.events', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=current)
        try:
            if not stat.S_ISREG(os.fstat(events).st_mode):
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
            raw = os.read(events, 4097)
            if len(raw) > 4096:
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
            rows = [line.split() for line in raw.decode('ascii').splitlines()]
            if any(len(row) != 2 for row in rows) or len({r[0] for r in rows}) != len(rows):
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
            if dict(rows).get('populated') != '0':
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        finally:
            os.close(events)
    except (OSError, UnicodeError) as exc:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN') from exc
    finally:
        os.close(current)
