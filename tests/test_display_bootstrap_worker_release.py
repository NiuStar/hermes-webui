"""Worker release inventory must exactly cover committed executable trees."""
from types import SimpleNamespace
import pytest
from api.display_bootstrap_worker_release import WorkerRelease


def test_missing_committed_source_is_rejected():
    value=object.__new__(WorkerRelease)
    value._tracked={'api/present.py':('100644','a'*40),'api/missing.py':('100644','b'*40),
                    'scripts/bootstrap_worker_entry.py':('100644','c'*40),'README.md':('100644','d'*40)}
    value._members=frozenset({'api/present.py','scripts/bootstrap_worker_entry.py'})
    with pytest.raises(ValueError,match='APPROVAL_MISMATCH'):
        value._verify_coverage()


def test_exact_source_coverage_accepts_unshipped_docs():
    value=object.__new__(WorkerRelease)
    value._tracked={'api/present.py':('100644','a'*40),'scripts/bootstrap_worker_entry.py':('100644','c'*40),
                    'README.md':('100644','d'*40)}
    value._members=frozenset({'api/present.py','scripts/bootstrap_worker_entry.py'})
    value._verify_coverage()
