"""Validate an explicitly accepted release digest; never issues approval."""
import hashlib
import json
import os
import stat
from pathlib import Path
from api.application_task_runner import RunnerBlocked


def verify_release(root, accepted_sha):
    root = Path(root)
    if type(accepted_sha) is not str or len(accepted_sha) != 64 or any(c not in '0123456789abcdef' for c in accepted_sha):
        raise RunnerBlocked('RELEASE_ACCEPTANCE_REQUIRED')
    def trusted_directory(path):
        if not path.is_absolute() or '..' in path.parts:
            raise RunnerBlocked('RELEASE_PATH_UNTRUSTED')
        for parent in (path, *path.parents):
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise RunnerBlocked('RELEASE_PATH_UNTRUSTED')
    trusted_directory(root)
    def read(path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_size > 16777216:
                raise RunnerBlocked('RELEASE_FILE_UNTRUSTED')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                return stream.read(16777217)
        finally:
            os.close(fd)
    raw = read(root / 'release-manifest.json')
    if hashlib.sha256(raw).hexdigest() != accepted_sha:
        raise RunnerBlocked('RELEASE_SHA')
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result: raise RunnerBlocked('RELEASE_DUPLICATE_KEY')
            result[key]=value
        return result
    manifest=json.loads(raw,object_pairs_hook=unique)
    if set(manifest) != {'status','files','interpreter','interpreter_sha','argv'} or not isinstance(manifest['files'],dict) or not manifest['files'] or len(manifest['files'])>10000:
        raise RunnerBlocked('RELEASE_SCHEMA')
    actual=set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            info=(Path(directory)/name).lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise RunnerBlocked('RELEASE_PATH_UNTRUSTED')
        for name in files:
            rel=str((Path(directory)/name).relative_to(root))
            if rel != 'release-manifest.json': actual.add(rel)
        if len(actual)>10000: raise RunnerBlocked('RELEASE_FILE_LIMIT')
    if actual != set(manifest['files']): raise RunnerBlocked('RELEASE_FILE_SET')
    for name,sha in manifest['files'].items():
        if hashlib.sha256(read(root/name)).hexdigest()!=sha:
            raise RunnerBlocked('RELEASE_CONTENT_SHA')
    interpreter=Path(manifest['interpreter'])
    if not interpreter.is_absolute() or interpreter.resolve()!=interpreter:
        raise RunnerBlocked('RELEASE_INTERPRETER')
    trusted_directory(interpreter.parent)
    if hashlib.sha256(read(interpreter)).hexdigest()!=manifest['interpreter_sha']:
        raise RunnerBlocked('RELEASE_INTERPRETER_SHA')
    expected=[str(interpreter),'-I','-B',str(root/'scripts/application_fixed_policy_test.py')]
    if manifest['argv']!=expected: raise RunnerBlocked('RELEASE_ARGV')
    return tuple(expected)
