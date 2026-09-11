"""Small, operation-scoped filesystem availability checks."""

import errno
import os

from api.application_operation_issue import issue


def require_writable(path, *, scope_id, minimum_bytes=4096):
    try:
        value = os.statvfs(path)
    except OSError as exc:
        raise from_os_error(exc, scope_id)
    if value.f_bavail * value.f_frsize < minimum_bytes or value.f_favail < 1:
        raise issue("SPACE_LOW", "resource", scope_id, "insufficient space for required write")


def from_os_error(exc, scope_id):
    if getattr(exc, "errno", None) in {errno.ENOSPC, errno.EDQUOT}:
        return issue("SPACE_LOW", "resource", scope_id, str(exc))
    if getattr(exc, "errno", None) in {errno.EBUSY, errno.EAGAIN, errno.ETIMEDOUT}:
        return issue("BUSY", "resource", scope_id, str(exc))
    return issue("INTERNAL_ERROR", "resource", scope_id, str(exc))