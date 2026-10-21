#!/usr/bin/env python3
"""Build-host release follower. Publishes only after actual final-image acceptance.

Run with an immutable accepted WebUI base. Safe to invoke on a timer: existing
catalog tags are checked against exact labels; failed receipts never publish.
"""
import argparse,fcntl,hashlib,json,os,pathlib,shutil,subprocess,sys,tarfile,tempfile,time,urllib.request
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from api.agent_image_catalog import image_tag
from api.official_agent_release import latest,prove_ancestry


def run(args,timeout=60,**kwargs):
    result=subprocess.run(args,capture_output=True,text=True,timeout=timeout,**kwargs)
    if result.returncode:raise RuntimeError('release command failed: '+args[0]+' '+str(result.returncode))
    return result.stdout.strip()

def normalize_source_permissions(root: pathlib.Path) -> None:
    """Public upstream code must be traversable by the non-root image runtime.

    Keep private build receipts/output outside this tree at service umask 0077.
    Preserve executable intent, never setuid/setgid or group/world write bits.
    """
    for path in [root, *root.rglob('*')]:
        if path.is_symlink():
            raise ValueError('Unexpected symlink in release source')
        mode = path.stat().st_mode
        path.chmod(0o755 if path.is_dir() or mode & 0o111 else 0o644)

def runtime_verifier(image, repository, webui, version, platform):
    import re
    if not re.fullmatch(re.escape(repository)+r'@sha256:[0-9a-f]{64}',image):
        raise ValueError('Immutable verifier image required')
    run(['docker','pull',image],timeout=600)
    actual=json.loads(run(['docker','image','inspect',image]))[0]
    labels=actual['Config']['Labels']
    if (labels.get('org.opencontainers.image.revision')!=webui or
            labels.get('org.opencontainers.image.version')!=version or
            actual['Os']+'/'+actual['Architecture']!=platform):
        raise ValueError('Verifier WebUI identity/platform mismatch')
    return actual['Id']


def prepare(base: str, repository: str, root: pathlib.Path, *, publish: bool, verifier=None) -> dict:
    import re
    if (not re.fullmatch(re.escape(repository)+r'@sha256:[0-9a-f]{64}',base)
            or not root.is_absolute()):raise ValueError('Immutable configured base required')
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    lock=(root/'prepare.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    version,commit=latest()
    run(['docker','pull',base],timeout=600)
    old=json.loads(run(['docker','image','inspect',base]))[0];labels=old['Config']['Labels']
    webui=labels['org.opencontainers.image.revision'];webver=labels['org.opencontainers.image.version']
    old_agent=labels['org.opencontainers.image.hermes-agent.revision']
    if old_agent==commit:return {'state':'up_to_date','agent_commit':commit}
    prove_ancestry(old_agent,commit,version)
    tag=image_tag(webui,commit);target=repository+':'+tag
    # Never overwrite a prior published pair.
    check=subprocess.run(['docker','manifest','inspect',target],capture_output=True,text=True,timeout=45)
    if check.returncode==0:
        run(['docker','pull',target],timeout=600)
        existing=json.loads(run(['docker','image','inspect',target]))[0]
        actual=existing['Config']['Labels']
        assert actual['org.opencontainers.image.revision']==webui
        assert actual['org.opencontainers.image.version']==webver
        assert actual['org.opencontainers.image.hermes-agent.revision']==commit
        assert actual['org.opencontainers.image.hermes-agent.version']==version
        assert existing['Os']+'/'+existing['Architecture']==old['Os']+'/'+old['Architecture']
        return {'state':'already_published','image':target,'agent_commit':commit,'repo_digests':existing['RepoDigests']}
    if not any(s in check.stderr.lower() for s in ('no such manifest','manifest unknown','not found')):
        raise RuntimeError('Registry availability unknown; refusing to overwrite or rebuild')
    if shutil.disk_usage(root).free<3*1024**3:raise RuntimeError('Insufficient build/QA disk headroom')
    verifier_id=(runtime_verifier(verifier,repository,webui,webver,old['Os']+'/'+old['Architecture'])
                 if verifier else None)
    attempt=root/(tag+'-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()));attempt.mkdir(mode=0o700)
    archive=attempt/'agent.tar.gz'
    with urllib.request.urlopen('https://codeload.github.com/NousResearch/hermes-agent/tar.gz/'+commit,timeout=120) as response,archive.open('wb') as f:
        total=0
        while block:=response.read(1024*1024):
            total+=len(block)
            if total>180*1024**2:raise ValueError('Agent archive too large')
            f.write(block)
    with tarfile.open(archive) as tar:
        members=tar.getmembers()
        if sum(m.size for m in members)>800*1024**2:raise ValueError('Expanded Agent archive too large')
        for m in members:
            parts=pathlib.PurePosixPath(m.name).parts
            if not parts or parts[0]!='hermes-agent-'+commit or '..' in parts or not (m.isfile() or m.isdir()):
                raise ValueError('Unexpected official archive member')
        tar.extractall(attempt,filter='data')
    source=attempt/('hermes-agent-'+commit)
    (source/'.hermes-agent-revision').write_text(commit+'\n')
    (source/'install-stamp.json').write_text(json.dumps({'baseVersion':version[1:],'commit':commit,'source':'docker','distribution':'docker','updateMechanism':'external'}))
    normalize_source_permissions(source)
    localbase='hermes-agent-release-base:'+old['Id'].split(':')[1][:16]
    run(['docker','tag',old['Id'],localbase])
    (attempt/'Dockerfile').write_text(f'''FROM {localbase}
USER root
RUN rm -rf /opt/hermes && mkdir -p /opt/hermes
COPY --chown=root:root hermes-agent-{commit}/ /opt/hermes/
LABEL org.opencontainers.image.hermes-agent.revision="{commit}" org.opencontainers.image.hermes-agent.version="{version}"
''')
    (attempt/'.dockerignore').write_text('/agent.tar.gz\n/*.json\n/*.log\n')
    # Keep private raw build diagnostics on builder, never embed them into image.
    with (attempt/'build.log').open('w') as log:
        result=subprocess.run(['docker','build','--pull=false','-t',target,str(attempt)],stdout=log,stderr=subprocess.STDOUT,timeout=600)
    if result.returncode:raise RuntimeError('Candidate image build failed')
    new=json.loads(run(['docker','image','inspect',target]))[0]
    readable="import os,pathlib;root=pathlib.Path('/opt/hermes');assert os.access(root,os.R_OK|os.X_OK);assert all(os.access(p,os.R_OK|(os.X_OK if p.is_dir() else 0)) for p in root.rglob('*'))"
    run(['docker','run','--rm','--network','none','--read-only','--user','1024:1024',
         '--entrypoint','python3',new['Id'],'-B','-c',readable],timeout=60)
    # Actual immutable base/candidate payload comparison, not labels alone.
    compare="import pathlib,hashlib,json;print(json.dumps({str(p.relative_to('/apptoo')):hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path('/apptoo').rglob('*') if p.is_file()},sort_keys=True))"
    def hashes(image):return run(['docker','run','--rm','--network','none','--read-only','--entrypoint','python3',image,'-B','-c',compare],timeout=120)
    assert hashes(old['Id'])==hashes(new['Id']),'Agent-only build changed WebUI payload'
    name='hermes-agent-release-qa-'+commit[:12]+'-'+str(os.getpid())
    cid=run(['docker','run','-d','--name',name,'--restart=no','-e','HERMES_WEBUI_STATE_DIR=/home/hermeswebui/.hermes/webui','-e','HERMES_WEBUI_AUTH_MODE=none','-e','PYTHONPATH=/opt/hermes','-e','HERMES_WEBUI_AGENT_DIR=/opt/hermes',new['Id']])
    try:
        deadline=time.monotonic()+420
        while time.monotonic()<deadline:
            owner=json.loads(run(['docker','inspect',cid]))[0]
            if not owner['State']['Running']:raise RuntimeError('Candidate init exited')
            if owner['State'].get('Health',{}).get('Status')=='healthy':break
            time.sleep(5)
        else:raise RuntimeError('Candidate health deadline')
        run(['docker','cp',str(pathlib.Path(__file__).with_name('agent_release_tool_probe.py')),cid+':/tmp/agent_release_tool_probe.py'])
        run(['docker','exec',cid,'chmod','644','/tmp/agent_release_tool_probe.py'])
        loop=run(['docker','exec','-u','1024:1024',cid,'/app/venv/bin/python3','/tmp/agent_release_tool_probe.py',commit,version],timeout=210)
        probe="import sys;sys.path.insert(0,'/apptoo');from api.docker_self_update import DockerEngine,_verify_imported_agent;_verify_imported_agent(DockerEngine(),"+repr(cid)+','+repr(commit)+','+repr(version)+");print('verified')"
        run(['docker','run','--rm','--network','none','--read-only','-v','/var/run/docker.sock:/var/run/docker.sock','--entrypoint','python3',verifier_id or new['Id'],'-B','-c',probe],timeout=210)
    finally:
        run(['docker','stop',cid],timeout=45)
    # Recheck publication identity immediately before push; no stale target promotion.
    if latest()!=(version,commit):raise RuntimeError('Official release changed during build')
    receipt={'state':'verified','base':base,'image':target,'image_id':new['Id'],'webui_commit':webui,'webui_version':webver,'agent_commit':commit,'agent_version':version,'platform':new['Os']+'/'+new['Architecture'],'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'tool_loop':json.loads(loop),'qa_container':cid}
    receipt['verifier_image_id']=verifier_id or new['Id']
    (attempt/'verification.json').write_text(json.dumps(receipt,indent=2))
    if publish:
        repeated=subprocess.run(['docker','manifest','inspect',target],capture_output=True,text=True,timeout=45)
        if repeated.returncode==0 or not any(s in repeated.stderr.lower() for s in ('no such manifest','manifest unknown','not found')):
            raise RuntimeError('Catalog target appeared or registry unavailable before push')
        run(['docker','push',target],timeout=600)
        distribution=json.loads(run(['docker','manifest','inspect',target],timeout=60))
        if not distribution:raise RuntimeError('Published manifest missing')
        run(['docker','pull',target],timeout=600)
        actual=json.loads(run(['docker','image','inspect',target]))[0]
        assert actual['Config']['Labels']['org.opencontainers.image.hermes-agent.revision']==commit
        receipt.update(state='published',repo_digests=actual['RepoDigests'])
    (attempt/'receipt.json').write_text(json.dumps(receipt,indent=2))
    (root/'last-result.json').write_text(json.dumps(receipt,indent=2))
    return receipt

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base',required=True);p.add_argument('--repository',default='24802117/hermes-webui');p.add_argument('--root',type=pathlib.Path,required=True);p.add_argument('--publish',action='store_true');args=p.parse_args()
    print(json.dumps(prepare(args.base,args.repository,args.root,publish=args.publish,
                            verifier=os.getenv('HERMES_AGENT_VERIFIER_IMAGE') or None)))
