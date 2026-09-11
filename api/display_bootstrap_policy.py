"""Fail-closed admission while trusted bootstrap context is being implemented."""
import os
import re


class BootstrapRejected(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def open_protected_root(path, trusted_uids):
    """Walk every component no-follow; reject writable ancestors and ACL grants."""
    if (type(path) is not str or not path.startswith('/') or '\x00' in path
            or any(part in ('.', '..') for part in path.split('/'))):
        raise BootstrapRejected('INVALID_INPUT')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in [None] + [p for p in path.split('/') if p]:
            if component is not None:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            identity = os.fstat(fd)
            if identity.st_uid not in trusted_uids or identity.st_mode & 0o022:
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
            attributes = os.listxattr(fd)
            if any(name in ('system.posix_acl_access', 'system.posix_acl_default') for name in attributes):
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        result, fd = fd, None
        return result
    except OSError as exc:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN') from exc
    finally:
        if fd is not None:
            os.close(fd)


def read_protected_record(directory_fd, name, trusted_uids):
    """Bounded no-follow read; caller must hold a validated parent directory."""
    import stat
    if (type(name) is not str or not name or name in ('.', '..')
            or '/' in name or '\x00' in name):
        raise BootstrapRejected('INVALID_INPUT')
    fd = None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory_fd)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid not in trusted_uids or before.st_mode & 0o022
                or not 0 < before.st_size <= 65536
                or any(key in ('system.posix_acl_access', 'system.posix_acl_default')
                       for key in os.listxattr(fd))):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(65537)
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        fields = ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_nlink',
                  'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if (len(raw) != before.st_size or any(getattr(before, field) != getattr(other, field)
                for other in (after, named) for field in fields)):
            raise BootstrapRejected('IDENTITY_CHANGED')
        return raw
    except FileNotFoundError as exc:
        raise BootstrapRejected('POLICY_MISSING') from exc
    except OSError as exc:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN') from exc
    finally:
        if fd is not None:
            os.close(fd)


class _HeldLock:
    """Descriptor ownership; inherited children must never unlock the parent."""
    def __init__(self, fd):
        self.fd = fd
        self.pid = os.getpid()

    def close(self):
        import fcntl
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            if self.pid == os.getpid():
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __reduce__(self):
        raise TypeError('lock cannot be serialized')


def acquire_fixed_lock(directory_fd, expected_identity):
    """Low-level primitive; caller validates fixed root and root-owned anchor."""
    import fcntl
    import stat
    fd = None
    try:
        fd = os.open('bootstrap.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory_fd)
        current = os.fstat(fd)
        fields = ('dev', 'ino', 'uid', 'gid', 'mode', 'nlink')
        identity = {key: getattr(current, 'st_' + key) for key in fields}
        if (identity != expected_identity or not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1 or current.st_mode & 0o022):
            raise BootstrapRejected('IDENTITY_CHANGED')
        if any(key.startswith('system.posix_acl_') for key in os.listxattr(fd)):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BootstrapRejected('LOCK_BUSY') from exc
        named = os.stat('bootstrap.lock', dir_fd=directory_fd, follow_symlinks=False)
        if {key: getattr(named, 'st_' + key) for key in fields} != identity:
            raise BootstrapRejected('IDENTITY_CHANGED')
        result = _HeldLock(fd)
        fd = None
        return result
    except OSError as exc:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN') from exc
    finally:
        if fd is not None:
            os.close(fd)


def directory_identity(fd):
    value = os.fstat(fd)
    return {key: getattr(value, 'st_' + key) for key in ('dev', 'ino', 'uid', 'gid', 'mode')}


def open_policy_roots(config):
    """Hold roots from an already schema-validated trusted deployment policy."""
    fds = {}
    trusted = {0, config['creator_uid'], config['approver_uid']}
    try:
        for item in config['ancestors']:
            fd = open_protected_root(item['path'], trusted)
            try:
                if directory_identity(fd) != item['identity']:
                    raise BootstrapRejected('IDENTITY_CHANGED')
            finally:
                os.close(fd)
        for key, item in config['roots'].items():
            # A creator-owned ancestor can replace even a root-owned child.
            owners = ({0} if key == 'lock_root' else
                      {0, config['approver_uid']} if key == 'approval_root' else trusted)
            fds[key] = open_protected_root(item['path'], owners)
            if directory_identity(fds[key]) != item['identity']:
                raise BootstrapRejected('IDENTITY_CHANGED')
        return fds
    except BaseException:
        for fd in fds.values():
            os.close(fd)
        raise


def recheck_policy_roots(config, fds):
    """Compare both held and newly resolved identities; never update policy."""
    fresh = open_policy_roots(config)
    try:
        if set(fresh) != set(fds):
            raise BootstrapRejected('IDENTITY_CHANGED')
        for key, fd in fds.items():
            if directory_identity(fd) != directory_identity(fresh[key]):
                raise BootstrapRejected('IDENTITY_CHANGED')
    finally:
        for fd in fresh.values():
            os.close(fd)


def verify_role_permissions(fds, config):
    """Check effective OS access without creating probe files."""
    import stat
    verify_process_identity(config)
    expected = {'candidate_root', 'publish_root', 'registry_root',
                'approval_root', 'lock_root'}
    if set(fds) != expected:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
    try:
        identities = set()
        for key, fd in fds.items():
            value = os.fstat(fd)
            identities.add((value.st_dev, value.st_ino))
            protected = key in ('approval_root', 'lock_root')
            owner = (0 if key == 'lock_root' else config['approver_uid']) if protected else config['creator_uid']
            if (not stat.S_ISDIR(value.st_mode) or value.st_uid != owner
                    or value.st_mode & 0o022 or any(
                        name.startswith('system.posix_acl_') for name in os.listxattr(fd))):
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
            if not os.access('.', os.R_OK | os.X_OK, dir_fd=fd, effective_ids=True):
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
            writable = os.access('.', os.W_OK, dir_fd=fd, effective_ids=True)
            if writable == protected:
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        if len(identities) != len(expected):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
    except OSError as exc:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN') from exc


def verify_process_identity(config):
    """Reject root, saved-ID escalation, extra groups and user namespaces."""
    from pathlib import Path
    try:
        uid = config['creator_uid']
        if (type(uid) is not int or uid <= 0 or uid == config['approver_uid']
                or os.getresuid() != (uid, uid, uid)):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        gid = os.getgid()
        if os.getresgid() != (gid, gid, gid) or set(os.getgroups()) - {gid}:
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        status = dict(line.split(':', 1) for line in
                      Path('/proc/self/status').read_text().splitlines() if ':' in line)
        caps = [int(status[key].strip(), 16) for key in
                ('CapInh', 'CapPrm', 'CapEff', 'CapAmb')]
        if any(caps) or [int(x) for x in status['Uid'].split()] != [uid] * 4:
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        if [int(x) for x in status['Gid'].split()] != [gid] * 4:
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        for name in ('uid_map', 'gid_map'):
            mapping = Path('/proc/self/' + name).read_text().split()
            if mapping != ['0', '0', '4294967295']:
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        return {'uid': uid, 'gid': gid, 'capabilities': 0,
                'user_namespace': os.readlink('/proc/self/ns/user')}
    except (OSError, KeyError, ValueError) as exc:
        if isinstance(exc, BootstrapRejected):
            raise
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN') from exc


def verify_memory_limit(max_bytes):
    """Inspect this process's cgroup v2, never trust an environment assertion."""
    from pathlib import Path
    if type(max_bytes) is not int or not 0 < max_bytes <= 9007199254740991:
        raise BootstrapRejected('INVALID_INPUT')
    try:
        lines = Path('/proc/self/cgroup').read_text().splitlines()
        if len(lines) != 1 or not lines[0].startswith('0::/'):
            raise BootstrapRejected('UNSUPPORTED_PLATFORM')
        relative = lines[0][3:]
        if any(part in ('.', '..') for part in relative.split('/')):
            raise BootstrapRejected('UNSUPPORTED_PLATFORM')
        group = Path('/sys/fs/cgroup') / relative.lstrip('/')
        memory = (group / 'memory.max').read_text().strip()
        swap = (group / 'memory.swap.max').read_text().strip()
        if memory == 'max' or not 0 < int(memory) <= max_bytes or swap != '0':
            raise BootstrapRejected('RESOURCE_LIMIT')
        if os.access(group / 'memory.max', os.W_OK) and os.geteuid() != 0:
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        return dict(cgroup=relative, memory_max=int(memory), swap_max=0)
    except (OSError, ValueError) as exc:
        if isinstance(exc, BootstrapRejected):
            raise
        raise BootstrapRejected('RESOURCE_LIMIT') from exc


def read_resource_policy(root_fd, config):
    """Load root-owned resource policy through no-follow directory handles."""
    from api.display_bootstrap_manifest import parse_record, validate_record, digest
    resource_id = config['resource_policy_id']
    if type(resource_id) is not str or re.fullmatch('[0-9a-f]{32}', resource_id) is None:
        raise BootstrapRejected('INVALID_INPUT')
    fds = []
    try:
        current = root_fd
        for name in ('policies', 'resources'):
            current = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            fds.append(current)
            identity = os.fstat(current)
            if identity.st_uid != 0 or identity.st_mode & 0o022 or any(
                    key.startswith('system.posix_acl_') for key in os.listxattr(current)):
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        raw = read_protected_record(current, resource_id + '.json', {0})
        if digest(raw) != config['resource_policy_sha']:
            raise BootstrapRejected('APPROVAL_MISMATCH')
        return validate_record(parse_record(raw), 'resource')
    except BootstrapRejected:
        raise
    except OSError as exc:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN') from exc
    except ValueError as exc:
        raise BootstrapRejected('INVALID_INPUT') from exc
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _validate_deployment(value):
    from api.display_bootstrap_manifest import validate_record
    return validate_record(value, 'deployment')


def read_active_policy(root_fd, policy_id):
    """Read current ID and exact bytes under caller's fixed global lock."""
    from api.display_bootstrap_manifest import parse_record, validate_record, digest
    if type(policy_id) is not str or re.fullmatch('[0-9a-f]{32}', policy_id) is None:
        raise BootstrapRejected('INVALID_INPUT')
    policies_fd = None
    try:
        active = validate_record(parse_record(read_protected_record(root_fd, 'active.json', {0})), 'active')
        if active['policy_id'] != policy_id:
            raise BootstrapRejected('APPROVAL_MISMATCH')
        policies_fd = os.open('policies', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        identity = os.fstat(policies_fd)
        if (identity.st_uid != 0 or identity.st_mode & 0o022
                or any(key.startswith('system.posix_acl_') for key in os.listxattr(policies_fd))):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        raw = read_protected_record(policies_fd, policy_id + '.json', {0})
        if digest(raw) != active['deployment_policy_sha']:
            raise BootstrapRejected('APPROVAL_MISMATCH')
        value = _validate_deployment(parse_record(raw))
        return value, active['deployment_policy_sha']
    except BootstrapRejected:
        raise
    except OSError as exc:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN') from exc
    except ValueError as exc:
        raise BootstrapRejected('INVALID_INPUT') from exc
    finally:
        if policies_fd is not None:
            os.close(policies_fd)


def observe_platform(directory_fd):
    """Read mount identity from the held FD and explicitly select SQLite unix VFS."""
    from pathlib import Path
    import sqlite3
    import sys
    if sys.platform != 'linux':
        raise BootstrapRejected('UNSUPPORTED_PLATFORM')
    try:
        fields = dict(line.split(':', 1) for line in
                      Path(f'/proc/self/fdinfo/{directory_fd}').read_text().splitlines())
        mount_id = int(fields['mnt_id'])
        rows = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
        matches = [row for row in rows if int(row[0]) == mount_id]
        if len(matches) != 1:
            raise BootstrapRejected('UNSUPPORTED_PLATFORM')
        row = matches[0]
        divider = row.index('-')
        filesystem = row[divider+1]
        options = sorted(set(row[5].split(',') + row[divider+3].split(',')))
        if filesystem not in ('ext4', 'xfs') or 'rw' not in options:
            raise BootstrapRejected('UNSUPPORTED_PLATFORM')
        connection = sqlite3.connect('file::memory:?vfs=unix', uri=True)
        try:
            compile_options = sorted({r[0] for r in connection.execute('PRAGMA compile_options')})
        finally:
            connection.close()
        return dict(kernel=os.uname().release, sqlite_version=sqlite3.sqlite_version,
                    compile_options=compile_options, vfs='unix', filesystem=filesystem,
                    mount_id=mount_id, mount_options=options,
                    namespace_id=os.readlink('/proc/self/ns/mnt'))
    except (OSError, KeyError, ValueError, sqlite3.Error) as exc:
        if isinstance(exc, BootstrapRejected):
            raise
        raise BootstrapRejected('UNSUPPORTED_PLATFORM') from exc


def verify_platform(directory_fd, expected):
    actual = observe_platform(directory_fd)
    if actual != expected:
        raise BootstrapRejected('UNSUPPORTED_PLATFORM')
    return actual


def verify_anchor_binding(root_fd, anchor, config):
    """A policy cannot select a different lock or substitute its root."""
    expected = config['roots']['lock_root']
    if (expected['path'] != '/etc/hermes-display-bootstrap'
            or expected['identity'] != directory_identity(root_fd)
            or config['lock_identity'] != anchor['lock_identity']):
        raise BootstrapRejected('IDENTITY_CHANGED')
    named = os.stat('bootstrap.lock', dir_fd=root_fd, follow_symlinks=False)
    if {k: getattr(named, 'st_' + k) for k in
            ('dev', 'ino', 'uid', 'gid', 'mode', 'nlink')} != anchor['lock_identity']:
        raise BootstrapRejected('IDENTITY_CHANGED')


class BootstrapContext:
    """Process-local ownership, issued only after trusted admission succeeds."""
    def __init__(self):
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')

    @classmethod
    def _issue(cls, receipt):
        from api.display_bootstrap_admission import issue
        return issue(receipt)

    def revalidate(self):
        """Recheck held authority; incomplete volume admission remains blocked."""
        self.check_owner()
        if hasattr(self, '_admission_owner'):
            from api.display_bootstrap_admission_checks import verify_common
            verify_common(config=self.config, resource=self.resource, roots=self.roots,
                storage=self.storage_handle, release=self.release_handle, registry_fd=self.audit_fd,
                candidate_id=self.candidate_id, boundary=self.audit_boundary,
                role=self.role, deadline=self.operation_deadline())
            self.verify_candidate_quota(self.candidate_id, self.candidate_fd)
            return
        from api.display_bootstrap_manifest import parse_record, validate_record
        try:
            active = validate_record(parse_record(read_protected_record(
                self.root_fd, 'active.json', {0})), 'active')
            config, sha = read_active_policy(self.root_fd, active['policy_id'])
            if config != self.config or sha != self.deployment_sha:
                raise BootstrapRejected('APPROVAL_MISMATCH')
            anchor = validate_record(parse_record(read_protected_record(
                self.root_fd, 'anchor.json', {0})), 'anchor')
            if self.lock.pid != os.getpid() or self.lock.fd is None:
                raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
            held = os.fstat(self.lock.fd)
            if {k: getattr(held, 'st_' + k) for k in anchor['lock_identity']} != anchor['lock_identity']:
                raise BootstrapRejected('IDENTITY_CHANGED')
            verify_anchor_binding(self.root_fd, anchor, config)
            if config.get('format_version') in (2, 3):
                from api.display_bootstrap_roles import verify_dac_configuration
                confined = (getattr(self, 'creation_confined', False)
                            or getattr(self, 'publication_confined', False))
                verify_dac_configuration(self.roots, config, role=self.role,
                                         before_confinement=not confined)
                if confined:
                    receipt = getattr(self, 'audit_boundary', None)
                    if type(receipt) is not dict:
                        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
                    audit_fd = os.open(receipt['candidate_id'],
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=self.roots['registry_root'])
                    try:
                        self.verify_audit_boundary(audit_fd, receipt['candidate_id'])
                    finally:
                        os.close(audit_fd)
            else:
                verify_process_identity(config)
                verify_role_permissions(self.roots, config)
            recheck_policy_roots(config, self.roots)
            verify_platform(self.roots['candidate_root'], config['platform_requirements'])
            if read_resource_policy(self.root_fd, config) != self.resource:
                raise BootstrapRejected('APPROVAL_MISMATCH')
            from api.display_bootstrap_runner import verify_systemd_runner
            verify_systemd_runner(self.resource['hard_limit_profile_id'],
                max_rss_bytes=self.resource['max_rss_bytes'],
                max_elapsed_seconds=self.resource['max_elapsed_seconds'],
                config=config if config.get('format_version') == 2 else None,
                role=self.role if config.get('format_version') == 2 else None)
            from api.display_bootstrap_volume import verify_fixed_volumes
            verify_fixed_volumes(self.roots, self.resource)
            # Fixed-volume identity does not prove reserved audit metadata.
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        except BootstrapRejected:
            raise
        except (OSError, ValueError, AttributeError, KeyError) as exc:
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN') from exc

    def operation_deadline(self):
        """One cooperative budget per context; systemd remains the hard stop."""
        import math
        import time
        self.check_owner()
        start = getattr(self, 'started_monotonic', None)
        seconds = self.resource.get('max_elapsed_seconds')
        if (type(start) not in (int, float) or not math.isfinite(start)
                or type(seconds) is not int or not 0 < seconds <= 9007199254740991):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        now = time.monotonic()
        deadline = start + seconds
        if now < start:
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        if now >= deadline:
            raise BootstrapRejected('RESOURCE_LIMIT')
        return deadline

    def verify_audit_boundary(self, registry_fd, candidate_id):
        """Bind a live fixed-file restriction to this candidate and process."""
        self.check_owner()
        receipt = getattr(self, 'audit_boundary', None)
        if (type(receipt) is not dict or receipt.get('pid') != os.getpid()
                or receipt.get('candidate_id') != candidate_id
                or receipt.get('registry_identity') != directory_identity(registry_fd)):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        audit = os.stat('audit.bin', dir_fd=registry_fd, follow_symlinks=False)
        current = {k: getattr(audit, 'st_' + k) for k in
                   ('dev', 'ino', 'uid', 'gid', 'mode', 'nlink')}
        if current != receipt['audit_identity']:
            raise BootstrapRejected('IDENTITY_CHANGED')
        from api.display_bootstrap_boundary_probe import probe
        from api.display_bootstrap_seccomp import verify_quota_guard
        verify_quota_guard()
        probe(self.config, denied=True, deadline=self.operation_deadline())
        return receipt

    def restrict_fixed_writes(self, registry_fd, candidate_id, *, candidate_fd=None):
        self.check_owner()
        if (self.config.get('format_version') != 3
                or getattr(self, 'creation_confined', False)
                or getattr(self, 'publication_confined', False)
                or type(candidate_id) is not str
                or re.fullmatch('[0-9a-f]{32}', candidate_id) is None):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        from api.display_bootstrap_boundary_probe import probe
        from api.display_bootstrap_write_guard import install_fixed_boundary
        deadline = self.operation_deadline()
        probe(self.config, denied=False, deadline=deadline)
        creator = candidate_fd is not None
        if creator:
            self.creation_confined = True
        else:
            self.publication_confined = True
        before = os.stat('audit.bin', dir_fd=registry_fd, follow_symlinks=False)
        installed = install_fixed_boundary(
            candidate_fd if creator else self.roots['candidate_root'], registry_fd,
            publish_fd=None if creator else self.roots['publish_root'])
        receipt = dict(pid=os.getpid(), candidate_id=candidate_id,
                       registry_identity=directory_identity(registry_fd),
                       audit_identity={k: getattr(before, 'st_' + k) for k in
                           ('dev', 'ino', 'uid', 'gid', 'mode', 'nlink')},
                       installed=installed)
        self.audit_boundary = receipt
        try:
            self.verify_audit_boundary(registry_fd, candidate_id)
        except BaseException:
            self.audit_boundary = None
            raise
        return receipt

    def restrict_publication_writes(self, registry_fd):
        self.check_owner()
        if getattr(self, 'creation_confined', False):
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
        from api.display_bootstrap_write_guard import install_publication_boundary
        # Mark before installation: partial restriction must never permit retry
        # as a creator. Kernel restrictions, not these flags, enforce access.
        self.publication_confined = True
        return install_publication_boundary(self.roots['candidate_root'],
                                            self.roots['publish_root'], registry_fd)

    def restrict_creation_writes(self, candidate_fd, registry_fd):
        self.check_owner()
        from api.display_bootstrap_write_guard import install_creation_boundary
        self.creation_confined = True
        return install_creation_boundary(candidate_fd, registry_fd)

    def prepare_candidate_quota(self, cid, directory_fd):
        self.check_owner()
        from api.display_bootstrap_quota import candidate_quota
        return candidate_quota(self, cid, directory_fd, allocate=True)

    def verify_candidate_quota(self, cid, directory_fd):
        self.check_owner()
        from api.display_bootstrap_quota import candidate_quota
        return candidate_quota(self, cid, directory_fd)

    def check_owner(self):
        if self.closed or self.pid != os.getpid():
            raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')

    def __enter__(self):
        self.check_owner()
        return self

    def __exit__(self, *args):
        self.close()

    def __reduce__(self):
        raise TypeError('bootstrap context cannot be serialized')

    def close(self):
        if self.closed:
            return
        self.closed = True
        if hasattr(self, "_admission_owner"):
            self._admission_owner.close()
            return
        try:
            for fd in self.roots.values():
                try:
                    os.close(fd)
                except OSError:
                    pass
        finally:
            try:
                self.lock.close()
            finally:
                os.close(self.root_fd)


def acquire_bootstrap_context(policy_id, *, role='creator'):
    if (type(policy_id) is not str or re.fullmatch('[0-9a-f]{32}',policy_id) is None
            or role not in ('creator','publisher','recover')):
        raise BootstrapRejected('INVALID_INPUT')
    raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
