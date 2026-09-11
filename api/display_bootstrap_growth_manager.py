"""Lock-owner budget refresh and per-growth lease checks. No implicit admission."""
import os
from api.display_bootstrap_growth_lease import issue, verify, revoke, _starttime
from api.display_bootstrap_growth_math import commitments, require_headroom
from api.display_bootstrap_audit_log import _deadline


class GrowthManager:
    """Owned by trusted preparation/receipt, never constructed from RPC fields.

    inventory verifies all permanent records and static roots under the same
    outer locks. The launcher must supply this live implementation, not cached
    inventory or a request callback. No defaults deliberately exist.
    """
    def __init__(self, *, resource, session, inventory, verify_locks, placements,
                 bindings, candidate_id, operation, request_sha, deadline):
        self.resource, self.session = resource, session
        self.inventory, self.verify_locks = inventory, verify_locks
        self.placements, self.bindings = placements, bindings
        self.candidate_id, self.operation, self.request_sha = candidate_id, operation, request_sha
        self.deadline = deadline
        self.pid, self.starttime = os.getpid(), _starttime()
        self.lease = None
        self.closed = False
        self.verify_owner()

    def __reduce__(self):
        raise TypeError('growth manager cannot be serialized')

    def verify_owner(self):
        if self.closed or (self.pid, self.starttime) != (os.getpid(), _starttime()):
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        _deadline(self.deadline)
        self.verify_locks()
        _deadline(self.deadline)

    def refresh(self):
        revoke(self.lease)
        self.lease = None
        try:
            self.verify_owner()
            sample, started = self.session.observe()
            self.verify_owner()
            inventory = self.inventory()
            ids = inventory['candidate_ids']
            if self.operation != 'create' and self.candidate_id not in ids:
                raise ValueError('STATE_CONFLICT')
            count = len(ids | {self.candidate_id})
            committed = commitments(self.resource, count, self.placements)
            if set(inventory['filesystems']) != set(committed):
                raise ValueError('IDENTITY_CHANGED')
            minima = {}
            observed = {}
            for role, row in sample['capacity'].items():
                key = (row['fs_uuid'], row['device']['major'], row['device']['minor'])
                free = dict(bytes=row['free_bytes'], inodes=row['free_inodes'])
                if key in observed and observed[key] != free:
                    raise ValueError('IDENTITY_CHANGED')
                observed[key] = free
                limits = {axis: self.resource['growth'][role+'_min_free_'+axis]
                          for axis in ('bytes', 'inodes')}
                old = minima.setdefault(key, limits)
                minima[key] = {axis: max(old[axis], limits[axis]) for axis in old}
            if set(observed) != set(committed):
                raise ValueError('IDENTITY_CHANGED')
            for key, total in committed.items():
                require_headroom(total, inventory['filesystems'][key], observed[key], minima[key])
            self.verify_owner()
            self.lease = issue(owner=self, candidate_id=self.candidate_id,
                operation=self.operation, request_sha=self.request_sha, bindings=self.bindings,
                observation=sample, deadline=self.deadline, verify_owner=self.verify_owner,
                request_started_ns=started)
            return self.lease
        except BaseException:
            self.close()
            raise

    def before_growth(self):
        self.verify_owner()
        if self.lease is None:
            self.refresh()
        try:
            return verify(self.lease, owner=self, candidate_id=self.candidate_id,
                          operation=self.operation, request_sha=self.request_sha, bindings=self.bindings)
        except ValueError as exc:
            if str(exc) != 'RESOURCE_LIMIT':
                self.close()
                raise
            self.refresh()
            return verify(self.lease, owner=self, candidate_id=self.candidate_id,
                          operation=self.operation, request_sha=self.request_sha, bindings=self.bindings)

    def close(self):
        revoke(self.lease)
        self.lease = None
        self.closed = True
        self.session.abort()
