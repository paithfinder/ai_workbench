"""Common API response and error contracts."""

from typing import Any

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """Stable machine-readable error detail."""

    code: str
    message: str
    details: Any | None = None


class ApiEnvelope[DataT](BaseModel):
    """Uniform success/error response envelope."""

    data: DataT | None = None
    error: ErrorDetail | None = None
    request_id: str = Field(min_length=1)


class ApiError(Exception):
    """Expected API failure translated by the global exception handler."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
