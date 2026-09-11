"""One operation per process, offline only; no activation or live DB access.

Explicit IDs only. Acquire real context, never synthesize authority. A creator
exits with its irreversible write boundary; approval and publication run in
separate processes. Stdout is a bounded canonical result, not a connection.
"""
import argparse
import sys

from api.display_bootstrap_manifest import canonical_bytes
from api.display_bootstrap_policy import BootstrapRejected, acquire_bootstrap_context
from api.display_bootstrap_lifecycle import create_candidate, publish_candidate, recover_candidate


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline protected candidate lifecycle')
    parser.add_argument('--policy-id', required=True)
    commands = parser.add_subparsers(dest='operation', required=True)
    create = commands.add_parser('create')
    create.add_argument('--candidate-id', required=True)
    publish = commands.add_parser('publish')
    publish.add_argument('--candidate-id', required=True)
    publish.add_argument('--approval-id', required=True)
    recovery = commands.add_parser('recover')
    recovery.add_argument('--candidate-id', required=True)
    args = parser.parse_args(argv)
    import re
    for value in (args.policy_id, getattr(args, 'candidate_id', None),
                  getattr(args, 'approval_id', None)):
        if value is not None and re.fullmatch('[0-9a-f]{32}', value) is None:
            parser.error('identifiers must be exactly 32 lowercase hexadecimal characters')
    try:
        from api.display_bootstrap_admission import execute_operation
        result = execute_operation(args.policy_id,args.operation,
            candidate_id=getattr(args,'candidate_id',None),approval_id=getattr(args,'approval_id',None))
    except BootstrapRejected as exc:
        result = dict(format_version=1, status='BLOCKED', code=exc.code,
                      candidate_id=getattr(args, 'candidate_id', None),
                      target_name=getattr(args, 'candidate_id', None), registry_seq=None)
    from api.display_bootstrap_result import validate_result
    try:
        validate_result(result, operation=args.operation,
                        candidate_id=getattr(args, 'candidate_id', None))
    except ValueError:
        # Publication may already have moved data. Emit no fabricated lifecycle
        # result: the explicit candidate must be inspected by recovery.
        sys.stderr.write('Invalid lifecycle result; inspect retained candidate before retry.\n')
        return 2
    sys.stdout.buffer.write(canonical_bytes(result) + b'\n')
    sys.stdout.buffer.flush()
    return 0 if result['status'] in ('VERIFIED', 'PUBLISHED_UNACTIVATED') else 1


if __name__ == '__main__':
    raise SystemExit(main())
