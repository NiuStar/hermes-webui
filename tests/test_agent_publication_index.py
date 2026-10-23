"""A published catalog target must be a platform-bearing immutable index."""
import json
import pytest
from scripts import prepare_agent_release as b
REPO='24802117/hermes-webui'
CHILD='sha256:'+'a'*64
INDEX='sha256:'+'b'*64


def test_publication_creates_index_from_exact_verified_payload(monkeypatch):
    calls=[]
    payload=REPO+':agent-payload'
    target=REPO+':agent-catalog'
    doc={'schemaVersion':2,'mediaType':'application/vnd.oci.image.index.v1+json','manifests':[
        {'digest':CHILD,'platform':{'os':'linux','architecture':'amd64'}}]}
    def run(a,**_):
        calls.append(a)
        if a[-1]=='{{json .Manifest}}':return json.dumps({'digest':CHILD})
        if a[-1]=='--raw':return json.dumps(doc)
        return ''
    monkeypatch.setattr(b,'run',run)
    result=b.publish_platform_index(payload,target,REPO,'linux/amd64')
    assert ['docker','push',payload] in calls
    assert ['docker','buildx','imagetools','create','--tag',target,REPO+'@'+CHILD] in calls
    assert result['payload_digest']==CHILD
    assert result['index']==doc
    assert not any(a[:2]==['docker','push'] and a[-1]==target for a in calls)


@pytest.mark.parametrize('doc',[
    {'schemaVersion':2},
    {'manifests':[{'digest':CHILD,'platform':{'os':'linux','architecture':'arm64'}}]},
    {'manifests':[{'digest':'sha256:'+'c'*64,'platform':{'os':'linux','architecture':'amd64'}}]},
])
def test_publication_rejects_wrong_or_missing_platform_payload(monkeypatch,doc):
    def run(a,**_):
        if a[-1]=='{{json .Manifest}}':return json.dumps({'digest':CHILD})
        if a[-1]=='--raw':return json.dumps(doc)
        return ''
    monkeypatch.setattr(b,'run',run)
    with pytest.raises(RuntimeError,match='Published platform index mismatch'):
        b.publish_platform_index(REPO+':payload',REPO+':catalog',REPO,'linux/amd64')
