"""Authenticated WebUI routes for the configured application v2 ledger."""

import os
import re
from urllib.parse import parse_qs

from api.application_operation_issue import ApplicationIssue, envelope, issue
from api.application_runtime_config import load_config
from api.application_task_service import ApplicationService
from api.helpers import j

_DETAIL = re.compile(r"^/api/application/tasks/([0-9a-f]{32})$")
_RECOVERY = re.compile(r"^/api/application/tasks/([0-9a-f]{32})/recovery$")


def _context():
    path = os.environ.get("APPLICATION_V2_CONFIG", "").strip()
    if not path:
        raise issue("NOT_FOUND", "configuration", None, "application v2 is not configured")
    config = load_config(path)
    # WebUI profile cookies select data, never an execution principal.
    # The single-user WebUI identity is bound by trusted server deployment only.
    principal = config.principal(os.environ.get("APPLICATION_V2_WEB_PRINCIPAL", "default"))
    return ApplicationService(config), principal


def _send(handler, call):
    try:
        result = call()
        status = 200
        if isinstance(result, dict):
            if result.get("accepted") and result.get("task_state") in {"RESERVED", "RUNNING"}:
                status = 202
            elif result.get("issue"):
                status = issue(result["issue"]["code"]).http_status
        return j(handler, result, status=status)
    except ApplicationIssue as exc:
        return j(handler, envelope(exc.scope_id, problem=exc), status=exc.http_status)
    except Exception:
        problem = issue("INTERNAL_ERROR", "service", None, "application runtime failed")
        return j(handler, envelope(None, problem=problem), status=500)


def handle_application_get(handler, parsed):
    if parsed.path == "/api/application/tasks/capabilities":
        def capabilities():
            service, principal = _context()
            return {"runtime_format": "application_runtime_v2", "principal": principal.name,
                    "permissions": sorted(principal.permissions), "task_read_all": principal.task_read_all,
                    "policy_sha": service.config.artifact_policy_sha,
                    "fixed_tool_configured": service.config.fixed_tool is not None}
        return _send(handler, capabilities)
    if parsed.path == "/api/application/tasks":
        query = parse_qs(parsed.query or "")

        def listing():
            service, principal = _context()
            try:
                limit = min(100, max(1, int(query.get("limit", ["50"])[0])))
                offset = max(0, int(query.get("offset", ["0"])[0]))
            except ValueError as exc:
                raise issue("INVALID_REQUEST", message="invalid pagination") from exc
            return service.list(principal, limit, offset)

        return _send(handler, listing)
    match = _RECOVERY.fullmatch(parsed.path)
    if match:
        query = parse_qs(parsed.query or "")

        def inspect():
            service, principal = _context()
            principal.require("recover")
            return service.inspect_recovery(
                match.group(1), query.get("candidate_id", [""])[0], principal
            )

        return _send(handler, inspect)
    match = _DETAIL.fullmatch(parsed.path)
    if match:
        return _send(handler, lambda: _get(match.group(1)))
    return False


def _get(task_id):
    service, principal = _context()
    return service.get(task_id, principal)


def handle_application_post(handler, parsed, body):
    if parsed.path == "/api/application/tasks":
        return _send(handler, lambda: _submit(body))
    match = _RECOVERY.fullmatch(parsed.path)
    if match:
        def recover():
            if not isinstance(body, dict):
                raise issue("INVALID_REQUEST", message="request body must be an object")
            parameters = body.get("parameters")
            if body.get("operation") != "recover" or not isinstance(parameters, dict) or parameters.get("interrupted_task_id") != match.group(1):
                raise issue("CONFLICT", "task", match.group(1), "URL and recovery task mismatch")
            return _submit(body)

        return _send(handler, recover)
    return False


def _submit(body):
    service, principal = _context()
    return service.submit(body, principal)
