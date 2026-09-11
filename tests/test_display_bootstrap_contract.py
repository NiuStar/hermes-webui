"""Offline bootstrap contracts; databases are retained by the test runner."""
import hashlib
import sqlite3

import pytest


def test_canonical_digest():
    import importlib.util
    assert importlib.util.find_spec('api.display_bootstrap_manifest'), 'strict metadata codec missing'
    from api.display_bootstrap_manifest import canonical_bytes, digest
    raw = b'{"a":1,"b":true}'
    assert canonical_bytes({'b': True, 'a': 1}) == raw
    assert digest(raw) == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize('value', [1.5, float('nan'), {'x': 1.5}, {1: 'x'},
                                        'x' * 4097, '\ud800', [0] * 257,
                                        9007199254740992, b'bytes'])
def test_canonical_rejects_invalid_values(value):
    from api.display_bootstrap_manifest import canonical_bytes
    with pytest.raises(ValueError):
        canonical_bytes(value)


@pytest.mark.parametrize('raw', [b'{"a":1,"a":2}', b'{ "a":1}',
    b'{"a":1.0}', b'{"a":NaN}', b'{"a":"\\ud800"}', b'[]', b'{} trailing',
    b'\xef\xbb\xbf{}', b'{"a":-1}', b'x' * 65537])
def test_parse_rejects_noncanonical_records(raw):
    import api.display_bootstrap_manifest as codec
    assert hasattr(codec, 'parse_record'), 'strict record parser missing'
    with pytest.raises(ValueError):
        codec.parse_record(raw)


def test_parse_canonical_record():
    import api.display_bootstrap_manifest as codec
    assert hasattr(codec, 'parse_record'), 'strict record parser missing'
    assert codec.parse_record(b'{"a":1}') == {'a': 1}


def test_approval_schema():
    import api.display_bootstrap_manifest as codec
    assert hasattr(codec, 'validate_record'), 'object schema validator missing'
    record = dict(format_version=1, approval_id='a'*32, candidate_id='b'*32,
                  manifest_sha='c'*64, target_name='b'*32, policy_sha='d'*64,
                  scope='PUBLISH_EMPTY_UNACTIVATED', approver_reference='test')
    assert codec.validate_record(record, 'approval') == record
    for key in record:
        bad = dict(record)
        del bad[key]
        with pytest.raises(ValueError):
            codec.validate_record(bad, 'approval')
    for key, value in [('format_version', True), ('extra', 1), ('approval_id', 'A'*32),
                       ('manifest_sha', 'x'*64), ('scope', 'ACTIVATE')]:
        with pytest.raises(ValueError):
            codec.validate_record(dict(record, **{key: value}), 'approval')


def test_identity_record_schema():
    import api.display_bootstrap_manifest as codec
    directory = dict(dev=1, ino=2, uid=999, gid=987, mode=16872)
    file_identity = dict(directory, mode=33184, nlink=1)
    assert codec.validate_record(directory, 'directory_identity') == directory
    assert codec.validate_record(file_identity, 'file_identity') == file_identity
    for kind, record in [('directory_identity', directory), ('file_identity', file_identity)]:
        for key in record:
            bad = dict(record, **{key: True})
            with pytest.raises(ValueError):
                codec.validate_record(bad, kind)
        with pytest.raises(ValueError):
            codec.validate_record(dict(record, extra=1), kind)
    for bad in [dict(directory, mode=33184), dict(directory, ino=0)]:
        with pytest.raises(ValueError):
            codec.validate_record(bad, 'directory_identity')
    for bad in [dict(file_identity, nlink=2), dict(file_identity, mode=16872)]:
        with pytest.raises(ValueError):
            codec.validate_record(bad, 'file_identity')


def test_resource_policy_schema():
    import api.display_bootstrap_manifest as codec
    record = dict(format_version=1, max_candidate_bytes=1048576, min_free_bytes=1048576,
                  max_retained_candidates=4, max_rss_bytes=67108864,
                  max_elapsed_seconds=10, check_interval_ms=100,
                  audit_reserve_bytes=1048576, hard_limit_profile_id='a'*32)
    assert codec.validate_record(record, 'resource') == record
    for key in record:
        bad = dict(record)
        del bad[key]
        with pytest.raises(ValueError):
            codec.validate_record(bad, 'resource')
    for key in set(record) - {'hard_limit_profile_id'}:
        for invalid in [True, 0, -1, 1.5]:
            with pytest.raises(ValueError):
                codec.validate_record(dict(record, **{key: invalid}), 'resource')
    with pytest.raises(ValueError):
        codec.validate_record(dict(record, check_interval_ms=10001), 'resource')


def test_registry_schema():
    import api.display_bootstrap_manifest as codec
    record = dict(format_version=1, candidate_id='a'*32, seq=1, previous_sha=None,
                  state='RESERVED', target_name='a'*32, manifest_sha=None,
                  approval_id=None, error_code=None)
    assert codec.validate_record(record, 'registry') == record
    for changes in [dict(seq=True), dict(state='SUCCESS'), dict(extra=1),
                    dict(seq=0), dict(candidate_id='x'*32)]:
        with pytest.raises(ValueError):
            codec.validate_record(dict(record, **changes), 'registry')


def test_registry_state_fields():
    import api.display_bootstrap_manifest as codec
    base = dict(format_version=1, candidate_id='a'*32, seq=1, previous_sha=None,
                state='RESERVED', target_name='a'*32, manifest_sha=None,
                approval_id=None, error_code=None)
    invalid = [dict(seq=2), dict(previous_sha='b'*64), dict(state='VERIFIED'),
               dict(state='APPROVED', manifest_sha='c'*64), dict(error_code='BAD'),
               dict(state='FAILED'), dict(approval_id='d'*32)]
    for changes in invalid:
        with pytest.raises(ValueError):
            codec.validate_record(dict(base, **changes), 'registry')


def test_manifest_schema():
    import api.display_bootstrap_manifest as codec
    record = dict(format_version=1, candidate_id='a'*32, ddl_sha='578f80789de98456324d430142631c7b7f984f80f3ffe30fc7c31d0634bdcd9d',
                  sqlite_version='3', platform_evidence_sha='c'*64, resource_policy_sha='d'*64,
                  db_bytes=1, db_sha='e'*64,
                  db_identity=dict(dev=1,ino=2,uid=999,gid=987,mode=33184,nlink=1),
                  candidate_directory_identity=dict(dev=1,ino=3,uid=999,gid=987,mode=16872),
                  connection_policy=dict(foreign_keys=1,synchronous=2),
                  verification=dict(integrity_check='ok',foreign_key_violations=0,empty_state=True),
                  creator_commit='f'*40)
    assert codec.validate_record(record, 'manifest') == record
    for key in record:
        bad = dict(record)
        del bad[key]
        with pytest.raises(ValueError):
            codec.validate_record(bad, 'manifest')
    for changes in [dict(ddl_sha='b'*64), dict(extra=1), dict(db_bytes=True), dict(creator_commit='abc'),
                    dict(connection_policy=dict(foreign_keys=True,synchronous=2)),
                    dict(verification=dict(integrity_check='ok',foreign_key_violations=False,empty_state=True))]:
        with pytest.raises(ValueError):
            codec.validate_record(dict(record, **changes), 'manifest')


def test_platform_evidence_schema():
    import api.display_bootstrap_manifest as codec
    record = dict(format_version=1, evidence_id='a'*32, kernel='Linux', sqlite_version='3',
                  compile_options=['A'], vfs='unix', filesystem='ext4', mount_id=1,
                  mount_options=['rw'], namespace_id='mnt:[1]', approved_policy_sha='b'*64)
    assert codec.validate_record(record, 'platform_evidence') == record
    for key in record:
        bad = dict(record)
        del bad[key]
        with pytest.raises(ValueError):
            codec.validate_record(bad, 'platform_evidence')
    for changes in [dict(mount_id=True), dict(compile_options=[1]), dict(vfs=1),
                    dict(extra=1), dict(format_version=2)]:
        with pytest.raises(ValueError):
            codec.validate_record(dict(record, **changes), 'platform_evidence')


def test_active_anchor_schemas():
    import api.display_bootstrap_manifest as codec
    active = dict(format_version=1, policy_id='a'*32, deployment_policy_sha='b'*64)
    anchor = dict(format_version=1, lock_path='/etc/hermes-display-bootstrap/bootstrap.lock',
                  lock_identity=dict(dev=1,ino=2,uid=0,gid=987,mode=33184,nlink=1))
    for kind, record in [('active', active), ('anchor', anchor)]:
        assert codec.validate_record(record, kind) == record
        for key in record:
            bad = dict(record)
            del bad[key]
            with pytest.raises(ValueError):
                codec.validate_record(bad, kind)
        with pytest.raises(ValueError):
            codec.validate_record(dict(record, format_version=True), kind)
    with pytest.raises(ValueError):
        codec.validate_record(dict(anchor, lock_path='/tmp/lock'), 'anchor')


def test_deployment_policy_schema():
    import api.display_bootstrap_manifest as codec
    identity = dict(dev=1, ino=2, uid=0, gid=0, mode=16877)
    roots = {k: dict(path='/opt/' + k, identity=identity) for k in
             ('registry_root', 'approval_root', 'candidate_root', 'publish_root', 'lock_root')}
    record = dict(format_version=1, policy_id='a'*32, creator_uid=999, approver_uid=0,
                  roots=roots, ancestors=[dict(path='/', identity=identity), dict(path='/opt', identity=identity)],
                  lock_identity=dict(dev=1,ino=3,uid=0,gid=987,mode=33184,nlink=1),
                  platform_requirements=dict(kernel='Linux',sqlite_version='3',compile_options=['A'],
                      vfs='unix',filesystem='ext4',mount_id=1,mount_options=['rw'],namespace_id='mnt:[1]'),
                  resource_policy_id='b'*32,resource_policy_sha='c'*64,
                  expected_ddl_sha='578f80789de98456324d430142631c7b7f984f80f3ffe30fc7c31d0634bdcd9d')
    assert codec.validate_record(record, 'deployment') == record
    for key in record:
        bad = dict(record)
        del bad[key]
        with pytest.raises(ValueError):
            codec.validate_record(bad, 'deployment')
    for changes in [dict(creator_uid=0), dict(creator_uid=True), dict(expected_ddl_sha='d'*64),
                    dict(extra=1), dict(ancestors=[])]:
        with pytest.raises(ValueError):
            codec.validate_record(dict(record, **changes), 'deployment')


def test_legacy_initializer_no_access():
    from api.display_schema import initialize

    class Untouchable:
        def __getattribute__(self, name):
            raise AssertionError(f'database accessed: {name}')

    with pytest.raises(ValueError, match='LEGACY_INITIALIZER_DISABLED'):
        initialize(Untouchable())


def test_legacy_initializer_preserves_unknown_database(tmp_path):
    from api.display_schema import initialize

    path = tmp_path / 'unknown.sqlite'
    db = sqlite3.connect(path)
    try:
        db.execute('CREATE TABLE important(value TEXT)')
        db.execute("INSERT INTO important VALUES ('keep')")
        db.commit()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        mode = db.execute('PRAGMA journal_mode').fetchone()
        statements = []
        db.set_trace_callback(statements.append)
        with pytest.raises(ValueError, match='LEGACY_INITIALIZER_DISABLED'):
            initialize(db)
        assert statements == []
        db.set_trace_callback(None)
        assert db.execute('PRAGMA journal_mode').fetchone() == mode
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before
        assert db.execute('SELECT * FROM important').fetchall() == [('keep',)]
    finally:
        db.close()
