"""Bounded public preparation evidence; diagnostic only, never install authority."""
import json
import os
from pathlib import Path
import stat
import tempfile
import time
try:
    from .agent_image_catalog import image_tag
except ImportError:
    from agent_image_catalog import image_tag


def write_status(root, webui, agent, version, state):
    if state not in {'IMAGE_PREPARING','IMAGE_FAILED','PUBLISHED'}:
        raise ValueError('Invalid preparation state')
    tag=image_tag(webui,agent)
    root=Path(root)
    if not root.exists():
        root.mkdir(parents=True,mode=0o755);root.chmod(0o755)
    payload={'webui_commit':webui,'agent_commit':agent,'agent_version':version,
             'state':state,'updated_at':time.time()}
    fd,name=tempfile.mkstemp(prefix='.status-',dir=root)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(payload,f);f.flush();os.fsync(f.fileno())
        os.chmod(name,0o644);os.replace(name,root/(tag+'.json'))
    finally:
        if os.path.exists(name):os.unlink(name)


def read_status(root, webui, agent, version):
    """Missing, damaged, mismatched, future or stale receipts remain unknown."""
    if not root:return None
    try:
        p=Path(root)/(image_tag(webui,agent)+'.json')
        fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as f:
            st=os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_mode&0o022 or st.st_size>4096:return None
            data=json.loads(f.read(4097))
        if (data['webui_commit']!=webui or data['agent_commit']!=agent or
                data['agent_version']!=version or type(data['updated_at']) not in (int,float)):
            return None
        age=time.time()-data['updated_at']
        ttl=3600 if data['state']=='IMAGE_PREPARING' else 86400
        if not 0<=age<=ttl:return None
        return data['state'] if data['state'] in {'IMAGE_PREPARING','IMAGE_FAILED'} else None
    except (OSError,ValueError,KeyError,TypeError):
        return None
