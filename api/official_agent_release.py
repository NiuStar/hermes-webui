"""Official Agent release identity via human-facing Release HTML and Git, never API."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request

REPO='https://github.com/NousResearch/hermes-agent'

def latest() -> tuple[str,str]:
    with urllib.request.urlopen(urllib.request.Request(REPO+'/releases/latest',headers={'User-Agent':'hermes-webui'}),timeout=20) as r:
        match=re.fullmatch(re.escape(REPO)+r'/releases/tag/(v[0-9]+\.[0-9]+\.[0-9]+)',r.geturl())
        data=r.read(2_000_001)
    if not match or len(data)>2_000_000:raise ValueError('Unverified official Agent Release')
    version=match[1];html=data.decode('utf-8')
    link=re.search(r'href="/NousResearch/hermes-agent/releases/tag/'+re.escape(version)+r'"',html)
    if not link:raise ValueError('Official Release tag link absent')
    commits=re.findall(r'href="/NousResearch/hermes-agent/commit/([0-9a-f]{40})"',html[link.end():link.end()+20000])
    if len(commits)!=1:raise ValueError('Official Release commit ambiguous')
    return version,commits[0]


def prove_ancestry(current: str, target: str, version: str) -> None:
    if (not all(isinstance(x,str) and re.fullmatch(r'[0-9a-f]{40}',x) for x in (current,target))
            or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+',version) or current==target):
        raise ValueError('Invalid upgrade direction')
    with tempfile.TemporaryDirectory(prefix='hermes-release-proof-') as root:
        env={**os.environ,'GIT_TERMINAL_PROMPT':'0','GIT_NO_LAZY_FETCH':'1',
             'GIT_HTTP_LOW_SPEED_LIMIT':'1','GIT_HTTP_LOW_SPEED_TIME':'30'}
        def git(*a,timeout=20):return subprocess.check_output(['git','-C',root,*a],env=env,stderr=subprocess.PIPE,text=True,timeout=timeout).strip()
        git('init','-q')
        git('-c','protocol.version=2','-c','http.version=HTTP/1.1','fetch','--quiet',
            '--no-tags','--filter=tree:0','--depth=1024',REPO+'.git',
            'refs/tags/'+version,timeout=90)
        if git('rev-parse','FETCH_HEAD^{}')!=target or git('cat-file','-t',current)!='commit':raise ValueError('Official Git commit mismatch')
        git('merge-base','--is-ancestor',current,target)
