"""Failed CI workload evidence must survive its nonzero exit and cleanup."""
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.load((ROOT / '.github/workflows' / name).read_text(), Loader=yaml.BaseLoader)


def test_pytest_failure_evidence_is_retained_without_changing_shards():
    steps = workflow('tests.yml')['jobs']['test']['steps']
    execution = next(s for s in steps if s.get('name', '').startswith('Run tests'))
    assert 'set -o pipefail' in execution['run']
    assert '--shard-id=${{ matrix.shard }} --num-shards=5' in execution['run']
    assert '--junitxml=' in execution['run']
    upload = next(s for s in steps if s.get('uses', '').startswith('actions/upload-artifact@'))
    assert upload['if'] == 'always()'
    assert 'matrix.python-version' in upload['with']['name']
    assert 'matrix.shard' in upload['with']['name']


def test_smoke_captures_logs_before_teardown_and_uploads_on_failure():
    steps = workflow('docker-smoke.yml')['jobs']['smoke']['steps']
    execution = next(s for s in steps if s.get('name', '').startswith('Smoke ('))
    run = execution['run']
    assert 'mkdir -p ci-evidence' in run
    assert run.index('tee ci-evidence/compose.log') < run.index('down -v --remove-orphans')
    upload = next(s for s in steps if s.get('uses', '').startswith('actions/upload-artifact@'))
    assert upload['if'] == 'always()'
    assert 'matrix.variant' in upload['with']['name']
