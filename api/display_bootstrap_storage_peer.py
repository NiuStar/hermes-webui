"""R3 observer identity: kernel sender + pidfd + host system manager."""
import os
import select
import re
from contextlib import contextmanager
from api import display_bootstrap_systemd_bus as bus
from api.display_bootstrap_observer_release import read_observer
from api.display_bootstrap_policy import open_protected_root
from api.display_bootstrap_audit_log import _deadline


def snapshot(pid, profile, release, deadline):
    path=bus.unit_for_pid(pid,deadline)
    def prop(interface,name,signature):
        return bus.property_value(path,interface,name,signature,deadline)
    unit=prop('Unit','Id','s')
    if re.fullmatch(r'hermes-bootstrap-storage@[^/\x00]+\.service',unit) is None:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    basic={name:prop('Unit',name,'s') for name in ('LoadState','ActiveState','SubState','ControlGroup','FragmentPath')}
    if (basic['LoadState'],basic['ActiveState'],basic['SubState'])!=('loaded','active','running'):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    if prop('Service','MainPID','u')!=pid:
        raise ValueError('IDENTITY_CHANGED')
    invocation=prop('Unit','InvocationID','ay')
    if len(invocation)!=16 or not any(invocation):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    drops=prop('Unit','DropInPaths','as')
    if drops:
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    approved={release['release_root']+'/'+item['path'] for item in release['files']}
    if basic['FragmentPath'] not in approved or not basic['FragmentPath'].endswith('/'+profile['unit_template']):
        raise ValueError('APPROVAL_MISMATCH')
    if profile['socket_unit'] not in prop('Unit','TriggeredBy','as'):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    signatures={'Type':'s','User':'s','Group':'s','Restart':'s','Delegate':'b',
        'NoNewPrivileges':'b','KillMode':'s','KillSignal':'i','FinalKillSignal':'i',
        'SendSIGKILL':'b','ProtectSystem':'s','ProtectHome':'s','PrivateTmp':'b',
        'PrivateDevices':'b','RestrictAddressFamilies':'(bas)','UMask':'u'}
    values={}
    for name,expected in profile['service_properties'].items():
        value=prop('Service',name,signatures[name])
        if name in ('User','Group'):
            # Unit generator explicitly sets root, not an empty implicit default.
            if value not in ('root','0'): raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        elif name=='RestrictAddressFamilies':
            if (type(value) is not list or len(value) != 2 or value[0] is not True
                    or value[1] != expected):
                raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        elif name=='UMask':
            if value!=int(expected,8): raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        elif value!=expected:
            raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
        values[name]=value
    executable=profile['python_executable_identity']['path']
    expected_argv=[executable,'-I','-B',release['release_root']+'/scripts/bootstrap_storage_entry.py']
    commands=prop('Service','ExecStart','a(sasbttttuii)')
    if (type(commands) is not list or len(commands)!=1 or type(commands[0]) is not list
            or len(commands[0])!=len(('s','as','b','t','t','t','t','u','i','i')) or commands[0][0]!=executable
            or commands[0][1]!=expected_argv or commands[0][2] is not False):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    limits=profile['resource_limits']
    for name,key in (('MemoryMax','memory_max_bytes'),('MemorySwapMax','memory_swap_max_bytes'),
                     ('TasksMax','pids_max'),('RuntimeMaxUSec','runtime_max_usec')):
        if prop('Service',name,'t')!=limits[key]:
            raise ValueError('RESOURCE_LIMIT')
    group=basic['ControlGroup']
    if (not group.startswith('/') or any(v in ('','.','..') for v in group.split('/')[1:])):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    fd=open_protected_root('/sys/fs/cgroup'+group,{0})
    try:
        for name,key in (('memory.max','memory_max_bytes'),('memory.swap.max','memory_swap_max_bytes'),('pids.max','pids_max')):
            item=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
            try:
                raw=os.read(item,128)
                if raw.strip()!=str(limits[key]).encode(): raise ValueError('RESOURCE_LIMIT')
            finally: os.close(item)
    finally: os.close(fd)
    _deadline(deadline)
    return dict(path=path,unit=unit,invocation=invocation,basic=basic,properties=values)


@contextmanager
def hold_observer(peer,binding,deadline):
    pid,uid,gid=peer
    if type(pid) is not int or pid<=0 or (uid,gid)!=(0,0):
        raise ValueError('ACCESS_BOUNDARY_UNPROVEN')
    pidfd=os.pidfd_open(pid,0)
    try:
        poller=select.poll()
        poller.register(pidfd,select.POLLIN|select.POLLHUP|select.POLLERR)
        def alive():
            _deadline(deadline)
            if poller.poll(0): raise ValueError('IDENTITY_CHANGED')
        alive()
        profile,release=read_observer(binding['observer_profile_id'],binding['observer_profile_sha'],deadline)
        baseline=snapshot(pid,profile,release,deadline)
        alive()
        def verify():
            alive()
            if read_observer(binding['observer_profile_id'],binding['observer_profile_sha'],deadline)!=(profile,release):
                raise ValueError('IDENTITY_CHANGED')
            if snapshot(pid,profile,release,deadline)!=baseline:
                raise ValueError('IDENTITY_CHANGED')
            alive()
        yield verify
    finally:
        os.close(pidfd)
