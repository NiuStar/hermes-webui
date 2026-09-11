"""Preparation audit reader without BootstrapContext or append authority."""
import os
from contextlib import contextmanager
from api.display_bootstrap_audit_codec_v2 import decode_header, HEADER_BYTES
from api.display_bootstrap_audit_log import AuditFile, _deadline
from api.display_bootstrap_audit_budget import validate_layout
from api.display_bootstrap_extents import verify_allocated
from api.display_bootstrap_manifest import canonical_bytes, digest


@contextmanager
def scan_prepared_audit(*, directory_fd, candidate_id, deployment_sha, resource,
                        actor, deadline):
    _deadline(deadline)
    layout = validate_layout(resource)
    fd = os.open('audit.bin', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                 dir_fd=directory_fd)
    try:
        header = decode_header(os.pread(fd, HEADER_BYTES, 0))
        held = os.fstat(fd)
        if ({k:getattr(held,'st_'+k) for k in header['file_identity']} != header['file_identity']
                or header['candidate_id'] != candidate_id or header['deployment_sha'] != deployment_sha
                or header['resource_sha'] != digest(canonical_bytes(resource))
                or header['slot_count'] != layout['slot_count']):
            raise ValueError('APPROVAL_MISMATCH')
    finally:
        os.close(fd)
    with AuditFile(directory_fd, header, deadline=deadline, verify_allocation=verify_allocated,
                   writable=False, resource=resource) as log:
        log.actor = actor
        log.confirm_durable()
        yield log
