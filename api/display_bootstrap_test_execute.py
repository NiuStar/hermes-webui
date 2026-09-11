"""Bounded test child execution. The supervisor owns durable quota reservation."""
import os
import selectors
import subprocess
import time


def run(command, *, cwd, env, log_fd, max_log_bytes, deadline, verify_budget):
    """Foreground only; no arbitrary user command reaches this internal executor.

    PID1 is responsible for the entire fixed cgroup if this supervisor exits.
    Readback of that original cgroup is mandatory before another batch.
    """
    if (type(command) is not tuple or not command or any(type(v) is not str for v in command)
            or type(max_log_bytes) is not int or max_log_bytes < 1
            or os.fstat(log_fd).st_size != 0):
        raise ValueError('INVALID_INPUT')
    verify_budget()
    if time.monotonic() >= deadline:
        raise ValueError('RESOURCE_LIMIT')
    child = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             close_fds=True, start_new_session=False)
    written = 0
    try:
        with selectors.DefaultSelector() as selector:
            os.set_blocking(child.stdout.fileno(), False)
            selector.register(child.stdout, selectors.EVENT_READ)
            while selector.get_map():
                verify_budget()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('RESOURCE_LIMIT')
                for key, _ in selector.select(min(remaining, 0.1)):
                    raw = os.read(key.fd, min(65536, max_log_bytes-written+1))
                    if not raw:
                        selector.unregister(key.fileobj)
                        continue
                    if written + len(raw) > max_log_bytes:
                        raise ValueError('RESOURCE_LIMIT')
                    verify_budget()
                    offset = 0
                    while offset < len(raw):
                        if time.monotonic() >= deadline:
                            raise ValueError('RESOURCE_LIMIT')
                        count = os.write(log_fd, raw[offset:])
                        if count <= 0:
                            raise ValueError('IO_FAILURE')
                        offset += count
                    written += len(raw)
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise ValueError('RESOURCE_LIMIT')
            code = child.wait(timeout=remaining)
            verify_budget()
            os.fsync(log_fd)
            return dict(exit_code=code, log_bytes=written)
    finally:
        child.stdout.close()
        if child.poll() is None:
            child.kill()
            # This only reaps the direct child, NEVER certifies descendants gone.
            child.wait(timeout=5)
