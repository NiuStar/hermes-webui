"""Pure, checked per-filesystem commitment arithmetic (not admission authority)."""
from api.display_bootstrap_v4 import checked, round_block
from api.display_bootstrap_manifest import validate_record


def commitments(resource, count, filesystems):
    """Compute the approved conservative R1 budget for authenticated FS roles.

    filesystems is the observer's deduplicated mapping, never caller evidence.
    Each entry has candidate/registry/managed boolean placement flags.
    This function neither observes capacity nor authorizes a write.
    """
    validate_record(resource, 'resource')
    if resource['format_version'] != 4:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    checked(count)
    if count > resource['max_retained_candidates']:
        raise ValueError('RESOURCE_LIMIT')
    if type(filesystems) is not dict or not 1 <= len(filesystems) <= 2:
        raise ValueError('UNSUPPORTED_PLATFORM')
    g = resource['growth']
    result = {}
    for key, flags in filesystems.items():
        if (type(flags) is not dict or set(flags) != {'candidate', 'registry', 'managed'}
                or any(type(v) is not bool for v in flags.values()) or not flags['managed']):
            raise ValueError('INVALID_INPUT')
        peak = checked((round_block(resource['max_candidate_bytes']) if flags['candidate'] else 0)
                       + (resource['audit_reserve_bytes'] if flags['registry'] else 0)
                       + g['candidate_metadata_bytes'])
        inodes = checked((g['candidate_peak_inodes'] if flags['candidate'] else 0)
                         + (2 if flags['registry'] else 0) + g['candidate_metadata_inodes'])
        result[key] = {'bytes': checked(g['fixed_bytes'] + checked(count * peak)),
                       'inodes': checked(g['fixed_inodes'] + checked(count * inodes))}
    for axis, limit in [('bytes', 'total_bytes'), ('inodes', 'total_inodes')]:
        total = 0
        for row in result.values():
            total = checked(total + row[axis])
        if total >= g[limit]:
            raise ValueError('RESOURCE_LIMIT')
    return result


def require_headroom(committed, used, free, minimum):
    """Strict early rejection; all inputs must be freshly authenticated upstream."""
    for row in (committed, used, free, minimum):
        if type(row) is not dict or set(row) != {'bytes', 'inodes'}:
            raise ValueError('INVALID_INPUT')
        for value in row.values():
            checked(value)
    for axis in ('bytes', 'inodes'):
        if used[axis] > committed[axis]:
            raise ValueError('RESOURCE_LIMIT')
        debt = committed[axis] - used[axis]
        if free[axis] <= debt or free[axis] - debt <= minimum[axis]:
            raise ValueError('RESOURCE_LIMIT')
