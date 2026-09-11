"""Fixed v2 request-policy suite. No discovery, subprocesses, or user arguments.

Release deployment pins this script, its imported API modules and interpreter.
This tool tests protocol rejection; it is not product acceptance or approval.
"""
import io
import json
import os
import sys
import unittest
from pathlib import Path

if len(sys.argv) != 1:
    raise SystemExit('ARGUMENTS_NOT_ALLOWED')
sys.dont_write_bytecode = True
os.environ.clear()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.application_operation_issue import ApplicationIssue
from api.application_protocol import MODE, Principal, parse_request


class FixedPolicyTests(unittest.TestCase):
    def request(self):
        return {'mode': MODE, 'task_id': 'a' * 32, 'operation': 'create',
                'parameters': {'candidate_id': 'b' * 32}}

    def test_create(self):
        self.assertEqual(parse_request(self.request()).operation, 'create')

    def test_no_command_or_authority_fields(self):
        for field in ('argv', 'shell', 'principal', 'pid', 'tool_id', 'ledger_path'):
            value = self.request()
            value[field] = 'unauthorized'
            with self.assertRaises(ApplicationIssue):
                parse_request(value)

    def test_no_legacy_mode(self):
        value = self.request()
        value['mode'] = 'application_budget_v1'
        with self.assertRaises(ApplicationIssue):
            parse_request(value)

    def test_no_malformed_ids(self):
        for candidate in ('B' * 32, '../file', '', None, 42):
            value = self.request()
            value['parameters']['candidate_id'] = candidate
            with self.assertRaises(ApplicationIssue):
                parse_request(value)

    def test_permissions(self):
        viewer = Principal('reader', frozenset())
        for operation in ('create', 'approve', 'publish', 'recover'):
            with self.assertRaises(ApplicationIssue):
                viewer.require(operation)

    def test_null_approval_rejected(self):
        value = self.request()
        value['operation'] = 'publish'
        value['parameters']['approval_id'] = None
        with self.assertRaises(ApplicationIssue):
            parse_request(value)


if __name__ == '__main__':
    log = io.StringIO()
    result = unittest.TextTestRunner(stream=log).run(unittest.defaultTestLoader.loadTestsFromTestCase(FixedPolicyTests))
    print(json.dumps({'tool_id': 'policy_suite', 'runtime_format': MODE,
                      'verdict': 'PASS' if result.wasSuccessful() else 'FAIL',
                      'tests_run': result.testsRun, 'failures': len(result.failures),
                      'errors': len(result.errors)}, sort_keys=True))
    if not result.wasSuccessful():
        print(log.getvalue(), file=sys.stderr)
    raise SystemExit(0 if result.wasSuccessful() else 1)
