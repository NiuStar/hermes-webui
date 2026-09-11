"""Approval has no calendar expiry; exact bindings still apply.

Synthetic records below are unit-test fixtures, never approval artifacts.
"""
import hashlib
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
from api import application_fixed_tool as mod
from api.application_protocol import digest
from api.application_task_runner import RunnerBlocked


@pytest.fixture
def candidate(monkeypatch):
    root = Path(mod.__file__).resolve().parents[1]
    names = {str(p.relative_to(root)) for p in (root / 'api').glob('application_*.py')}
    names |= {'api/__init__.py', 'api/_display_schema_ddl.py'}
    binding = {'source_commit': 'a' * 40, 'files': {
        n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in names}}
    config = dict(release_root='/unit/release', release_sha='b' * 64,
                  acceptance_path='/unit/approval', acceptance_sha='',
                  timeout_ms=1000, output_max_bytes=1024, service={'unit': 'unit-test'},
                  source_binding_path='/unit/source', source_binding_sha=digest(binding))
    receipt = dict(format_version=2, tool_id='policy_suite', release_sha=config['release_sha'],
                   policy_sha='c' * 64, source_commit=binding['source_commit'],
                   source_binding_sha=digest(binding), timeout_ms=1000, output_max_bytes=1024,
                   service_sha=digest(config['service']), verdict='PASS')
    records = {'/unit/source': binding}
    for key, kind, suffix in [('measurement_sha', 'measurement', '.measurement.json'),
                              ('review_sha', 'independent_review', '.review.json')]:
        record = {**receipt, 'kind': kind}
        records['/unit/approval' + suffix] = record
    receipt['measurement_sha'] = digest(records['/unit/approval.measurement.json'])
    receipt['review_sha'] = digest(records['/unit/approval.review.json'])
    records['/unit/approval'] = receipt
    config['acceptance_sha'] = digest(receipt)
    monkeypatch.setattr(mod, 'verify_release', lambda *a: ('/usr/bin/python3', '-V'))
    monkeypatch.setattr('api.application_storage_trust.trusted_path', lambda *a, **k: None)
    monkeypatch.setattr(Path, 'lstat', lambda *a, **k: SimpleNamespace(st_uid=0, st_mode=0o100644))
    monkeypatch.setattr(mod, 'read_record', lambda p: records[str(p)])
    return mod.FixedPolicyTool(config, policy_sha='c' * 64, source_commit='a' * 40), records


@pytest.mark.parametrize('clock', [0, 604800, 99999999999])
def test_resolve_without_expiry_at_any_clock(candidate, monkeypatch, clock):
    tool, _ = candidate
    monkeypatch.setattr(time, 'time', lambda: clock)
    assert tool.resolve().argv == ('/usr/bin/python3', '-V')


@pytest.mark.parametrize('field,value', [('policy_sha', 'd' * 64),
    ('release_sha', 'd' * 64), ('source_commit', 'd' * 40),
    ('service_sha', 'd' * 64), ('timeout_ms', 2), ('output_max_bytes', 2)])
def test_changed_approval_binding_rejected(candidate, field, value):
    tool, records = candidate
    records['/unit/approval'][field] = value
    tool.deployment['acceptance_sha'] = digest(records['/unit/approval'])
    with pytest.raises(RunnerBlocked, match='TOOL_ACCEPTANCE_BINDING'):
        tool.resolve()


def test_source_content_change_rejected(candidate):
    tool, records = candidate
    binding = records['/unit/source']
    binding['files']['api/__init__.py'] = '0' * 64
    tool.deployment['source_binding_sha'] = digest(binding)
    with pytest.raises(RunnerBlocked, match='SOURCE_CONTENT_MISMATCH'):
        tool.resolve()


def test_review_tamper_rejected(candidate):
    tool, records = candidate
    records['/unit/approval.review.json']['verdict'] = 'FAIL'
    with pytest.raises(RunnerBlocked, match='TOOL_ACCEPTANCE_EVIDENCE'):
        tool.resolve()
