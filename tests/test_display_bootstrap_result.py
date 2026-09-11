"""Result contract tests; no lifecycle authority or deployment is created."""
import pytest
from api.display_bootstrap_result import validate_result


def verified():
    return dict(format_version=1, status='VERIFIED', code='OK', candidate_id='a'*32,
                target_name='a'*32, registry_seq=3, manifest_sha='b'*64)


def test_verified_result():
    value = verified()
    assert validate_result(value, operation='create') is value


@pytest.mark.parametrize('field,value', [
    ('format_version', True), ('registry_seq', True), ('registry_seq', None),
    ('candidate_id', None), ('target_name', 'c'*32), ('manifest_sha', 'B'*64),
    ('code', 'IO_FAILURE'), ('status', []), ('extra', 1),
])
def test_invalid_success(field, value):
    result = verified()
    result[field] = value
    with pytest.raises(ValueError):
        validate_result(result, operation='create')


def test_success_must_match_operation():
    with pytest.raises(ValueError):
        validate_result(verified(), operation='publish', candidate_id='a'*32)


def test_requested_candidate_binding():
    with pytest.raises(ValueError):
        validate_result(verified(), operation='create', candidate_id='c'*32)


def test_publication_requires_completion_hash():
    result = verified()
    result['status'] = 'PUBLISHED_UNACTIVATED'
    with pytest.raises(ValueError):
        validate_result(result, operation='publish', candidate_id='a'*32)
    result['completion_record_sha'] = 'd'*64
    assert validate_result(result, operation='recover', candidate_id='a'*32) == result


def test_uncertain_is_not_creation_result():
    result = dict(format_version=1, status='UNCERTAIN', code='IO_FAILURE',
                  candidate_id='a'*32, target_name='a'*32, registry_seq=None)
    assert validate_result(result, operation='recover') == result
    with pytest.raises(ValueError):
        validate_result(result, operation='create')


def test_persisted_quarantine_requires_sequence():
    result = dict(format_version=1, status='QUARANTINED', code='IDENTITY_CHANGED',
                  candidate_id='a'*32, target_name='a'*32, registry_seq=None,
                  quarantine_persisted=True)
    with pytest.raises(ValueError):
        validate_result(result, operation='recover')
    result['quarantine_persisted'] = False
    assert validate_result(result, operation='recover') == result
