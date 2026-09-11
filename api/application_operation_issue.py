"""Scoped application-runtime failures and transport mappings."""

from dataclasses import dataclass


@dataclass
class ApplicationIssue(Exception):
    code: str
    scope_type: str
    scope_id: str | None
    retry_action: str
    message: str
    http_status: int = 500
    cli_status: int = 1

    def as_dict(self):
        return {
            "code": self.code,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "retry_action": self.retry_action,
            "message": self.message,
        }


def issue(code, scope_type="request", scope_id=None, message=None):
    mapping = {
        "INVALID_REQUEST": (400, 2, "none"),
        "UNSUPPORTED_VERSION": (400, 2, "none"),
        "UNSUPPORTED_FEATURE": (400, 2, "none"),
        "PERMISSION_DENIED": (403, 3, "none"),
        "NOT_FOUND": (404, 3, "none"),
        "CONFLICT": (409, 2, "none"),
        "BUSY": (409, 4, "check_status"),
        "SPACE_LOW": (507, 4, "recheck_resource"),
        "DEPENDENCY_UNAVAILABLE": (503, 4, "retry_request"),
        "OUTCOME_UNKNOWN": (500, 5, "inspect_recovery"),
        "INTEGRITY_ERROR": (500, 5, "operator_review"),
    }
    http, cli, retry = mapping.get(code, (500, 1, "operator_review"))
    return ApplicationIssue(code, scope_type, scope_id, retry, message or code, http, cli)


def envelope(request_id, *, accepted=False, task_state=None, result=None, problem=None):
    return {
        "api_version": "application_runtime_v2",
        "request_id": request_id,
        "accepted": bool(accepted),
        "task_state": task_state,
        "result_format": "application_runtime_v2" if result is not None else None,
        "result": result,
        "issue": problem.as_dict() if problem is not None else None,
    }