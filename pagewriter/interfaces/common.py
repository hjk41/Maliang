"""Small transport-neutral errors and service envelopes."""
import json
import math


class InterfaceError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}

    def as_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}


def failure(code, message, details=None):
    return {"ok": False, "error": InterfaceError(code, message, details).as_dict()}


def parse_json(value):
    """Reject explicit nonfinite constants and numeric exponent overflow."""
    def invalid(raw):
        raise ValueError("Nonfinite JSON number")

    def finite(raw):
        number = float(raw)
        return number if math.isfinite(number) else invalid(raw)

    return json.loads(value, parse_constant=invalid, parse_float=finite)


def dispatch_envelope(service, method, params):
    try:
        return {"ok": True, "result": service.dispatch(method, params)}
    except Exception as exc:
        if hasattr(exc, "code") and hasattr(exc, "message"):
            return failure(exc.code, exc.message, getattr(exc, "details", {}))
        # Do not leak paths, document content or tracebacks to transport clients.
        return failure("internal_error", "Application operation failed.")
