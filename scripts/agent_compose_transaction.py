"""Atomic image-only pin of one Compose service; caller owns stack validation.

Only a literal image line in a uniquely named service is supported. This
module does not run Compose or grant updater access to host configuration.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Callable

_IMAGE = re.compile(r'[a-z0-9._:/-]+@sha256:[0-9a-f]{64}\Z')
_SERVICE = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z')


class PinError(ValueError):
    pass


@contextmanager
def pin_lock(path: Path):
    """Hold an advisory lock across baseline, image pin and rollback.

    The operator supplies a protected parent directory. This lock cannot
    arbitrate against unrelated tools that ignore it; source/stack CAS still
    rejects their changed files.
    """
    path = Path(path)
    lock_path = path.with_name(path.name + '.agent-pin.lock')
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.geteuid()):
            raise PinError('Compose pin lock is not protected')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PinError('Compose pin is locked by another updater') from exc
        yield
    finally:
        os.close(fd)


@dataclass(frozen=True)
class PinPlan:
    path: Path
    previous: bytes
    updated: bytes
    previous_sha256: str
    expected: dict
    baseline: dict
    mode: int
    uid: int
    gid: int


def _read(path: Path) -> tuple[bytes, os.stat_result]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise PinError('Cannot read protected Compose config') from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            raise PinError('Compose config must be a protected regular file')
        if info.st_size <= 0 or info.st_size > 1024 * 1024:
            raise PinError('Invalid Compose config size')
        contents = os.read(fd, info.st_size + 1)
        if len(contents) != info.st_size:
            raise PinError('Compose config changed during read')
        return contents, info
    finally:
        os.close(fd)


def _replace_service_image(raw: bytes, service: str, old: str, new: str) -> bytes:
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise PinError('Compose config must be UTF-8') from exc
    lines = text.splitlines(keepends=True)
    top = next((i for i, line in enumerate(lines) if line.rstrip() == 'services:'), None)
    if top is None or sum(line.rstrip() == 'services:' for line in lines) != 1:
        raise PinError('Ambiguous Compose services block')
    service_line = re.compile(r'^([ \t]+)' + re.escape(service) + r':\s*$')
    matches = []
    for i in range(top + 1, len(lines)):
        line = lines[i]
        if line.strip() and not line.lstrip().startswith('#') and not line[0].isspace():
            break
        m = service_line.fullmatch(line.rstrip('\r\n'))
        if m:
            matches.append((i, len(m.group(1))))
    if len(matches) != 1:
        raise PinError('Compose service is absent or ambiguous')
    start, depth = matches[0]
    image_lines = []
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        indent = len(line) - len(line.lstrip(' \t'))
        if indent <= depth:
            break
        if re.match(r'^\s+image:', line) and indent == depth + 2:
            image_lines.append(i)
    if len(image_lines) != 1:
        raise PinError('Compose service image is absent or ambiguous')
    i = image_lines[0]
    m = re.fullmatch(r'''(\s+image: )(?:(['"])([a-z0-9._:/@-]+)\2|([a-z0-9._:/@-]+))(\r?\n?)''', lines[i])
    if m is None or (m.group(3) or m.group(4)) != old:
        raise PinError('Compose image is not the accepted literal digest')
    quote = m.group(2) or ''
    lines[i] = m.group(1) + quote + new + quote + m.group(5)
    return ''.join(lines).encode('utf-8')


def _prepare_pin(path: Path, service: str, old: str, new: str,
                 before: dict, render: Callable[[Path], dict]) -> PinPlan:
    if not _SERVICE.fullmatch(service) or not _IMAGE.fullmatch(new) or old == new:
        raise PinError('Invalid image-only transaction')
    path = Path(path)
    raw, info = _read(path)
    if render(path) != before or before.get('services', {}).get(service, {}).get('image') != old:
        raise PinError('Effective Compose baseline changed')
    updated = _replace_service_image(raw, service, old, new)
    # The renderer must consume the complete ordered stack with this file
    # substituted at the same position. It may use a temporary candidate file.
    with tempfile.TemporaryDirectory(prefix='.agent-pin-check-', dir=path.parent) as directory:
        candidate = Path(directory) / path.name
        candidate.write_bytes(updated)
        effective = render(candidate)
    expected = copy.deepcopy(before)
    expected['services'][service]['image'] = new
    if effective != expected:
        raise PinError('Compose candidate changes other effective configuration')
    return PinPlan(path, raw, updated, hashlib.sha256(raw).hexdigest(), expected, before,
                   stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)


def prepare_pin(path: Path, service: str, old: str, new: str,
                before: dict, render: Callable[[Path], dict]) -> PinPlan:
    if not _IMAGE.fullmatch(old):
        raise PinError('Old image is not an immutable digest')
    return _prepare_pin(path, service, old, new, before, render)


def prepare_baseline_pin(path: Path, service: str, old_tag: str, digest: str,
                         before: dict, render: Callable[[Path], dict], *,
                         live: dict, image: dict) -> PinPlan:
    """One-time tag-to-digest pin, only if the tag resolves to the live image."""
    if (not re.fullmatch(r'[a-z0-9._:/-]+:v[0-9][0-9A-Za-z_.-]*', old_tag)
            or not _IMAGE.fullmatch(digest)
            or old_tag.rsplit(':', 1)[0] != digest.split('@', 1)[0]
            or image.get('RepoDigests') is None or digest not in image['RepoDigests']
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(image.get('Id') or ''))
            or live.get('Image') != image['Id']
            or not (live.get('State') or {}).get('Running')):
        raise PinError('Old tag is not verified against the running image')
    return _prepare_pin(path, service, old_tag, digest, before, render)


def apply_pin(plan: PinPlan, render: Callable[[Path], dict]) -> None:
    original, info = _read(plan.path)
    if (original != plan.previous or stat.S_IMODE(info.st_mode) != plan.mode
            or (info.st_uid, info.st_gid) != (plan.uid, plan.gid)
            or render(plan.path) != plan.baseline):
        raise PinError('Compose source or effective stack changed after preflight')
    fd, tmp = tempfile.mkstemp(prefix='.agent-pin-', dir=plan.path.parent)
    try:
        os.fchmod(fd, plan.mode)
        os.fchown(fd, plan.uid, plan.gid)
        with os.fdopen(fd, 'wb') as output:
            output.write(plan.updated)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, plan.path)
        directory = os.open(plan.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def verify_pin(plan: PinPlan, render: Callable[[Path], dict]) -> bool:
    contents, _ = _read(plan.path)
    if contents != plan.updated or render(plan.path) != plan.expected:
        raise PinError('Effective Compose image pin did not persist')
    return True


def restore_pin(plan: PinPlan, render: Callable[[Path], dict]) -> bool:
    """Reverse this exact transaction, refusing another operator's changes."""
    contents, info = _read(plan.path)
    if (contents != plan.updated or render(plan.path) != plan.expected
            or stat.S_IMODE(info.st_mode) != plan.mode
            or (info.st_uid, info.st_gid) != (plan.uid, plan.gid)):
        raise PinError('Compose target changed after pin; manual recovery required')
    reverse = PinPlan(plan.path, plan.updated, plan.previous,
                      hashlib.sha256(plan.updated).hexdigest(), plan.baseline,
                      plan.expected, plan.mode, plan.uid, plan.gid)
    apply_pin(reverse, render)
    return verify_pin(reverse, render)
