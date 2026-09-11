"""Publication error classification only; no persistence claims."""
import errno
import sqlite3
import pytest
from api.display_bootstrap_lifecycle import _publication_error_code
from api.display_bootstrap_policy import BootstrapRejected


@pytest.mark.parametrize('error,expected', [
    (ValueError('RESOURCE_LIMIT'), 'RESOURCE_LIMIT'),
    (ValueError('UNKNOWN_SCHEMA'), 'UNKNOWN_SCHEMA'),
    (ValueError('SIDECAR_REMAINS'), 'SIDECAR_REMAINS'),
    (ValueError('IDENTITY_CHANGED'), 'IDENTITY_CHANGED'),
    (ValueError('private diagnostic'), 'IO_FAILURE'),
    (OSError(errno.ENOSPC, 'full'), 'RESOURCE_LIMIT'),
    (OSError(errno.EDQUOT, 'quota'), 'RESOURCE_LIMIT'),
    (OSError(errno.EIO, 'io'), 'IO_FAILURE'),
    (BootstrapRejected('APPROVAL_MISMATCH'), 'APPROVAL_MISMATCH'),
])
def test_error_classification(error, expected):
    assert _publication_error_code(error) == expected


def test_sqlite_full_is_resource_limit():
    error = sqlite3.OperationalError('full')
    error.sqlite_errorcode = sqlite3.SQLITE_FULL
    assert _publication_error_code(error) == 'RESOURCE_LIMIT'


def test_sqlite_interrupt_without_deadline_evidence_is_io():
    error = sqlite3.OperationalError('interrupted')
    error.sqlite_errorcode = sqlite3.SQLITE_INTERRUPT
    assert _publication_error_code(error) == 'IO_FAILURE'
