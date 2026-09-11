"""Real filesystem authority tests; opt-in root setup only on isolated .10."""
import os
from pathlib import Path
import subprocess
import tempfile
import shutil

import pytest


@pytest.mark.parametrize('case', ['trusted', 'uid_parent', 'writable_parent', 'symlink', 'uid_file'])
def test_real_source_authority(case):
    base = os.environ.get('APPLICATION_AUTHORITY_TEST_ROOT')
    if not base or os.geteuid() != 0:
        pytest.skip('explicit root-owned isolated authority fixture required')
    root = Path(tempfile.mkdtemp(prefix='source-authority-', dir=base))
    try:
        root.chmod(0o755)
        parent = root / 'source'
        parent.mkdir(mode=0o755)
        source = parent / 'module.py'
        source.write_text('value = 1\n')
        source.chmod(0o644)
        if case == 'uid_parent':
            os.chown(parent, 65534, 65534)
        elif case == 'writable_parent':
            parent.chmod(0o777)
        elif case == 'uid_file':
            os.chown(source, 65534, 65534)
        elif case == 'symlink':
            target = parent / 'actual.py'
            source.rename(target)
            source.symlink_to(target)
        code = ('from api.application_fixed_tool import trusted_source_path\n'
                f'trusted_source_path({str(source)!r})\n')
        repo = str(Path(__file__).resolve().parents[1])
        result = subprocess.run(['runuser', '-u', 'nobody', '--', 'env',
            'PYTHONDONTWRITEBYTECODE=1', 'PYTHONPATH=' + repo,
            '/usr/bin/python3', '-c', code], capture_output=True, text=True)
        if case == 'trusted':
            assert result.returncode == 0, result.stderr
        else:
            assert result.returncode != 0
            assert ('SOURCE_BINDING_AUTHORITY' in result.stderr
                    or 'storage path validation failed' in result.stderr
                    or 'untrusted storage ancestry' in result.stderr), result.stderr
    finally:
        shutil.rmtree(root)
