"""Bounded-growth schema regressions; no kernel mutation or storage exhaustion."""
import copy
import pytest
from api.display_bootstrap_manifest import canonical_bytes, validate_record
from api.display_bootstrap_storage_contract import (
    PROOF_SCOPE, EVIDENCE_KINDS, validate_acceptance,
)
from api.display_bootstrap_audit_codec_v2 import validate_payload


def registry():
    return dict(format_version=2, candidate_id='a'*32, target_name='a'*32,
                seq=1, previous_sha=None, state='RESERVED', manifest_sha=None,
                approval_id=None, error_code=None, request_sha='b'*64)


def report():
    return dict(format_version=2, report_id='a'*32, code_commit='b'*40,
                kernel_release='test-kernel', volume_profile_sha='c'*64,
                topology_sha='d'*64, proof_scope=copy.deepcopy(PROOF_SCOPE),
                checks={k: dict(status='PASS', evidence_sha='e'*64, evidence_kind=v)
                        for k, v in EVIDENCE_KINDS.items()})


def test_registry_v2_envelope():
    record = registry()
    actor = dict(role='creator', profile_id='c'*32, profile_sha='d'*64)
    raw = canonical_bytes(dict(format_version=2, actor=actor, record=record))
    assert validate_payload(1, raw, 'a'*32, 16)[0]['record'] == record
    assert len(raw) <= 685


@pytest.mark.parametrize('value', [True, 0, 17, '1'])
def test_registry_rejects_bad_sequence(value):
    record = registry()
    record['seq'] = value
    with pytest.raises(ValueError):
        validate_record(record, 'registry')


def test_registry_requires_request_sha():
    record = registry()
    del record['request_sha']
    with pytest.raises(ValueError):
        validate_record(record, 'registry')


def test_acceptance_new_scope():
    value = report()
    assert validate_acceptance(value, admission=True) is value


@pytest.mark.parametrize('mutation', ['host_journal', 'integer_bool', 'wrong_kind', 'unknown_check'])
def test_acceptance_rejects_proof_drift(mutation):
    value = report()
    if mutation == 'host_journal':
        value['proof_scope']['excluded_writers'].append('HOST_JOURNALD')
    elif mutation == 'integer_bool':
        value['proof_scope']['host_headroom_required'] = 1
    elif mutation == 'wrong_kind':
        value['checks']['bounded_growth']['evidence_kind'] = 'controlled_injection'
    else:
        value['checks']['host_blocks_full'] = dict(status='PASS', evidence_sha='f'*64)
    with pytest.raises(ValueError):
        validate_acceptance(value, admission=True)


def test_old_acceptance_never_admits():
    value = report()
    value['format_version'] = 1
    with pytest.raises(ValueError, match='ACCESS_BOUNDARY_UNPROVEN'):
        validate_acceptance(value, admission=True)
