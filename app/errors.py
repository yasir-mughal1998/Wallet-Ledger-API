"""The API's error contract: {"error": "<code>", "message": "<text>"}."""

import json

from starlette.responses import Response


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def invalid_request(message: str) -> ApiError:
    return ApiError(400, "invalid_request", message)


def user_not_found() -> ApiError:
    return ApiError(404, "user_not_found", "user has never been credited")


def insufficient_funds() -> ApiError:
    return ApiError(409, "insufficient_funds", "debit is larger than the current balance")


def service_unavailable(message: str) -> ApiError:
    return ApiError(503, "service_unavailable", message)


def error_body(code: str, message: str) -> bytes:
    return json.dumps({"error": code, "message": message}).encode()


def error_response(status: int, code: str, message: str) -> Response:
    return Response(error_body(code, message), status_code=status, media_type="application/json")
