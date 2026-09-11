"""Root-owned append-only project allocation ledger; never frees identifiers.

Caller serializes operations with the broker lock. Torn or conflicting records
block allocation instead of silently advancing past damaged evidence.
"""
import os
import re
from api.display_bootstrap_manifest import canonical_bytes, parse_record
from api.display_bootstrap_policy import BootstrapRejected, read_protected_record


def _name(candidate_id):
    if type(candidate_id) is not str or re.fullmatch('[0-9a-f]{32}', candidate_id) is None:
        raise BootstrapRejected('INVALID_INPUT')
    return candidate_id + '.json'


def read(directory_fd, candidate_id):
    name = _name(candidate_id)
    try:
        raw = read_protected_record(directory_fd, name, {0})
    except BootstrapRejected as exc:
        if exc.code == 'POLICY_MISSING':
            return None
        raise
    value = parse_record(raw)
    required = {'version', 'candidate_id', 'policy_sha', 'directory_identity',
                'project_id', 'hard_bytes', 'hard_inodes', 'device'}
    if (type(value) is not dict or set(value) != required
            or type(value['version']) is not int or value['version'] != 1
            or value['candidate_id'] != candidate_id
            or type(value['project_id']) is not int
            or not 0 < value['project_id'] < 2**31):
        raise BootstrapRejected('STATE_CONFLICT')
    if (type(value['policy_sha']) is not str
            or re.fullmatch('[0-9a-f]{64}', value['policy_sha']) is None
            or type(value['hard_bytes']) is not int or not 0 < value['hard_bytes'] <= 9007199254740991
            or value['hard_bytes'] % 1024
            or type(value['hard_inodes']) is not int or not 0 < value['hard_inodes'] < 2**31
            or type(value['device']) is not int or value['device'] < 0):
        raise BootstrapRejected('STATE_CONFLICT')
    from api.display_bootstrap_manifest import validate_record
    try:
        validate_record(value['directory_identity'], 'directory_identity')
    except ValueError as exc:
        raise BootstrapRejected('STATE_CONFLICT') from exc
    if value['directory_identity']['dev'] != value['device']:
        raise BootstrapRejected('STATE_CONFLICT')
    return value


def validate_index(directory_fd, first_id, last_id):
    """Validate every retained binding before accepting even an existing ID."""
    if (type(first_id) is not int or type(last_id) is not int
            or not 0 < first_id <= last_id < 2**31):
        raise BootstrapRejected('INVALID_INPUT')
    used = set()
    records = {}
    for name in os.listdir(directory_fd):
        if re.fullmatch(r'[0-9a-f]{32}\.json', name) is None:
            raise BootstrapRejected('STATE_CONFLICT')
        item = read(directory_fd, name[:-5])
        if (item is None or item['project_id'] in used
                or not first_id <= item['project_id'] <= last_id):
            raise BootstrapRejected('STATE_CONFLICT')
        used.add(item['project_id'])
        records[item['candidate_id']] = item
    return records


def reserve(directory_fd, candidate_id, *, first_id, last_id, policy_sha,
            directory_identity, hard_bytes, hard_inodes, device, check_unused):
    if os.geteuid() != 0:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
    _name(candidate_id)
    records = validate_index(directory_fd, first_id, last_id)
    existing = records.get(candidate_id)
    binding = dict(version=1, candidate_id=candidate_id, policy_sha=policy_sha,
                   directory_identity=directory_identity, hard_bytes=hard_bytes,
                   hard_inodes=hard_inodes, device=device)
    if existing is not None:
        if any(existing[k] != v for k, v in binding.items()):
            raise BootstrapRejected('IDENTITY_CHANGED')
        return existing
    used = {item['project_id'] for item in records.values()}
    project_id = max(used, default=first_id-1) + 1
    if project_id > last_id or project_id < first_id:
        raise BootstrapRejected('RESOURCE_LIMIT')
    # Check before persisting intent: otherwise a collision could be mistaken
    # for our partially completed allocation on the next request.
    check_unused(project_id)
    value = dict(binding, project_id=project_id)
    raw = canonical_bytes(value)
    # Exclusive direct record: interruption leaves a rejected damaged record,
    # never an invisible reusable allocation. There is no automatic cleanup.
    fd = os.open(_name(candidate_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory_fd)
    try:
        remaining = memoryview(raw)
        while remaining:
            count = os.write(fd, remaining)
            if count <= 0:
                raise OSError('short ledger write')
            remaining = remaining[count:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(directory_fd)
    if read(directory_fd, candidate_id) != value:
        raise BootstrapRejected('STATE_CONFLICT')
    return value
