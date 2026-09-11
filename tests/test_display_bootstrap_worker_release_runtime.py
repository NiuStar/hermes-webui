"""Real Git-object verification in isolated subprocesses; not runner admission."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest


@pytest.mark.parametrize('mutation',['clean','dirty','missing','untracked','post_verify'])
def test_real_release_bytes(tmp_path,mutation):
    root=tmp_path/'release'
    root.mkdir()
    source=Path(__file__).resolve().parents[1]
    shutil.copytree(source/'api',root/'api',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    (root/'scripts').mkdir()
    entry=root/'scripts/bootstrap_worker_entry.py'
    entry.write_text('''import sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).absolute().parent.parent))
from api.display_bootstrap_worker_release import WorkerRelease
with_release=WorkerRelease(time.monotonic()+60)
try:
    if len(sys.argv)>1 and sys.argv[1]=='post_verify':
        target=Path(__file__).absolute().parent.parent/'api/__init__.py'
        target.write_bytes(target.read_bytes()+b'\\n# changed\\n')
    with_release.revalidate()
    print(with_release.commit)
finally:
    with_release.close()
''')
    for command in (['git','init','-q',str(root)],['git','-C',str(root),'add','api','scripts'],
                    ['git','-C',str(root),'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','fixture']):
        subprocess.run(command,check=True,capture_output=True,timeout=30)
    expected=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()
    if mutation=='dirty':
        with (root/'api/__init__.py').open('a') as f: f.write('\n# changed\n')
    elif mutation=='missing':
        # Preserve the missing source as evidence outside the verified tree.
        (root/'api/__init__.py').rename(tmp_path/'retained-init.py')
    elif mutation=='untracked':
        (root/'api/untracked.py').write_text('VALUE=1\n')
    result=subprocess.run(['/usr/bin/python3','-I','-B','-S',str(entry),mutation],capture_output=True,text=True,timeout=90)
    if mutation=='clean':
        assert result.returncode==0,result.stderr
        assert result.stdout.strip()==expected
    else:
        assert result.returncode!=0
        assert 'APPROVAL_MISMATCH' in result.stderr or 'IDENTITY_CHANGED' in result.stderr,result.stderr
