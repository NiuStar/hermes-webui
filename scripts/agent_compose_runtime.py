"""Fixed-target Docker Compose runtime for an isolated Agent update candidate.

No shell commands are constructed from browser input. Deployment must provision
exact config file paths, project/service/container and publisher-signed artifact.
"""
from __future__ import annotations
import json
from pathlib import Path
import re
import subprocess
import time
try:
    from .agent_compose_transaction import PinError
except ImportError:
    from agent_compose_transaction import PinError

_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*\Z')
_REPO = re.compile(r'[a-z0-9._:/-]+\Z')
_SHA = re.compile(r'[0-9a-f]{40}\Z')


def verify_runtime_agent(owner: dict, commit: str, version: str) -> None:
    """Reuse the signed updater's real-venv staged/baked-source verifier."""
    from api.docker_self_update import DockerEngine, _verify_imported_agent
    identifier = owner.get('Id')
    if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{64}', identifier):
        raise PinError('Compose candidate has no immutable owner ID')
    _verify_imported_agent(DockerEngine(), identifier, commit, version)


class DockerComposeRuntime:
    def __init__(self, files: list[Path], *, project: str, service: str,
                 container: str, repository: str, agent_commit: str,
                 agent_version: str, health_timeout: int = 180, agent_probe=None):
        files = [Path(p) for p in files]
        if (not files or len(set(files)) != len(files)
                or any(not p.is_absolute() or not p.is_file() or p.is_symlink() for p in files)
                or not all(_NAME.fullmatch(s) for s in (project, service, container))
                or not _REPO.fullmatch(repository) or not _SHA.fullmatch(agent_commit)
                or not re.fullmatch(r'v[0-9][0-9A-Za-z_.-]*', agent_version)
                or not isinstance(health_timeout, int) or not 0 <= health_timeout <= 900):
            raise PinError('Invalid Compose runtime configuration')
        self.files=files
        self.project=project
        self.service=service
        self.container=container
        self.repository=repository
        self.agent_commit=agent_commit
        self.agent_version=agent_version
        self.health_timeout=health_timeout
        self.agent_probe=agent_probe
        self.cwd=files[0].parent
        self.prefix=['docker','compose','--project-directory',str(self.cwd)]
        for f in files:self.prefix.extend(['-f',str(f)])

    @staticmethod
    def _run(cmd: list[str], *, timeout: int = 30) -> str:
        try:
            result=subprocess.run(cmd,capture_output=True,text=True,timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PinError('Docker or Compose operation unavailable') from exc
        if result.returncode:
            raise PinError('Docker or Compose operation failed')
        return result.stdout.strip()

    def _stack(self, command: list[str], *, timeout: int = 30, path: Path | None = None) -> str:
        args=['docker','compose','--project-directory',str(self.cwd)]
        for f in self.files:
            args.extend(['-f',str(path if path is not None and f==self.files[-1] else f)])
        return self._run(args+command,timeout=timeout)

    def render(self,path: Path) -> dict:
        result=json.loads(self._stack(['config','--format','json'],path=Path(path)))
        if not isinstance(result,dict) or result.get('name')!=self.project:
            raise PinError('Compose effective project changed')
        return result

    def hash(self) -> str:
        result=self._stack(['config','--hash',self.service])
        parts=result.split()
        if len(parts)!=2 or parts[0]!=self.service or not re.fullmatch(r'[0-9a-f]{64}',parts[1]):
            raise PinError('Compose service hash unavailable')
        return parts[1]

    def inspect(self) -> dict:
        response=json.loads(self._run(['docker','inspect',self.container]))
        if not isinstance(response,list) or len(response)!=1:
            raise PinError('Compose target owner unavailable')
        return response[0]

    def image_info(self,ref: str) -> dict:
        if not re.fullmatch(re.escape(self.repository)+r'@sha256:[0-9a-f]{64}',ref):
            raise PinError('Image is outside configured repository or unpinned')
        response=json.loads(self._run(['docker','image','inspect',ref]))
        if not isinstance(response,list) or len(response)!=1:
            raise PinError('Pinned image unavailable')
        return response[0]

    def siblings(self) -> tuple[tuple[str,str,str],...]:
        raw=self._run(['docker','ps','-a','--filter','label=com.docker.compose.project='+self.project,
                       '--format','{{json .}}'])
        result=[]
        for line in raw.splitlines():
            item=json.loads(line)
            if item.get('Names')==self.container:continue
            name=item.get('Names');identifier=item.get('ID')
            if not isinstance(name,str) or not _NAME.fullmatch(name) or not isinstance(identifier,str):
                raise PinError('Invalid Compose sibling inventory')
            sibling=json.loads(self._run(['docker','inspect',name]))[0]
            result.append((name,sibling['Id'],sibling['State']['Status']))
        return tuple(sorted(result))

    def up(self) -> None:
        if not callable(self.agent_probe):
            raise PinError('Verified runtime Agent probe not configured')
        self._stack(['up','-d','--no-deps','--pull','never',self.service],timeout=240)

    def _wait(self,owner: dict,expected_image: str) -> dict:
        deadline=time.monotonic()+self.health_timeout
        while True:
            actual=self.inspect();labels=(actual.get('Config') or {}).get('Labels') or {}
            if (actual.get('Id')!=owner.get('Id') or actual.get('Image')!=expected_image
                    or labels.get('com.docker.compose.project')!=self.project
                    or labels.get('com.docker.compose.service')!=self.service):
                raise PinError('foreign Compose owner during health wait')
            state=actual.get('State') or {}
            if state.get('Running') and (state.get('Health') or {}).get('Status')=='healthy':
                return actual
            if time.monotonic()>=deadline or not state.get('Running'):
                raise PinError('Compose service did not become healthy')
            time.sleep(2)

    def accept(self,owner: dict) -> None:
        image=self.image_info(self.render(self.files[-1])['services'][self.service]['image'])
        if not callable(self.agent_probe):
            raise PinError('Verified runtime Agent probe not configured')
        if image['Id']==owner.get('Image'):
            checked=self._wait(owner,image['Id'])
            self.agent_probe(checked,self.agent_commit,self.agent_version)
        else:raise PinError('Agent candidate image changed')

    def accept_restored(self,owner: dict) -> None:
        image=self.image_info(self.render(self.files[-1])['services'][self.service]['image'])
        self._wait(owner,image['Id'])
