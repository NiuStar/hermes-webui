"""V2 audit envelope roundtrip and old media rejection."""
import stat
import pytest
from api import display_bootstrap_audit_codec_v2 as codec
from api.display_bootstrap_manifest import canonical_bytes


def header():
    return dict(format_version=2,candidate_id='a'*32,
        file_identity=dict(dev=1,ino=1,uid=1001,gid=1001,mode=stat.S_IFREG|0o600,nlink=1),
        deployment_sha='b'*64,resource_sha='c'*64,slot_count=9,
        slot_bytes=codec.SLOT_BYTES,payload_bytes=codec.PAYLOAD_BYTES)


def payload(role='creator'):
    return canonical_bytes(dict(format_version=2,
        actor=dict(role=role,profile_id='d'*32,profile_sha='e'*64),
        record=dict(format_version=1,candidate_id='a'*32,target_name='a'*32,
            seq=1,previous_sha=None,state='RESERVED',manifest_sha=None,
            approval_id=None,error_code=None)))


def test_roundtrip_keeps_envelope_and_record_separate():
    value=header()
    encoded=codec.encode_header(value)
    assert codec.decode_header(encoded)==value
    sha=encoded[4064:].hex()
    raw=payload()
    item=codec.decode_slot(value,sha,0,codec.encode_slot(value,sha,0,1,raw))
    assert item['payload']==raw
    assert item['envelope']['actor']['role']=='creator'
    assert item['value']['state']=='RESERVED'


def test_publisher_cannot_reserve():
    value=header()
    sha=codec.encode_header(value)[4064:].hex()
    with pytest.raises(ValueError):
        codec.encode_slot(value,sha,0,1,payload('publisher'))


def test_old_header_rejected():
    from api import display_bootstrap_audit_codec as old
    with pytest.raises(ValueError):
        codec.decode_header(old.encode_header(dict(header(),format_version=1)))
