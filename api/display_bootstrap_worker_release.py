"""Verify immutable worker bytes against raw Git objects before confinement."""
import configparser
import hashlib
import os
import re
import selectors
import stat
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from api.display_bootstrap_audit_log import _deadline
from api.display_bootstrap_policy import open_protected_root, directory_identity

_ENV = dict(PATH='/usr/bin:/bin', LC_ALL='C', GIT_CONFIG_NOSYSTEM='1',
            GIT_CONFIG_GLOBAL='/dev/null', GIT_NO_REPLACE_OBJECTS='1', GIT_OPTIONAL_LOCKS='0')
_FIELDS = ('dev','ino','uid','gid','mode','nlink','size','mtime_ns','ctime_ns')
_LIMIT = 16 * 1024 * 1024


class WorkerRelease:
    def __init__(self, deadline):
        self.deadline, self.pid = deadline, os.getpid()
        self._stack, self._directories, self._files = ExitStack(), {}, {}
        self.closed = False
        try:
            self.root = Path(__file__).absolute().parent.parent
            if (Path(sys.argv[0]).absolute() != self.root / 'scripts/bootstrap_worker_entry.py'
                    or not sys.flags.isolated or not sys.dont_write_bytecode):
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
            self._directory(self.root)
            for path in sys.path:
                item = Path(path)
                if item == self.root:
                    continue
                # -I -S is not required: installed system libraries are trusted,
                # but another project import root is never accepted.
                if not item.is_absolute() or not any(str(item).startswith(prefix) for prefix in
                        (sys.base_prefix + '/lib/', sys.base_exec_prefix + '/lib/')):
                    raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
                self._directory(item if item.is_dir() else item.parent)
            self._directory(self.root / '.git')
            for name in ('commondir','objects/info/alternates','info/grafts','refs/replace','worktrees'):
                if os.path.lexists(self.root / '.git' / name):
                    raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
            config = self._read('.git/config').decode('utf-8')
            parser = configparser.RawConfigParser(strict=True)
            parser.read_string(config)
            for section in parser.sections():
                if section not in ('core','extensions') and not section.startswith(('remote ', 'branch ')):
                    raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
                for key in parser[section]:
                    if key not in {'repositoryformatversion','filemode','bare','logallrefupdates',
                                   'objectformat','url','fetch','remote','merge'}:
                        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
            if parser.getboolean('core','bare',fallback=False):
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
            self.algorithm = parser.get('extensions','objectformat',fallback='sha1')
            if self.algorithm not in ('sha1','sha256'):
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
            head = self._read('.git/HEAD').decode('ascii').strip()
            if head.startswith('ref: '):
                ref = head[5:]
                if (not ref.startswith('refs/heads/') or any(p in ('','.','..') for p in ref.split('/'))
                        or re.fullmatch(r'[A-Za-z0-9_./-]+', ref) is None):
                    raise ValueError('INVALID_INPUT')
                if (self.root / '.git' / ref).exists():
                    head = self._read('.git/' + ref).decode('ascii').strip()
                else:
                    rows = self._read('.git/packed-refs').decode('ascii').splitlines()
                    found = [row.split()[0] for row in rows if not row.startswith(('#','^'))
                             and len(row.split()) == 2 and row.split()[1] == ref]
                    if len(found) != 1:
                        raise ValueError('APPROVAL_MISMATCH')
                    head = found[0]
            self._oid(head)
            self._commit = head
            commit = self._object('commit', head)
            first = commit.split(b'\n',1)[0]
            if not first.startswith(b'tree '):
                raise ValueError('INVALID_INPUT')
            self._tracked = {}
            self._tree(first[5:].decode('ascii'), '')
            self._members = self._inventory()
            self._verify_coverage()
            for relative in self._members:
                if relative not in self._tracked:
                    raise ValueError('APPROVAL_MISMATCH')
                mode, oid = self._tracked[relative]
                raw = self._read(relative)
                if raw != self._object('blob', oid):
                    raise ValueError('APPROVAL_MISMATCH')
                executable = bool(self._files[relative][0][4] & 0o111)
                if executable != (mode == '100755'):
                    raise ValueError('APPROVAL_MISMATCH')
            self.revalidate()
        except BaseException:
            self.close()
            raise

    def _directory(self, path):
        _deadline(self.deadline)
        path = str(path)
        if path not in self._directories:
            fd = open_protected_root(path, {0})
            self._stack.callback(os.close, fd)
            self._directories[path] = (fd, directory_identity(fd))
        return self._directories[path][0]

    def _read(self, relative):
        path = self.root / relative
        parent = self._directory(path.parent)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_nlink != 1
                    or before.st_mode & 0o022 or before.st_size > _LIMIT or os.listxattr(fd)):
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
            chunks, size = [], 0
            while True:
                _deadline(self.deadline)
                chunk = os.read(fd, min(65536, _LIMIT+1-size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > _LIMIT:
                    raise ValueError('RESOURCE_LIMIT')
            identity = tuple(getattr(before,'st_'+key) for key in _FIELDS)
            named = os.stat(path.name,dir_fd=parent,follow_symlinks=False)
            if any(tuple(getattr(v,'st_'+key) for key in _FIELDS) != identity
                   for v in (named,os.fstat(fd))) or size != before.st_size:
                raise ValueError('IDENTITY_CHANGED')
            raw = b''.join(chunks)
            snap = identity, hashlib.sha256(raw).hexdigest()
            if relative in self._files and self._files[relative] != snap:
                raise ValueError('IDENTITY_CHANGED')
            self._files[relative] = snap
            return raw
        finally:
            os.close(fd)

    def _git(self, *args):
        _deadline(self.deadline)
        command = ['/usr/bin/git', '--git-dir='+str(self.root/'.git'),
                   '--work-tree='+str(self.root), *args]
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=_ENV, cwd=self.root, close_fds=True) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout,selectors.EVENT_READ,'out')
                    selector.register(process.stderr,selectors.EVENT_READ,'err')
                    chunks, size = [], 0
                    while selector.get_map():
                        _deadline(self.deadline)
                        for key,_ in selector.select(max(0,self.deadline-time.monotonic())):
                            raw = os.read(key.fileobj.fileno(),65536)
                            if not raw:
                                selector.unregister(key.fileobj)
                                continue
                            size += len(raw)
                            if size > _LIMIT:
                                raise ValueError('RESOURCE_LIMIT')
                            if key.data == 'out':
                                chunks.append(raw)
                    if process.wait(timeout=max(.001,self.deadline-time.monotonic())) != 0:
                        raise ValueError('APPROVAL_MISMATCH')
                    return b''.join(chunks)
            except BaseException:
                process.kill()
                process.wait()
                raise

    def _oid(self, value):
        if re.fullmatch('[0-9a-f]{%d}' % (40 if self.algorithm=='sha1' else 64),value) is None:
            raise ValueError('INVALID_INPUT')

    def _object(self, kind, oid):
        self._oid(oid)
        raw = self._git('cat-file',kind,oid)
        encoded = kind.encode()+b' '+str(len(raw)).encode()+b'\0'+raw
        if hashlib.new(self.algorithm,encoded).hexdigest() != oid:
            raise ValueError('APPROVAL_MISMATCH')
        return raw

    def _tree(self, oid, prefix, depth=0):
        if depth > 32 or len(self._tracked) > 10000:
            raise ValueError('RESOURCE_LIMIT')
        raw = self._object('tree',oid)
        pos, width = 0, 20 if self.algorithm=='sha1' else 32
        names = set()
        while pos < len(raw):
            end = raw.index(b'\0',pos)
            mode, name = raw[pos:end].split(b' ',1)
            name = name.decode('utf-8')
            if name in ('','.','..') or '/' in name or name in names:
                raise ValueError('INVALID_INPUT')
            names.add(name)
            child = raw[end+1:end+1+width]
            if len(child) != width:
                raise ValueError('INVALID_INPUT')
            pos = end+1+width
            relative = prefix+name
            if mode == b'40000':
                self._tree(child.hex(),relative+'/',depth+1)
            elif mode in (b'100644',b'100755'):
                self._tracked[relative] = mode.decode(),child.hex()
            else:
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')

    def _verify_coverage(self):
        expected = {name for name in self._tracked if name.startswith(('api/', 'scripts/'))}
        if self._members != expected:
            raise ValueError('APPROVAL_MISMATCH')

    def _inventory(self):
        result, pending = set(), [self.root/'api',self.root/'scripts']
        while pending:
            path = pending.pop()
            fd = self._directory(path)
            with os.scandir(fd) as entries:
                for entry in entries:
                    _deadline(self.deadline)
                    if entry.name in ('.git','__pycache__'):
                        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(path/entry.name)
                    elif stat.S_ISREG(info.st_mode):
                        if entry.name.endswith(('.so','.pyc','.pyo')):
                            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
                        result.add(str((path/entry.name).relative_to(self.root)))
                    else:
                        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
                    if len(result)+len(pending) > 10000:
                        raise ValueError('RESOURCE_LIMIT')
        return frozenset(result)

    @property
    def commit(self):
        if self.closed or self.pid != os.getpid():
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        return self._commit

    def revalidate(self):
        self.commit
        for path,(held,identity) in list(self._directories.items()):
            _deadline(self.deadline)
            fresh = open_protected_root(path,{0})
            try:
                if directory_identity(fresh) != identity or directory_identity(held) != identity:
                    raise ValueError('IDENTITY_CHANGED')
            finally:
                os.close(fresh)
        if self._inventory() != self._members:
            raise ValueError('IDENTITY_CHANGED')
        for relative in tuple(self._files):
            self._read(relative)
        _deadline(self.deadline)

    def close(self):
        self.closed = True
        self._stack.close()


@contextmanager
def hold_worker_release(*, deadline):
    handle = WorkerRelease(deadline)
    try:
        yield handle
    finally:
        handle.close()
