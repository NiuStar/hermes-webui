"""Finite helpers for a fixed systemd application service.

bounded_run captures output; the service owner must additionally prove cgroup
membership before accepting another task. Process-group cleanup alone is not
claimed to contain all descendants.
"""
import os
import re
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


class RunnerBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class RunResult:
    returncode: int
    output: bytes
    timed_out: bool


def parse_systemd_show(raw):
    try:
        pairs = [line.split('=', 1) for line in raw.decode('utf-8').splitlines()]
        value = dict(pairs)
    except (ValueError, UnicodeError) as exc:
        raise RunnerBlocked('SYSTEMD_RESPONSE_INVALID') from exc
    if len(value) != len(pairs) or set(value) != {'MainPID','ControlGroup','InvocationID','ActiveState','SubState'}:
        raise RunnerBlocked('SYSTEMD_PROPERTIES_MISSING')
    if not re.fullmatch('[1-9][0-9]*', value['MainPID']):
        raise RunnerBlocked('MAIN_PID_INVALID')
    if not re.fullmatch('[0-9a-f]{32}', value['InvocationID']):
        raise RunnerBlocked('INVOCATION_INVALID')
    if not value['ControlGroup'].startswith('/') or '..' in value['ControlGroup'].split('/'):
        raise RunnerBlocked('CGROUP_INVALID')
    return value


def bounded_run(argv, *, timeout_ms, output_max_bytes, allowed_argv0,
                term_grace_ms=200, kill_wait_ms=1000, monitor=None, output_sink=None):
    if not argv or argv[0] not in allowed_argv0 or not os.path.isabs(argv[0]):
        raise RunnerBlocked('EXECUTABLE_NOT_ALLOWED')
    if any(type(arg) is not str or '\x00' in arg for arg in argv):
        raise RunnerBlocked('ARGV_INVALID')
    if type(timeout_ms) is not int or timeout_ms <= 0 or type(output_max_bytes) is not int or output_max_bytes < 0:
        raise RunnerBlocked('LIMIT_INVALID')
    if any(type(v) is not int or v <= 0 for v in (term_grace_ms, kill_wait_ms)):
        raise RunnerBlocked('STOP_LIMIT_INVALID')
    if monitor is not None:
        monitor()
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    deadline = time.monotonic() + timeout_ms / 1000
    output = bytearray()
    output_size = 0
    timed_out = False
    selector = selectors.DefaultSelector()
    try:
        os.set_blocking(proc.stdout.fileno(), False)
        selector.register(proc.stdout, selectors.EVENT_READ)
        while selector.get_map():
            if monitor is not None:
                monitor()
            left = deadline - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            for key, _ in selector.select(min(left, 0.05)):
                chunk = os.read(key.fd, min(65536, output_max_bytes - output_size + 1))
                if not chunk:
                    selector.unregister(key.fileobj)
                elif output_size + len(chunk) > output_max_bytes:
                    raise RunnerBlocked('OUTPUT_LIMIT')
                else:
                    output_size += len(chunk)
                    if output_sink is None:
                        output.extend(chunk)
                    else:
                        output_sink(chunk)
        while not timed_out and proc.poll() is None:
            if monitor is not None:
                monitor()
            left = deadline - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            try:
                proc.wait(timeout=min(0.05, left))
            except subprocess.TimeoutExpired:
                pass
    finally:
        selector.close()
        if proc.poll() is None or timed_out:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=term_grace_ms / 1000)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=kill_wait_ms / 1000)
        proc.stdout.close()
    if monitor is not None:
        monitor()
    return RunResult(proc.returncode if not timed_out else -signal.SIGTERM, bytes(output), timed_out)


def cgroup_members(root, *, max_entries, deadline_ms):
    deadline = time.monotonic() + deadline_ms / 1000
    pending = [Path(root)]
    members = set()
    count = 0
    while pending:
        directory = pending.pop()
        count += 1
        if count > max_entries or time.monotonic() >= deadline:
            raise RunnerBlocked('CGROUP_SCAN_LIMIT')
        if directory.is_symlink():
            raise RunnerBlocked('CGROUP_LINK')
        try:
            raw = (directory / 'cgroup.procs').read_text()
            for item in raw.splitlines():
                if not item.isascii() or not item.isdecimal() or int(item) < 1:
                    raise RunnerBlocked('CGROUP_PID_INVALID')
                members.add(int(item))
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        raise RunnerBlocked('CGROUP_LINK')
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
        except OSError as exc:
            raise RunnerBlocked('CGROUP_UNREADABLE') from exc
    return members


def verify_tool_shutdown(profile):
    result = bounded_run([profile['systemctl_path'], 'show', profile['service_unit'],
                          '--property=KillMode,SendSIGKILL,TimeoutStopUSec,StandardOutput,StandardError,LimitCORE'],
                         timeout_ms=profile['show_timeout_ms'], output_max_bytes=4096,
                         allowed_argv0={profile['systemctl_path']},
                         term_grace_ms=profile['helper_term_grace_ms'],
                         kill_wait_ms=profile['helper_kill_wait_ms'])
    if result.returncode or result.timed_out:
        raise RunnerBlocked('SHUTDOWN_UNPROVEN')
    pairs = [line.split('=', 1) for line in result.output.decode().splitlines()]
    props = dict(pairs)
    if len(pairs) != len(props) or set(props) != {'KillMode','SendSIGKILL','TimeoutStopUSec','StandardOutput','StandardError','LimitCORE'}:
        raise RunnerBlocked('SHUTDOWN_UNPROVEN')
    if props['StandardOutput'] != 'null' or props['StandardError'] != 'null' or props['LimitCORE'] != '0':
        raise RunnerBlocked('OUTPUT_BOUNDARY_UNPROVEN')
    # Only simple explicit millisecond/second forms are supported; unknown
    # systemd duration formatting fails closed, never defaults to infinity.
    match = re.fullmatch(r'([0-9]+)(ms|s)', props['TimeoutStopUSec'])
    if (props['KillMode'] != 'control-group' or props['SendSIGKILL'] != 'yes' or not match or int(match[1]) <= 0
            or int(match[1]) * (1 if match[2] == 'ms' else 1000) > profile['stop_timeout_ms']):
        raise RunnerBlocked('SHUTDOWN_UNPROVEN')
    return props


def verify_service(profile):
    pid = os.getpid()
    result = bounded_run([profile['systemctl_path'], 'show', profile['service_unit'],
                          '--property=MainPID,ControlGroup,InvocationID,ActiveState,SubState'],
                         timeout_ms=profile['show_timeout_ms'],
                         output_max_bytes=profile['output_max_bytes'],
                         allowed_argv0={profile['systemctl_path']},
                         term_grace_ms=profile['helper_term_grace_ms'],
                         kill_wait_ms=profile['helper_kill_wait_ms'])
    if result.timed_out or result.returncode:
        raise RunnerBlocked('SYSTEMD_QUERY_FAILED')
    props = parse_systemd_show(result.output)
    own = [line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::')]
    if (own != [props['ControlGroup']] or int(props['MainPID']) != pid
            or props['InvocationID'] != os.environ.get('INVOCATION_ID')
            or (props['ActiveState'], props['SubState']) != ('active', 'running')):
        raise RunnerBlocked('SERVICE_IDENTITY_MISMATCH')
    root = Path('/sys/fs/cgroup') / props['ControlGroup'].lstrip('/')
    start = time.monotonic()
    for sample in range(2):
        if sample:
            time.sleep(profile['membership_gap_ms'] / 1000)
        remaining = profile['membership_total_deadline_ms'] - int((time.monotonic() - start) * 1000)
        if remaining <= 0 or cgroup_members(root, max_entries=profile['max_cgroup_entries'], deadline_ms=remaining) != {pid}:
            raise RunnerBlocked('DESCENDANTS_UNPROVEN')
    return props
