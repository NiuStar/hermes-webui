"""Fixed-registry catalog for prepared Agent-only images, no publisher key."""
import re


class CatalogUnavailable(ValueError):
    def __init__(self, state):
        self.state = state
        super().__init__(state)


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
    try:
        record=engine.distribution(repository+':'+image_tag(webui_commit,agent_commit))
    except Exception as exc:
        # Only an authoritative missing-manifest result proves absence. A
        # timeout/auth/registry outage must never be reported as still building.
        missing = str(exc).startswith('Docker API 404:') and any(
            s in str(exc).lower() for s in ('manifest unknown','no such manifest','not found'))
        raise CatalogUnavailable('IMAGE_PENDING' if missing else 'REGISTRY_UNAVAILABLE') from exc
    digest=(record.get('Descriptor') or {}).get('digest')
    systems=[str(p.get('os'))+'/'+str(p.get('architecture')) for p in record.get('Platforms',[])]
    if not isinstance(digest,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',digest) or platform not in systems:
        raise CatalogUnavailable('IMAGE_INCOMPATIBLE')
    return {'image':repository+'@'+digest,'webui_commit':webui_commit,'webui_version':webui_version,
            'agent_commit':agent_commit,'agent_version':agent_version,'platform':platform}
