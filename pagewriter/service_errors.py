"""Stable, transport-independent errors for GUI, CLI and agent callers."""
class ServiceError(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}

    def as_dict(self):
        return {'code': self.code, 'message': self.message, 'details': self.details}


def number(value, name, low=None, high=None):
    import math
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ServiceError('INVALID_ARGUMENT', f'{name} 必须为有限数值')
    if (low is not None and value < low) or (high is not None and value > high):
        raise ServiceError('INVALID_ARGUMENT', f'{name} 超出范围', {'min': low, 'max': high})
    return value


def fields(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ServiceError('INVALID_ARGUMENT', '字段缺失或包含未知字段',
                           {'allowed': sorted(allowed), 'required': sorted(required)})
    return value
