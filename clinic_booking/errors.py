import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ApiError(Exception):
    status_code = 400

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class NotFoundError(ApiError):
    status_code = 404


class ConflictError(ApiError):
    status_code = 409


def error_response(
    status_code: int, code: str, message: str, details: list[dict] | None = None
) -> JSONResponse:
    body: dict = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return JSONResponse(status_code=status_code, content={"error": body})


def _location(loc: tuple) -> str:
    return ".".join(str(part) for part in loc)


async def _handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
    return error_response(exc.status_code, exc.code, exc.message)


async def _handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    # Pydantic's raw errors echo the submitted input back; only location and message
    # are returned so patient data is not reflected in error payloads or logs.
    details = [{"location": _location(err["loc"]), "message": err["msg"]} for err in exc.errors()]
    return error_response(422, "validation_error", "The request is invalid.", details)


_HTTP_ERROR_CODES = {404: "not_found", 405: "method_not_allowed"}


async def _handle_http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _HTTP_ERROR_CODES.get(exc.status_code, "http_error")
    response = error_response(exc.status_code, code, str(exc.detail))
    if exc.headers:
        response.headers.update(exc.headers)
    return response


async def _handle_database_unavailable(request: Request, exc: OperationalError) -> JSONResponse:
    logger.error(
        "Database operation failed on %s %s: %s",
        request.method,
        request.url.path,
        exc.orig.__class__.__name__,
    )
    return error_response(
        503, "database_unavailable", "The database is temporarily unavailable. Please retry."
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return error_response(500, "internal_error", "An unexpected error occurred.")


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_error)
    app.add_exception_handler(OperationalError, _handle_database_unavailable)
    app.add_exception_handler(Exception, _handle_unexpected_error)
