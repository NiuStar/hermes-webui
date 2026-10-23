#!/usr/bin/env python3
"""Build-host release follower. Publishes only after actual final-image acceptance.

Run with an immutable accepted WebUI base. Safe to invoke on a timer: existing
catalog tags are checked against exact labels; failed receipts never publish.
"""
import argparse,fcntl,hashlib,json,os,pathlib,shutil,subprocess,sys,tarfile,tempfile,time,urllib.request
import re
import urllib.error
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from api.agent_image_catalog import image_tag
from api.official_agent_release import latest,prove_ancestry


def run(args,timeout=60,**kwargs):
    result=subprocess.run(args,capture_output=True,text=True,timeout=timeout,**kwargs)
    if result.returncode:
        exc=RuntimeError('release command failed: '+args[0]+' '+str(result.returncode))
        exc.transport_detail=result.stderr
        raise exc
    return result.stdout.strip()


def retry_transport(fn):
    """Only transport failures retry; never retry identity/QA/cleanup failures."""
    for attempt in range(3):
        try:return fn()
        except (TimeoutError,urllib.error.URLError,subprocess.TimeoutExpired,subprocess.CalledProcessError,RuntimeError) as exc:
            if isinstance(exc,urllib.error.HTTPError) and exc.code not in (429,500,502,503,504):raise
            if isinstance(exc,(subprocess.CalledProcessError,RuntimeError)):
                detail=str(getattr(exc,'stderr',None) or getattr(exc,'transport_detail','')).lower()
                if not any(s in detail for s in ('timeout','timed out','reset','early eof','resolve host','tls','http','connect','remote end hung up')):raise
            if attempt==2:raise
            time.sleep(5*(attempt+1))


def cleanup_scratch(attempt, commit):
    """Only terminal audited attempt payload; retain receipt/log/Dockerfile."""
    attempt=pathlib.Path(attempt)
    if attempt.is_symlink() or not re.fullmatch(r'[0-9a-f]{40}',commit):raise ValueError('Invalid scratch owner')
    p=attempt/'receipt.json'
    if p.is_symlink() or not p.is_file():raise ValueError('Published receipt required')
    receipt=json.loads(p.read_text())
    if receipt.get('state') not in ('published','verified','failed') or receipt.get('agent_commit')!=commit:raise ValueError('Terminal receipt mismatch')
    paths=[attempt/'agent.tar.gz',attempt/('hermes-agent-'+commit),attempt/'agent.tar.gz.partial']
    if any(p.is_symlink() for p in paths):raise ValueError('Symlink scratch refused')
    removed=[]
    for p in paths:
        if p.is_dir():shutil.rmtree(p);removed.append(p.name)
        elif p.is_file():p.unlink();removed.append(p.name)
    (attempt/'scratch-cleanup.json').write_text(json.dumps({'removed':removed,'time':time.time()}))
    return removed


def cleanup_qa(cid, owner, attempt):
    if not re.fullmatch(r'[0-9a-f]{64}',cid):raise ValueError('Exact QA ID required')
    d=json.loads(run(['docker','inspect',cid]))[0]
    labeled=d['Config'].get('Labels',{}).get('io.hermes.release-builder.attempt')==owner
    receipt_path=pathlib.Path(attempt)/'receipt.json'
    recorded=False
    if receipt_path.is_file() and not receipt_path.is_symlink():
        receipt=json.loads(receipt_path.read_text())
        recorded=(receipt.get('state')=='published' and receipt.get('qa_container')==cid
                  and receipt.get('image_id')==d['Image'] and pathlib.Path(attempt).name==owner)
    if (d['Id']!=cid or d['State']['Status']!='exited' or d['Mounts'] or not (labeled or recorded)):
        raise ValueError('QA cleanup ownership mismatch')
    audit={k:d[k] for k in ('Id','Image','State')}
    (pathlib.Path(attempt)/'qa-audit.json').write_text(json.dumps(audit,indent=2))
    # Keep failed QA stderr on builder; never publicize model/config values.
    logs=subprocess.run(['docker','logs','--tail','120',cid],capture_output=True,text=True,timeout=20)
    if logs.returncode:raise RuntimeError('QA log capture failed; container retained')
    p=pathlib.Path(attempt)/'qa.log';p.write_text(logs.stdout+logs.stderr);p.chmod(0o600)
    again=json.loads(run(['docker','inspect',cid]))[0]
    if (again['State']['Status']!='exited' or again['Mounts'] or again['Id']!=cid or
            again['Image']!=d['Image'] or again['Config']!=d['Config']):raise ValueError('QA owner changed')
    run(['docker','rm',cid])


def reap_published_scratch(root):
    """Receipted source/archives only: no arbitrary directories or Docker prune."""
    for p in pathlib.Path(root).iterdir():
        if not p.is_dir() or p.is_symlink():continue
        m=re.fullmatch(r'agent-[0-9a-f]{40}-([0-9a-f]{40})-[0-9]{8}T[0-9]{6}Z',p.name)
        if not m:continue
        qa=p/'qa-owner.json'
        if qa.is_file() and not qa.is_symlink():
            owner=json.loads(qa.read_text());cid=owner.get('id','')
            if re.fullmatch(r'[0-9a-f]{64}',cid):
                found=subprocess.run(['docker','inspect',cid],capture_output=True,text=True,timeout=20)
                if found.returncode==0:
                    d=json.loads(found.stdout)[0]
                    if d['State']['Status']=='exited':cleanup_qa(cid,p.name,p)
                    else:raise RuntimeError('Previous owned QA still running; manual reconciliation required')
        r=p/'receipt.json'
        # A killed process can leave no terminal receipt. Under the global
        # prepare lock, only our exact target-bound ownership marker may close
        # that interrupted attempt; unmarked historical directories stay intact.
        marker=p/'attempt-owner.json'
        if not r.exists() and marker.is_file() and not marker.is_symlink():
            owner=json.loads(marker.read_text())
            if owner.get('attempt')==p.name and owner.get('agent_commit')==m[1]:
                r.write_text(json.dumps({'state':'failed','agent_commit':m[1],
                                         'error_type':'InterruptedPreparation'}))
        if r.is_file() and not r.is_symlink():
            data=json.loads(r.read_text())
            if data.get('state') in ('published','verified','failed') and data.get('agent_commit')==m[1]:
                cid=data.get('qa_container')
                if data.get('state')=='published' and isinstance(cid,str) and re.fullmatch(r'[0-9a-f]{64}',cid):
                    found=subprocess.run(['docker','inspect',cid],capture_output=True,text=True,timeout=20)
                    if found.returncode==0:cleanup_qa(cid,p.name,p)
                cleanup_scratch(p,m[1])


def retire_old_images(root, repository, protected):
    """Keep two latest receipted tags; no force/global/cache/volume prune."""
    records={}
    for p in pathlib.Path(root).glob('agent-*/receipt.json'):
        if p.is_symlink() or p.parent.is_symlink():continue
        data=json.loads(p.read_text())
        if data.get('state')!='published':continue
        try:ref=repository+':'+image_tag(data['webui_commit'],data['agent_commit'])
        except (KeyError,ValueError):continue
        if data.get('image')!=ref or not re.fullmatch(r'sha256:[0-9a-f]{64}',data.get('image_id','')):continue
        if not p.parent.name.startswith(ref.split(':')[-1]+'-'):continue
        records[ref]=(p.stat().st_mtime,data['image_id'])
    ordered=sorted(records,key=lambda x:records[x][0],reverse=True)
    removed=[]
    for ref in ordered[2:]:
        if ref in protected or records[ref][1] in protected:continue
        if run(['docker','ps','-aq','--filter','ancestor='+records[ref][1]]).strip():continue
        found=subprocess.run(['docker','image','inspect',ref],capture_output=True,text=True,timeout=20)
        if found.returncode:continue
        actual=json.loads(found.stdout)[0]
        if actual.get('Id')!=records[ref][1]:continue
        result=subprocess.run(['docker','image','rm',ref],capture_output=True,text=True,timeout=30)
        if result.returncode==0:removed.append(ref)
    (pathlib.Path(root)/'image-retention.json').write_text(json.dumps({'removed':removed,'kept_latest':ordered[:2],'time':time.time()}))


def download_archive(commit, archive):
    partial=archive.with_name('agent.tar.gz.partial')
    def fetch():
        with urllib.request.urlopen('https://codeload.github.com/NousResearch/hermes-agent/tar.gz/'+commit,timeout=120) as response,partial.open('wb') as f:
            total=0
            while block:=response.read(1024*1024):
                total+=len(block)
                if total>180*1024**2:raise ValueError('Agent archive too large')
                f.write(block)
        partial.replace(archive)
    retry_transport(fetch)


def registry_manifest(target):
    def inspect():
        result=subprocess.run(['docker','manifest','inspect',target],capture_output=True,text=True,timeout=45)
        if result.returncode and not any(s in result.stderr.lower() for s in ('no such manifest','manifest unknown','not found')):
            raise subprocess.CalledProcessError(result.returncode,['docker','manifest','inspect'],stderr=result.stderr)
        return result
    try:return retry_transport(inspect)
    except (subprocess.SubprocessError,TimeoutError) as exc:
        raise RuntimeError('Registry availability unknown; refusing to overwrite or rebuild') from exc

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
    retry_transport(lambda:run(['docker','pull',image],timeout=600))
    actual=json.loads(run(['docker','image','inspect',image]))[0]
    labels=actual['Config']['Labels']
    if (labels.get('org.opencontainers.image.revision')!=webui or
            labels.get('org.opencontainers.image.version')!=version or
            actual['Os']+'/'+actual['Architecture']!=platform):
        raise ValueError('Verifier WebUI identity/platform mismatch')
    return actual['Id']


V0225_PYPROJECT_SHA256='68325d6717bd4ebc3a7b015c5150bea4d5600c612259b9b7393e964caa828761'


def apply_dependency_compatibility(source, commit, version, attempt):
    # Operator-approved metadata-only correction, not a policy for future releases.
    if (commit,version)!=('bf0203a794a07aac3ff764972ab9087baa4e4f4e','v0.22.5'):return None
    path=source/'pyproject.toml';original=path.read_bytes()
    old=b'Pillow==13.0.0';new=b'Pillow==12.3.0'
    if (hashlib.sha256(original).hexdigest()!=V0225_PYPROJECT_SHA256 or original.count(old)!=1):
        raise ValueError('Approved dependency source mismatch')
    patched=original.replace(old,new)
    evidence={'agent_commit':commit,'agent_version':version,
              'upstream_sha256':hashlib.sha256(original).hexdigest(),
              'patched_sha256':hashlib.sha256(patched).hexdigest(),
              'overrides':{'Pillow':'12.3.0'},
              'reason':'Operator-approved correction of browser-harness 0.1.13 exact Pillow conflict'}
    (attempt/'upstream-pyproject.toml').write_bytes(original)
    path.write_bytes(patched)
    (source/'.hermes-compatibility.json').write_text(json.dumps(evidence,sort_keys=True))
    (attempt/'dependency-compatibility.json').write_text(json.dumps(evidence,sort_keys=True))
    return evidence


def prepare(base: str, repository: str, root: pathlib.Path, *, publish: bool, verifier=None, rehearse=False) -> dict:
    import re
    if (not re.fullmatch(re.escape(repository)+r'@sha256:[0-9a-f]{64}',base)
            or not root.is_absolute()):raise ValueError('Immutable configured base required')
    if rehearse and publish:raise ValueError('Rehearsal cannot publish')
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    lock=(root/'prepare.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    version,commit=retry_transport(latest)
    attempt=None
    new=None
    status_root=os.getenv('HERMES_AGENT_PREPARATION_STATUS_DIR')
    from api.agent_preparation_status import write_status
    def status(state):
        if status_root:write_status(status_root,webui,commit,version,state)
    retry_transport(lambda:run(['docker','pull',base],timeout=600))
    old=json.loads(run(['docker','image','inspect',base]))[0];labels=old['Config']['Labels']
    webui=labels['org.opencontainers.image.revision'];webver=labels['org.opencontainers.image.version']
    old_agent=labels['org.opencontainers.image.hermes-agent.revision']
    if old_agent==commit and not rehearse:return {'state':'up_to_date','agent_commit':commit}

    tag=image_tag(webui,commit);target=repository+':'+tag
    if rehearse:target += '-rehearsal-'+str(os.getpid())
    # Never overwrite a prior published pair.
    check=(subprocess.CompletedProcess([],1,stdout='',stderr='manifest unknown') if rehearse else registry_manifest(target))
    if check.returncode==0:
        retry_transport(lambda:run(['docker','pull',target],timeout=600))
        existing=json.loads(run(['docker','image','inspect',target]))[0]
        actual=existing['Config']['Labels']
        assert actual['org.opencontainers.image.revision']==webui
        assert actual['org.opencontainers.image.version']==webver
        assert actual['org.opencontainers.image.hermes-agent.revision']==commit
        assert actual['org.opencontainers.image.hermes-agent.version']==version
        assert existing['Os']+'/'+existing['Architecture']==old['Os']+'/'+old['Architecture']
        if os.getenv('HERMES_AGENT_MANAGED_CLEANUP')=='1':
            reap_published_scratch(root)
            retire_old_images(root,repository,{base,old['Id'],verifier})
        status('PUBLISHED')
        return {'state':'already_published','image':target,'agent_commit':commit,'repo_digests':existing['RepoDigests']}
    if not any(s in check.stderr.lower() for s in ('no such manifest','manifest unknown','not found')):
        raise RuntimeError('Registry availability unknown; refusing to overwrite or rebuild')
    try:
        status('IMAGE_PREPARING')
        if os.getenv('HERMES_AGENT_MANAGED_CLEANUP')=='1':
            reap_published_scratch(root)
            retire_old_images(root,repository,{base,old['Id'],verifier})
        if old_agent!=commit:retry_transport(lambda:prove_ancestry(old_agent,commit,version))
        if shutil.disk_usage(root).free<3*1024**3:raise RuntimeError('Insufficient build/QA disk headroom')
        verifier_id=(runtime_verifier(verifier,repository,webui,webver,old['Os']+'/'+old['Architecture'])
                     if verifier else None)
        attempt=root/(tag+'-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()));attempt.mkdir(mode=0o700)
        (attempt/'attempt-owner.json').write_text(json.dumps({'attempt':attempt.name,'agent_commit':commit}))
        archive=attempt/'agent.tar.gz'
        download_archive(commit,archive)
        with tarfile.open(archive) as tar:
            members=tar.getmembers()
            if sum(m.size for m in members)>800*1024**2:raise ValueError('Expanded Agent archive too large')
            for m in members:
                parts=pathlib.PurePosixPath(m.name).parts
                if not parts or parts[0]!='hermes-agent-'+commit or '..' in parts or not (m.isfile() or m.isdir()):
                    raise ValueError('Unexpected official archive member')
            tar.extractall(attempt,filter='data')
        source=attempt/('hermes-agent-'+commit)
        compatibility=apply_dependency_compatibility(source,commit,version,attempt)
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
            # Legacy builder removes its exact intermediate containers/images;
            # do not add perpetual shared BuildKit caches for every release.
            result=subprocess.run(['docker','build','--pull=false','--no-cache','--rm=true','--force-rm=true','-t',target,str(attempt)],
                env={**os.environ,'DOCKER_BUILDKIT':'0'},stdout=log,stderr=subprocess.STDOUT,timeout=600)
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
        cid=run(['docker','run','-d','--name',name,'--restart=no','--label','io.hermes.release-builder.attempt='+attempt.name,'-e','HERMES_WEBUI_STATE_DIR=/home/hermeswebui/.hermes/webui','-e','HERMES_WEBUI_AUTH_MODE=none','-e','PYTHONPATH=/opt/hermes','-e','HERMES_WEBUI_AGENT_DIR=/opt/hermes',new['Id']])
        (attempt/'qa-owner.json').write_text(json.dumps({'id':cid,'image_id':new['Id']}))
        try:
            deadline=time.monotonic()+420
            while time.monotonic()<deadline:
                owner=json.loads(run(['docker','inspect',cid]))[0]
                if not owner['State']['Running']:raise RuntimeError('Candidate init exited')
                if owner['State'].get('Health',{}).get('Status')=='healthy':break
                time.sleep(5)
            else:raise RuntimeError('Candidate health deadline')
            dependency_probe=None
            if compatibility:
                # No --no-deps / metadata-only success: test the installed graph
                # and the affected image stack in the actual non-root venv.
                run(['docker','exec','-u','1024:1024',cid,'uv','pip','check','--python','/app/venv/bin/python3'],timeout=60)
                image_probe="""import io,json,importlib.metadata as m
from PIL import Image
import pillow_heif,browser_harness
assert m.version('Pillow')=='12.3.0'
assert m.version('browser-harness')=='0.1.13'
for fmt in ('PNG','JPEG'):
    b=io.BytesIO();Image.new('RGB',(17,11),(12,34,56)).save(b,format=fmt);b.seek(0)
    im=Image.open(b);im.load();assert im.size==(17,11)
print(json.dumps({'passed':True,'pillow':m.version('Pillow'),'browser_harness':m.version('browser-harness'),'pillow_heif':m.version('pillow-heif'),'formats':['PNG','JPEG']}))
"""
                dependency_probe=json.loads(run(['docker','exec','-u','1024:1024',cid,'/app/venv/bin/python3','-c',image_probe],timeout=60))
            run(['docker','cp',str(pathlib.Path(__file__).with_name('agent_release_tool_probe.py')),cid+':/tmp/agent_release_tool_probe.py'])
            run(['docker','exec',cid,'chmod','644','/tmp/agent_release_tool_probe.py'])
            loop=run(['docker','exec','-u','1024:1024',cid,'/app/venv/bin/python3','/tmp/agent_release_tool_probe.py',commit,version],timeout=210)
            probe="import sys;sys.path.insert(0,'/apptoo');from api.docker_self_update import DockerEngine,_verify_imported_agent;_verify_imported_agent(DockerEngine(),"+repr(cid)+','+repr(commit)+','+repr(version)+");print('verified')"
            run(['docker','run','--rm','--network','none','--read-only','-v','/var/run/docker.sock:/var/run/docker.sock','--entrypoint','python3',verifier_id or new['Id'],'-B','-c',probe],timeout=210)
        except Exception:
            try:
                run(['docker','stop',cid],timeout=45)
                if os.getenv('HERMES_AGENT_MANAGED_CLEANUP')=='1':cleanup_qa(cid,attempt.name,attempt)
            except Exception as cleanup_error:
                (attempt/'cleanup-error.json').write_text(json.dumps({'error_type':type(cleanup_error).__name__}))
            raise
        else:
            run(['docker','stop',cid],timeout=45)
            if os.getenv('HERMES_AGENT_MANAGED_CLEANUP')=='1':cleanup_qa(cid,attempt.name,attempt)
        # Recheck publication identity immediately before push; no stale target promotion.
        if retry_transport(latest)!=(version,commit):raise RuntimeError('Official release changed during build')
        receipt={'state':'verified','base':base,'image':target,'image_id':new['Id'],'webui_commit':webui,'webui_version':webver,'agent_commit':commit,'agent_version':version,'platform':new['Os']+'/'+new['Architecture'],'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'tool_loop':json.loads(loop),'qa_container':cid}
        receipt['verifier_image_id']=verifier_id or new['Id']
        if compatibility:
            receipt['dependency_compatibility']=compatibility
            receipt['dependency_probe']=dependency_probe
        (attempt/'verification.json').write_text(json.dumps(receipt,indent=2))
        if publish:
            repeated=registry_manifest(target)
            if repeated.returncode==0 or not any(s in repeated.stderr.lower() for s in ('no such manifest','manifest unknown','not found')):
                raise RuntimeError('Catalog target appeared or registry unavailable before push')
            retry_transport(lambda:run(['docker','push',target],timeout=600))
            distribution=json.loads(run(['docker','manifest','inspect',target],timeout=60))
            if not distribution:raise RuntimeError('Published manifest missing')
            retry_transport(lambda:run(['docker','pull',target],timeout=600))
            actual=json.loads(run(['docker','image','inspect',target]))[0]
            assert actual['Config']['Labels']['org.opencontainers.image.hermes-agent.revision']==commit
            receipt.update(state='published',repo_digests=actual['RepoDigests'])
        (attempt/'receipt.json').write_text(json.dumps(receipt,indent=2))
        (root/'last-result.json').write_text(json.dumps(receipt,indent=2))
        if publish:status('PUBLISHED')
        if os.getenv('HERMES_AGENT_MANAGED_CLEANUP')=='1':
            cleanup_scratch(attempt,commit)
            if rehearse:run(['docker','image','rm',target])
        return receipt
    except Exception as exc:
        if attempt is not None:
            p=attempt/'receipt.json'
            if not p.exists():
                evidence={'state':'failed','agent_commit':commit,'agent_version':version,'error_type':type(exc).__name__}
                if new is not None:evidence.update(image=target,image_id=new['Id'])
                if (attempt/'agent.tar.gz').is_file():evidence['archive_sha256']=hashlib.sha256((attempt/'agent.tar.gz').read_bytes()).hexdigest()
                p.write_text(json.dumps(evidence,indent=2))
                if os.getenv('HERMES_AGENT_MANAGED_CLEANUP')=='1':
                    try:cleanup_scratch(attempt,commit)
                    except Exception:pass
                    if new is not None:
                        # Unpublished tag belongs to this exact attempt only.
                        # Engine will refuse referenced images; never force.
                        try:
                            current=json.loads(run(['docker','image','inspect',target]))[0]
                            if current['Id']==new['Id'] and not run(['docker','ps','-aq','--filter','ancestor='+new['Id']]).strip():
                                run(['docker','image','rm',target])
                        except Exception:pass
        try:status('IMAGE_FAILED')
        except Exception:pass  # Diagnostics must not mask the build failure.
        raise

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base',required=True);p.add_argument('--repository',default='24802117/hermes-webui');p.add_argument('--root',type=pathlib.Path,required=True);p.add_argument('--publish',action='store_true');args=p.parse_args()
    print(json.dumps(prepare(args.base,args.repository,args.root,publish=args.publish,
                            verifier=os.getenv('HERMES_AGENT_VERIFIER_IMAGE') or None)))
