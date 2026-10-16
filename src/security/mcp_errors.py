"""Safe, stable MCP errors for expected operational failures."""

import json
from functools import wraps

from fastapi import HTTPException
from fastmcp.exceptions import ToolError


def _error_payload(exc: HTTPException) -> dict:
    detail = exc.detail
    details = None
    if isinstance(detail, dict):
        message = str(detail.get("message") or "Request failed")
        details = {
            key: value
            for key, value in detail.items()
            if key in {"imap", "smtp", "retry_after", "field", "account_id"}
        }
    else:
        message = str(detail)

    lowered = message.lower()
    if "regex requires" in lowered:
        code = "REGEX_SCOPE_REQUIRED"
    elif "invalid regex" in lowered:
        code = "INVALID_REGEX"
    elif "connection failed" in lowered:
        code = "PROVIDER_CONNECTION_FAILED"
    elif "authentication" in lowered or "login failed" in lowered:
        code = "ACCOUNT_AUTH_FAILED"
    elif exc.status_code == 404:
        code = "NOT_FOUND"
    elif exc.status_code == 401:
        code = "AUTHENTICATION_REQUIRED"
    elif exc.status_code == 403:
        code = "FORBIDDEN"
    elif exc.status_code == 409:
        code = "CONFLICT"
    elif exc.status_code == 429:
        code = "RATE_LIMITED"
    elif exc.status_code >= 500:
        code = "PROVIDER_ERROR"
    else:
        code = "INVALID_ARGUMENT"

    payload = {
        "code": code,
        "message": message[:500],
        # A refusal that names when to come back is one to come back to.
        "retryable": exc.status_code in {429, 502, 503, 504} or "retry_after" in (details or {}),
    }
    if details:
        payload["details"] = details
    return payload


def mcp_error_boundary(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except ToolError:
            raise
        except HTTPException as exc:
            raise ToolError(
                json.dumps(_error_payload(exc), separators=(",", ":"), sort_keys=True)
            ) from None
        except PermissionError:
            payload = {
                "code": "AUTHENTICATION_REQUIRED",
                "message": "Authentication required",
                "retryable": False,
            }
            raise ToolError(
                json.dumps(payload, separators=(",", ":"), sort_keys=True)
            ) from None
        except ValueError as exc:
            payload = {
                "code": "INVALID_ARGUMENT",
                "message": str(exc)[:500],
                "retryable": False,
            }
            raise ToolError(
                json.dumps(payload, separators=(",", ":"), sort_keys=True)
            ) from None

    return wrapped
