"""Strict root-owned broker configuration. No environment configuration.

Parsing does not confer deployment admission: launcher must match active
policy, held roots, mount identity, peer profile and exclusive ID ownership.
"""
import os
import re
from api.display_bootstrap_manifest import parse_record, digest
from api.display_bootstrap_policy import BootstrapRejected, open_protected_root, read_protected_record

ROOT = '/etc/hermes-display-bootstrap'
FIELDS = {'version', 'policy_id', 'policy_sha', 'creator_uid', 'creator_gid',
          'hard_limit_profile_id', 'hard_bytes', 'hard_inodes',
          'first_project_id', 'last_project_id', 'device_path',
          'device_identity', 'ledger_path', 'ledger_identity', 'mount_id'}


def validate(value):
    if type(value) is not dict or set(value) != FIELDS:
        raise BootstrapRejected('INVALID_INPUT')
    for key, width in [('policy_id', 32), ('policy_sha', 64), ('hard_limit_profile_id', 32)]:
        if type(value[key]) is not str or re.fullmatch('[0-9a-f]{%d}' % width, value[key]) is None:
            raise BootstrapRejected('INVALID_INPUT')
    for key in ('version', 'creator_uid', 'creator_gid', 'hard_bytes', 'hard_inodes',
                'first_project_id', 'last_project_id', 'mount_id'):
        if type(value[key]) is not int or not 0 < value[key] <= 9007199254740991:
            raise BootstrapRejected('INVALID_INPUT')
    if (value['version'] != 1 or value['hard_bytes'] % 1024
            or not value['first_project_id'] <= value['last_project_id'] < 2**31
            or value['hard_inodes'] >= 2**31):
        raise BootstrapRejected('INVALID_INPUT')
    for key in ('device_path', 'ledger_path'):
        path = value[key]
        if (type(path) is not str or not path.startswith('/') or '\x00' in path
                or any(p in ('', '.', '..') for p in path.split('/')[1:])):
            raise BootstrapRejected('INVALID_INPUT')
    for key, fields in [('device_identity', {'dev', 'ino', 'uid', 'gid', 'mode', 'rdev'}),
                        ('ledger_identity', {'dev', 'ino', 'uid', 'gid', 'mode'})]:
        item = value[key]
        if (type(item) is not dict or set(item) != fields
                or any(type(n) is not int or n < 0 for n in item.values())
                or item['uid'] != 0 or item['mode'] & 0o022):
            raise BootstrapRejected('INVALID_INPUT')
    import stat
    if (not stat.S_ISBLK(value['device_identity']['mode'])
            or not stat.S_ISDIR(value['ledger_identity']['mode'])
            or value['ledger_identity']['mode'] & 0o077):
        raise BootstrapRejected('INVALID_INPUT')
    return value


def read():
    root = open_protected_root(ROOT, {0})
    try:
        raw = read_protected_record(root, 'quota-broker.json', {0})
        return validate(parse_record(raw)), digest(raw)
    except BootstrapRejected:
        raise
    except ValueError as exc:
        raise BootstrapRejected('INVALID_INPUT') from exc
    finally:
        os.close(root)
