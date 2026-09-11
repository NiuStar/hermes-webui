"""Adapter tests use real fixed bytes; no kernel admission claims."""
import hashlib
import stat
from api.display_bootstrap_audit_codec_v2 import encode_header, encode_slot, SLOT_BYTES
from api.display_bootstrap_audit_scan import scan
from api.display_bootstrap_manifest import canonical_bytes, digest
from api import display_bootstrap_audit_codec_v2 as codec
from api.display_bootstrap_fixed_registry import envelope_bytes
from tests.test_display_bootstrap_v3 import resource
from api.display_bootstrap_fixed_registry import append_state, persist_platform
from api.display_bootstrap_audit_budget import require_slots


class MemoryLog:
    def __init__(self):
        self.resource = resource()
        self.actor = dict(role='creator', **self.resource['hard_limit_profiles']['creator'])
        self.header = dict(format_version=2,candidate_id='a'*32,
            file_identity=dict(dev=1,ino=2,uid=1000,gid=1000,mode=stat.S_IFREG|0o600,nlink=1),
            deployment_sha='b'*64,resource_sha=digest(canonical_bytes(self.resource)),slot_count=9,slot_bytes=SLOT_BYTES,payload_bytes=65536)
        self.raw = encode_header(self.header)+bytes(9*SLOT_BYTES)
        first = dict(format_version=1,candidate_id='a'*32,target_name='a'*32,seq=1,
                     previous_sha=None,state='RESERVED',manifest_sha=None,approval_id=None,error_code=None)
        self.append(1,envelope_bytes(first,self.actor))
    def inspect(self):
        return scan(lambda offset,length:self.raw[offset:offset+length], codec=codec, resource=self.resource)
    def append(self,kind,payload):
        before=self.inspect()
        index=len(before['receipts'])
        raw=encode_slot(self.header,self.raw[4064:4096].hex(),index,kind,payload)
        offset=4096+index*SLOT_BYTES
        proposed=self.raw[:offset]+raw+self.raw[offset+SLOT_BYTES:]
        result=scan(lambda start,length:proposed[start:start+length], codec=codec, resource=self.resource)
        self.raw=proposed
        return result['receipts'][-1]
    def get(self,kind,key):
        from api.display_bootstrap_audit_codec_v2 import decode_slot
        for receipt in self.inspect()['receipts']:
            if (receipt['kind'],receipt['logical_key'])==(kind,key):
                index=receipt['physical_slot']; offset=4096+index*SLOT_BYTES
                return decode_slot(self.header,self.raw[4064:4096].hex(),index,
                                   self.raw[offset:offset+SLOT_BYTES])['payload']


def test_minimum_log_advances_after_platform_slot():
    log=MemoryLog()
    append_state(log,'BUILDING')
    evidence=dict(format_version=1,evidence_id='d'*32,kernel='test',sqlite_version='test',
                  compile_options=[],vfs='unix',filesystem='ext4',mount_id=1,
                  mount_options=['rw'],namespace_id='mnt:[1]',approved_policy_sha='b'*64)
    persist_platform(log,evidence,'b'*64)
    assert log.inspect()['remaining_slots']==6
    assert require_slots(log.inspect(),operation='advance')==6
    append_state(log,'VERIFIED',manifest_sha='e'*64)
    log.actor = dict(role='publisher', **log.resource['hard_limit_profiles']['publisher'])
    append_state(log,'APPROVED',approval_id='f'*32)
    append_state(log,'PUBLISH_INTENT')
    completed=append_state(log,'PUBLISHED_UNACTIVATED')
    assert completed['seq']==6
    assert log.inspect()['remaining_slots']==2
