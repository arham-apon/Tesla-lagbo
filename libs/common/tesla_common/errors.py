from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status


def _body(code: str, message: str, request: Request, details=None) -> dict:
    return {"error": {"code": code, "message": message,
                      "request_id": request.headers.get("x-request-id"), "details": details}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError):
        return JSONResponse(_body(exc.code, exc.message, request), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        # jsonable_encoder, as FastAPI's own handler does: a model_validator's error "ctx" holds the raw
        # exception object, which plain JSON can't serialize (the handler itself would crash -> 500).
        details = jsonable_encoder(exc.errors())
        return JSONResponse(_body("VALIDATION_ERROR", "Invalid request", request, details), status_code=422)
