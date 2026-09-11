"""Optional fixed policy suite, bound by trusted deployment, never request argv.

Uses only an existing application systemd service's descendant cleanup. This
module does not create units, quotas, cgroups, or acceptance receipts.
"""
import hashlib
import json
import os
from pathlib import Path

from api.application_artifact_files import _write_exclusive, read_record
from api.application_operation_issue import issue
from api.application_protocol import digest
from api.application_runner_registry import FixedTool, ToolRegistry
from api.application_runner_release import verify_release
from api.application_task_runner import (RunnerBlocked, bounded_run, cgroup_members,
                                         verify_service, verify_tool_shutdown)


def trusted_source_path(path):
    """Source authority is stricter than application-writable storage."""
    from api.application_storage_trust import trusted_path
    path = Path(path)
    for component in (*reversed(path.parents), path):
        trusted_path(component, directory=component != path)
        st = component.lstat()
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise RunnerBlocked('SOURCE_BINDING_AUTHORITY')


class FixedPolicyTool:
    def __init__(self, deployment, *, policy_sha, source_commit):
        self.deployment = deployment
        self.policy_sha = policy_sha
        self.source_commit = source_commit

    def resolve(self):
        data = self.deployment
        fields = {'release_root', 'release_sha', 'acceptance_path', 'acceptance_sha',
                  'timeout_ms', 'output_max_bytes', 'service', 'source_binding_path', 'source_binding_sha'}
        if type(data) is not dict or set(data) != fields:
            raise issue('INVALID_REQUEST', 'tool', 'policy_suite', 'fixed tool configuration schema mismatch')
        if (type(data['timeout_ms']) is not int or not 1 <= data['timeout_ms'] <= 60000
                or type(data['output_max_bytes']) is not int or not 1 <= data['output_max_bytes'] <= 16384):
            raise issue('INVALID_REQUEST', 'tool', 'policy_suite', 'invalid fixed tool limits')
        # verify_release validates root-owned, non-writable ancestors and all bytes.
        command = verify_release(data['release_root'], data['release_sha'])
        acceptance_path = Path(data['acceptance_path'])
        from api.application_storage_trust import trusted_path
        for path in (acceptance_path, *acceptance_path.parents):
            trusted_path(path, directory=path != acceptance_path)
            if path.lstat().st_uid != 0:
                raise RunnerBlocked('TOOL_ACCEPTANCE_AUTHORITY')
        binding_path = Path(data['source_binding_path'])
        for path in (binding_path, *binding_path.parents):
            trusted_path(path, directory=path != binding_path)
            if path.lstat().st_uid != 0:
                raise RunnerBlocked('SOURCE_BINDING_AUTHORITY')
        binding = read_record(binding_path)
        root = Path(__file__).resolve().parents[1]
        required = {str(p.relative_to(root)) for p in (root / 'api').glob('application_*.py')}
        required |= {'api/__init__.py', 'api/_display_schema_ddl.py'}
        if (digest(binding) != data['source_binding_sha']
                or binding.get('source_commit') != self.source_commit
                or len(self.source_commit) != 40
                or any(c not in '0123456789abcdef' for c in self.source_commit)
                or type(binding.get('files')) is not dict
                or set(binding['files']) != required):
            raise RunnerBlocked('SOURCE_BINDING_MISMATCH')
        for name, sha in binding['files'].items():
            path = root / name
            trusted_source_path(path)
            if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise RunnerBlocked('SOURCE_CONTENT_MISMATCH')
        receipt = read_record(acceptance_path)
        expected = {'format_version': 2, 'tool_id': 'policy_suite',
                    'release_sha': data['release_sha'], 'policy_sha': self.policy_sha,
                    'source_commit': self.source_commit, 'source_binding_sha': data['source_binding_sha'], 'timeout_ms': data['timeout_ms'],
                    'output_max_bytes': data['output_max_bytes'], 'service_sha': digest(data['service']),
                    'verdict': 'PASS'}
        if (set(receipt) != set(expected) | {'measurement_sha', 'review_sha'}
                or digest(receipt) != data['acceptance_sha']
                or any(receipt.get(k) != v or type(receipt.get(k)) is not type(v)
                       for k, v in expected.items())):
            raise RunnerBlocked('TOOL_ACCEPTANCE_BINDING')
        for key, kind, suffix in [('measurement_sha', 'measurement', '.measurement.json'),
                                  ('review_sha', 'independent_review', '.review.json')]:
            path = Path(str(acceptance_path) + suffix)
            trusted_path(path)
            if path.lstat().st_uid != 0:
                raise RunnerBlocked('TOOL_ACCEPTANCE_AUTHORITY')
            record = read_record(path)
            if (digest(record) != receipt[key] or record.get('kind') != kind
                    or any(record.get(k) != v or type(record.get(k)) is not type(v)
                           for k, v in expected.items())):
                raise RunnerBlocked('TOOL_ACCEPTANCE_EVIDENCE')
        return ToolRegistry({'policy_suite': FixedTool(command, data['timeout_ms'])}).resolve('policy_suite')

    def preflight(self):
        try:
            tool = self.resolve()
            if os.geteuid() == 0:
                raise RunnerBlocked('NONROOT_REQUIRED')
            identity = verify_service(self.deployment['service'])
            verify_tool_shutdown(self.deployment['service'])
            return tool, identity
        except (OSError, ValueError, KeyError, TypeError, RunnerBlocked) as exc:
            raise issue('DEPENDENCY_UNAVAILABLE', 'tool', 'policy_suite', str(exc)) from exc

    def run(self, task_id, files, record_intent):
        tool, identity = self.preflight()
        intent = {'tool_id': 'policy_suite', 'release_sha': self.deployment['release_sha'],
                  'acceptance_sha': self.deployment['acceptance_sha'],
                  'control_group': identity['ControlGroup'], 'invocation_id': identity['InvocationID']}
        record_intent(task_id, 'fixed_policy_tool', intent)
        # Resolve and check again immediately before exec; no DB connection is held.
        if self.resolve() != tool:
            raise issue('INTEGRITY_ERROR', 'tool', 'policy_suite', 'tool changed before execution')
        profile = self.deployment['service']
        captured = bytearray()
        failure = None
        result = None
        try:
            result = bounded_run(tool.argv, timeout_ms=tool.timeout_ms,
                                 output_max_bytes=self.deployment['output_max_bytes'],
                                 allowed_argv0={tool.argv[0]}, output_sink=captured.extend,
                                 term_grace_ms=profile['helper_term_grace_ms'],
                                 kill_wait_ms=profile['helper_kill_wait_ms'])
        except Exception as exc:
            failure = exc
        finally:
            try:
                verify_service(profile)
            except BaseException:
                # Only this verified application service exits. PID1 performs its
                # already-proven control-group cleanup; no host-wide kill/scan.
                os._exit(1)
        output_bytes = bytes(captured)
        record = {**intent, 'task_id': task_id, 'returncode': result.returncode if result else None,
                  'timed_out': result.timed_out if result else False, 'error': type(failure).__name__ if failure else None,
                  'output_hex': output_bytes.hex(), 'output_sha': hashlib.sha256(output_bytes).hexdigest()}
        sha = _write_exclusive(files.audits / (task_id + '.tool.json'), record)
        try:
            output = json.loads(output_bytes)
        except (UnicodeError, ValueError):
            output = None
        if (failure or result is None or result.timed_out or result.returncode != 0 or type(output) is not dict
                or output.get('tool_id') != 'policy_suite' or output.get('verdict') != 'PASS'):
            raise issue('DEPENDENCY_UNAVAILABLE', 'tool', 'policy_suite', 'fixed policy suite failed; inspect retained log')
        return {'tool_record_sha': sha, 'tool_release_sha': intent['release_sha']}


def require_tool_descendants_gone(ledger, task_id):
    """Inspect the original recorded group even if current tool config changed."""
    with ledger.session(readonly=True) as db:
        rows = db.execute("SELECT payload FROM operation_intents WHERE request_id=? AND step='fixed_policy_tool'", (task_id,)).fetchall()
    for row in rows:
        payload = json.loads(row[0])
        group = payload['control_group']
        if not group.startswith('/') or '..' in group.split('/'):
            raise issue('INTEGRITY_ERROR', 'task', task_id, 'invalid stored tool group')
        root = Path('/sys/fs/cgroup') / group.lstrip('/')
        try:
            if not root.exists():
                continue  # Kernel removes the group only after its processes exit.
            members = cgroup_members(root, max_entries=128, deadline_ms=1000)
            # A reused service path containing this coordinator is not proof
            # that the recorded invocation was drained. No persisted cleanup
            # authority is available here, so every live member fails closed.
            if members:
                raise RunnerBlocked('TOOL_DESCENDANTS_ALIVE')
        except (OSError, RunnerBlocked) as exc:
            raise issue('BUSY', 'task', task_id, 'original tool descendants are not proven gone') from exc


def tool_evidence(ledger, files, task_id):
    with ledger.session(readonly=True) as db:
        row = db.execute("SELECT payload FROM operation_intents WHERE request_id=? AND step='fixed_policy_tool'", (task_id,)).fetchone()
    if row is None:
        return {}
    expected = json.loads(row[0])
    try:
        record = read_record(files.audits / (task_id + '.tool.json'))
        output = bytes.fromhex(record['output_hex'])
        parsed = json.loads(output)
        if (record['task_id'] != task_id or record['returncode'] != 0 or record['timed_out'] is not False
                or any(record.get(k) != v for k, v in expected.items())
                or hashlib.sha256(output).hexdigest() != record['output_sha']
                or parsed.get('tool_id') != 'policy_suite' or parsed.get('verdict') != 'PASS'):
            raise ValueError('invalid tool completion')
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise issue('INTEGRITY_ERROR', 'tool', task_id, 'tool completion evidence is unavailable') from exc
    return {'tool_record_sha': digest(record), 'tool_release_sha': expected['release_sha']}
