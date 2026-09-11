"""Shared command dispatch: caller supplies a trusted, transport-bound identity."""
from api.application_operation_issue import issue
from api.application_protocol import MODE


def dispatch(service, principal, command):
    if type(command) is not dict or set(command) != {'action', 'arguments'}:
        raise issue('INVALID_REQUEST', message='command schema mismatch')
    action, args = command['action'], command['arguments']
    shapes = {'init': set(), 'get': {'task_id'}, 'inspect': {'task_id', 'candidate_id'},
              'list': {'limit', 'offset'}, 'submit': {'request'}}
    if type(action) is not str or action not in shapes or type(args) is not dict or set(args) != shapes[action]:
        raise issue('INVALID_REQUEST', message='command arguments mismatch')
    if action == 'init':
        principal.require('create')
        return service.initialize()
    if action == 'submit':
        return service.submit(args['request'], principal)
    if action == 'get':
        return service.get(args['task_id'], principal)
    if action == 'inspect':
        principal.require('recover')
        return service.inspect_recovery(args['task_id'], args['candidate_id'], principal)
    if (type(args['limit']) is not int or not 1 <= args['limit'] <= 100
            or type(args['offset']) is not int or args['offset'] < 0):
        raise issue('INVALID_REQUEST', message='invalid pagination')
    return service.list(principal, args['limit'], args['offset'])
