"""Per-resource OS locking and persisted executor identity."""

import fcntl
import json
import os
import stat
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from api.application_operation_issue import issue


def process_identity():
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    fields = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
    group = {
        "unit": os.environ.get("SYSTEMD_UNIT", "application-runtime-cli"),
        "invocation_id": os.environ.get("INVOCATION_ID", "local"),
        "control_group": next((line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines()
                               if line.startswith("0::")), "/"),
    }
    return {"owner_state": "ACTIVE", "boot_id": boot_id, "pid": os.getpid(),
            "start_ticks": int(fields[19]), "execution_group": json.dumps(group, sort_keys=True,
            separators=(",", ":"))}


def owner_alive(owner):
    if owner["owner_state"] != "ACTIVE":
        return False
    try:
        current_boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if current_boot != owner["boot_id"]:
            uuid.UUID(current_boot)
            uuid.UUID(owner["boot_id"])
            return False
        fields = Path(f"/proc/{owner['pid']}/stat").read_text().rsplit(")", 1)[1].split()
        return int(fields[19]) == owner["start_ticks"]
    except (FileNotFoundError, ProcessLookupError):
        return False
    except (ValueError, IndexError, OSError):
        return True  # Unknown identity never proves the writer exited.


class ResourceOwner:
    def __init__(self, lock_root):
        self.lock_root = Path(lock_root)

    @contextmanager
    def acquire(self, keys):
        normalized = sorted(set(keys), key=lambda value: value.encode("utf-8"))
        if any(not (key.startswith("candidate:") or key.startswith("approval:"))
               for key in normalized):
            raise issue("INVALID_REQUEST", message="invalid resource key")
        with ExitStack() as stack:
            for key in normalized:
                name = key.replace(":", "-") + ".lock"
                fd = os.open(self.lock_root / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                stack.callback(os.close, fd)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise issue("INTEGRITY_ERROR", "resource", key, "unsafe resource lock")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise issue("BUSY", "resource", key, "resource is active") from exc
            yield