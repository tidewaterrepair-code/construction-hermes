"""Structured business errors. Each maps to a stable code for tools and HTTP."""

from __future__ import annotations

from typing import Any


class ChopsError(Exception):
    code = "error"
    http_status = 400

    def __init__(self, message: str, **detail: Any):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **({"detail": self.detail} if self.detail else {})}


class NotFound(ChopsError):
    code = "not_found"
    http_status = 404


class Forbidden(ChopsError):
    code = "forbidden"
    http_status = 403


class ValidationFailed(ChopsError):
    code = "validation_failed"
    http_status = 422


class InvalidTransition(ChopsError):
    code = "invalid_transition"
    http_status = 409


class Conflict(ChopsError):
    code = "conflict"
    http_status = 409


class Ambiguous(ChopsError):
    code = "ambiguous"
    http_status = 409


class Blocked(ChopsError):
    """A rule (kill switch, mode, missing configuration) prevents the action."""

    code = "blocked"
    http_status = 423


class ApprovalRequired(ChopsError):
    code = "approval_required"
    http_status = 202
