"""Fixed-registry catalog for prepared Agent-only images, no publisher key."""
import re


def image_tag(webui_commit: str, agent_commit: str) -> str:
    if not all(isinstance(x,str) and re.fullmatch(r'[0-9a-f]{40}',x) for x in (webui_commit,agent_commit)):
        raise ValueError('Invalid catalog commits')
    return f'agent-{webui_commit}-{agent_commit}'


def resolve(engine, *, repository: str, webui_commit: str, webui_version: str,
            agent_commit: str, agent_version: str, platform: str) -> dict:
    if (not isinstance(repository,str)
            or not re.fullmatch(r'(?:[a-z0-9][a-z0-9._-]*(?::[0-9]{1,5})?/)(?:[a-z0-9][a-z0-9._-]*/)*[a-z0-9][a-z0-9._-]*',repository)
            or platform not in ('linux/amd64','linux/arm64')
            or not all(isinstance(v,str) and re.fullmatch(r'v[0-9][0-9A-Za-z_.-]*',v)
                       for v in (webui_version,agent_version))):
        raise ValueError('Invalid catalog target')
    record=engine.distribution(repository+':'+image_tag(webui_commit,agent_commit))
    digest=(record.get('Descriptor') or {}).get('digest')
    systems=[str(p.get('os'))+'/'+str(p.get('architecture')) for p in record.get('Platforms',[])]
    if not isinstance(digest,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',digest) or platform not in systems:
        raise ValueError('Prepared image is absent or platform does not match')
    return {'image':repository+'@'+digest,'webui_commit':webui_commit,'webui_version':webui_version,
            'agent_commit':agent_commit,'agent_version':agent_version,'platform':platform}
