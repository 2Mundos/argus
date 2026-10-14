"""Domain errors, mapped to HTTP with a stable `code` agents can act on."""


class DomainError(Exception):
    status = 400
    code = "bad_request"

    def __init__(
        self, message: str, *, code: str | None = None, details: list[str] | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or []
        if code:
            self.code = code


class NotFoundError(DomainError):
    status = 404
    code = "not_found"


class ConflictError(DomainError):
    status = 409
    code = "conflict"


class LeaseConflictError(ConflictError):
    """The caller does not hold a valid lease on the task (worker lifecycle 3.3)."""


class UnauthorizedError(DomainError):
    status = 401
    code = "invalid_token"


class ForbiddenError(DomainError):
    status = 403
    code = "forbidden"


class ValidationFailedError(DomainError):
    """Input or result does not match its kind's schema; `details` lists why."""

    status = 422
    code = "validation_failed"


class TooLargeError(DomainError):
    status = 413
    code = "too_large"


class RateLimitedError(DomainError):
    status = 429
    code = "rate_limited"
