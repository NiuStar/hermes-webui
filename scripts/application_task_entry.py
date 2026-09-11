#!/usr/bin/env python3
"""Application v2 CLI: explicit IDs, trusted OS identity, one JSON response.

Run as `python3 -m scripts.application_task_entry`. For different OS principals
configure the local broker; direct mode remains a single-identity deployment.
"""
import argparse
import json
import os
import pwd
import sys

from api.application_commands import dispatch
from api.application_operation_issue import ApplicationIssue, envelope, issue
from api.application_protocol import MODE, parse_request
from api.application_runtime_config import load_config
from api.application_task_service import ApplicationService


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise issue('INVALID_REQUEST', message=message)


def parser():
    result = Parser(description=__doc__)
    result.add_argument('--config', required=True)
    result.add_argument('--principal', help='optional assertion, never impersonation')
    actions = result.add_subparsers(dest='action', required=True, parser_class=Parser)
    for name in ('init', 'serve', 'submit'):
        actions.add_parser(name)
    listing = actions.add_parser('list')
    listing.add_argument('--limit', type=int, default=50)
    listing.add_argument('--offset', type=int, default=0)
    for name in ('get', 'inspect', 'create', 'approve', 'publish', 'recover'):
        command = actions.add_parser(name)
        command.add_argument('--task-id', required=True)
        if name != 'get':
            command.add_argument('--candidate-id', required=True)
        if name in ('approve', 'publish', 'recover'):
            command.add_argument('--approval-id', required=name != 'recover')
        if name == 'approve':
            for field in ('manifest-sha', 'policy-sha', 'reference'):
                command.add_argument('--' + field, required=True)
        if name == 'recover':
            command.add_argument('--interrupted-task-id', required=True)
            command.add_argument('--expected-evidence-sha', required=True)
            command.add_argument('--decision', required=True,
                                 choices=('close_failed', 'finalize_existing', 'resume_publish'))
    return result


def main(argv=None, stdin=None):
    args = parser().parse_args(argv)
    if os.geteuid() == 0:
        raise issue('PERMISSION_DENIED', 'process', None, 'application CLI requires non-root execution')
    identity = pwd.getpwuid(os.geteuid()).pw_name
    if args.principal is not None and args.principal != identity:
        raise issue('PERMISSION_DENIED', 'principal', None, 'principal must match operating-system identity')
    config = load_config(args.config)
    if args.action == 'serve':
        if config.cli_socket is None or config.cli_broker_uid != os.geteuid():
            raise issue('PERMISSION_DENIED', message='broker must run as the configured UID')
        from api.application_broker import ApplicationBroker
        with ApplicationBroker(config, config.cli_socket) as server:
            server.serve_forever(poll_interval=0.2)
        return {'status': 'STOPPED', 'runtime_format': MODE}
    if args.action == 'submit':
        raw = (stdin if stdin is not None else sys.stdin.buffer).read(16385)
        request = parse_request(raw)
        value = {'mode': request.mode, 'task_id': request.task_id,
                 'operation': request.operation, 'parameters': dict(request.parameters)}
        command = {'action': 'submit', 'arguments': {'request': value}}
    elif args.action in {'create', 'approve', 'publish', 'recover'}:
        fields = set(vars(args)) - {'config', 'principal', 'action', 'task_id'}
        value = {'mode': MODE, 'task_id': args.task_id, 'operation': args.action,
                 'parameters': {key: getattr(args, key) for key in fields}}
        parse_request(value)
        command = {'action': 'submit', 'arguments': {'request': value}}
    else:
        fields = set(vars(args)) - {'config', 'principal', 'action'}
        command = {'action': args.action, 'arguments': {key: getattr(args, key) for key in fields}}
    if config.cli_socket is not None:
        from api.application_broker import client
        return client(config.cli_socket, config.cli_broker_uid, command)
    return dispatch(ApplicationService(config), config.principal(identity), command)


def exit_status(output):
    if not isinstance(output, dict):
        return 1
    problem = output.get('issue')
    if problem:
        return issue(problem.get('code', 'INTERNAL_ERROR')).cli_status
    if output.get('accepted') and output.get('task_state') != 'SUCCEEDED':
        return 5
    return 0


def run(argv=None):
    try:
        output = main(argv)
        status = exit_status(output)
    except ApplicationIssue as exc:
        status, output = exc.cli_status, envelope(None, problem=exc)
        print(f'application runtime: {exc.code}: {exc.message}', file=sys.stderr)
    except Exception as exc:
        status = 1
        output = envelope(None, problem=issue('INTERNAL_ERROR', message='internal application runtime error'))
        print(f'application runtime: {type(exc).__name__}', file=sys.stderr)
    print(json.dumps(output, sort_keys=True, ensure_ascii=False))
    return status


if __name__ == '__main__':
    raise SystemExit(run())
