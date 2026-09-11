"""Registry chain verification, separate from runtime recovery authorization."""
import os

import pytest


def record(seq=1, **changes):
    value = dict(format_version=1,candidate_id='a'*32,seq=seq,previous_sha=None,
                 state='RESERVED',target_name='a'*32,manifest_sha=None,
                 approval_id=None,error_code=None)
    value.update(changes)
    return value


@pytest.mark.parametrize('first_state,second_state', [('FAILED','BUILDING'), ('QUARANTINED','BUILDING'), ('RESERVED','PUBLISHED_UNACTIVATED')])
def test_registry_rejects_illegal_transitions(tmp_path, first_state, second_state):
    import api.display_bootstrap_publish as publisher
    from api.display_bootstrap_manifest import canonical_bytes, digest
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        first = record(state=first_state, error_code='STATE_CONFLICT' if first_state != 'RESERVED' else None)
        raw = canonical_bytes(first)
        publisher.write_record(fd, '00000000000000000001.json', raw)
        second = record(2, state=second_state, previous_sha=digest(raw))
        if second_state == 'PUBLISHED_UNACTIVATED':
            second.update(manifest_sha='b'*64, approval_id='c'*32)
        publisher.write_record(fd, '00000000000000000002.json', canonical_bytes(second))
        with pytest.raises(ValueError, match='STATE_CONFLICT'):
            publisher.read_registry(fd, 'a'*32)
    finally:
        os.close(fd)


def test_completed_then_quarantined(tmp_path):
    import api.display_bootstrap_publish as publisher
    from api.display_bootstrap_manifest import canonical_bytes, digest
    states = ['RESERVED', 'BUILDING', 'VERIFIED', 'APPROVED', 'PUBLISH_INTENT',
              'PUBLISHED_UNACTIVATED', 'QUARANTINED']
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    previous = None
    try:
        for seq, state in enumerate(states, 1):
            current = record(seq, state=state, previous_sha=previous,
                             manifest_sha='b'*64 if seq >= 3 else None,
                             approval_id='c'*32 if seq >= 4 else None,
                             error_code='STATE_CONFLICT' if state == 'QUARANTINED' else None)
            raw = canonical_bytes(current)
            publisher.write_record(fd, f'{seq:020d}.json', raw)
            previous = digest(raw)
        records = publisher.read_registry(fd, 'a'*32)
        assert records[-1]['state'] == 'QUARANTINED'
        assert records[-2]['state'] == 'PUBLISHED_UNACTIVATED'
        restored = record(8, state='PUBLISHED_UNACTIVATED', previous_sha=previous,
                          manifest_sha='b'*64, approval_id='c'*32)
        publisher.write_record(fd, '00000000000000000008.json', canonical_bytes(restored))
        with pytest.raises(ValueError, match='STATE_CONFLICT'):
            publisher.read_registry(fd, 'a'*32)
    finally:
        os.close(fd)


@pytest.mark.parametrize('name', ['1.json', '00000000000000000001.json.backup', 'unexpected'])
def test_registry_rejects_unknown_entries(tmp_path, name):
    import api.display_bootstrap_publish as publisher
    (tmp_path / name).write_bytes(b'{}')
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError, match='STATE_CONFLICT'):
            publisher.read_registry(fd, 'a'*32)
    finally:
        os.close(fd)


def test_registry_rejects_hardlinked_record(tmp_path):
    import api.display_bootstrap_publish as publisher
    from api.display_bootstrap_manifest import canonical_bytes
    source = tmp_path / 'original'
    source.write_bytes(canonical_bytes(record()))
    registry = tmp_path / 'registry'
    registry.mkdir()
    os.link(source, registry / '00000000000000000001.json')
    fd = os.open(registry, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError, match='IDENTITY_CHANGED'):
            publisher.read_registry(fd, 'a'*32)
    finally:
        os.close(fd)


def test_registry_chain(tmp_path):
    import api.display_bootstrap_publish as publisher
    from api.display_bootstrap_manifest import canonical_bytes, digest
    assert hasattr(publisher, 'read_registry'), 'registry chain reader missing'
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        first = canonical_bytes(record())
        publisher.write_record(fd, '00000000000000000001.json', first)
        second = record(2, previous_sha=digest(first), state='BUILDING')
        publisher.write_record(fd, '00000000000000000002.json', canonical_bytes(second))
        assert publisher.read_registry(fd, 'a'*32) == [record(), second]
        (tmp_path / '00000000000000000002.json').write_bytes(canonical_bytes(dict(second, previous_sha='b'*64)))
        with pytest.raises(ValueError, match='STATE_CONFLICT'):
            publisher.read_registry(fd, 'a'*32)
    finally:
        os.close(fd)
