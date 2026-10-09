from mining_contracts.domain.core import ErrorCode, ErrorDetails


class DomainError(Exception):
    def __init__(
        self, code: ErrorCode, message: str, details: ErrorDetails | None = None
    ):
        self.code = code
        self.message = message
        self.details = details or ErrorDetails()
        super().__init__(message)

    def __reduce__(self):
        return type(self), (self.code, self.message, self.details)


def fail(
    code: ErrorCode, message: str, details: ErrorDetails | None = None
) -> DomainError:
    return DomainError(code, message, details)
