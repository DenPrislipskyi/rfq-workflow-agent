from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class APIException(Exception):
    status_code = 500
    default_message = "Internal server error"

    def __init__(self, message: str | None = None):
        self.message = message or self.default_message
        super().__init__(self.message)


class UnsupportedFileTypeException(APIException):
    status_code = 415
    default_message = "Unsupported file type"

    def __init__(self, content_type: str | None = None):
        message = (
            f"Unsupported file type: {content_type}"
            if content_type
            else self.default_message
        )
        super().__init__(message)


class EmptyFileException(APIException):
    status_code = 400
    default_message = "File is empty"

    def __init__(self, filename: str | None = None):
        message = f"File '{filename}' is empty" if filename else self.default_message
        super().__init__(message)


class FileNameLongException(APIException):
    status_code = 400
    default_message = "File name is longer than 256 characters"

    def __init__(self, filename: str | None = None):
        message = (
            f"File name '{filename}' is longer than 256 characters"
            if filename
            else self.default_message
        )
        super().__init__(message)


class LLMUnavailableException(APIException):
    status_code = 502
    default_message = "The language model is unavailable"


class LLMTimeoutException(APIException):
    status_code = 504
    default_message = "The language model did not answer in time"


async def api_exception_handler(
    request: Request,
    exc: APIException,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"message": exc.message},
    )


def add_exceptions_handlers(app: FastAPI) -> None:
    app.add_exception_handler(APIException, api_exception_handler)  # ty: ignore
