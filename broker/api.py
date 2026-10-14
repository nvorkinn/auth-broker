"""The broker's side of the countdown API: vendor/countdown-api is the contract (an OpenAPI file) and the code
generated from it, and `api` is where the broker registers what answers each of its operations.

Registering checks the broker against the contract (see create_app): an operation the contract has and nothing
here answers, or a security scheme without an authenticator, stops the app from starting. So a contract change
that arrives with a bumped submodule breaks the build until the broker implements it.

Routes not in the contract (everything else under routes/) are plain Flask blueprints."""

from countdown_api.blueprints import Api, ApiHTTPError, AuthContext
from flask import request

from .auth import ROLES, DeviceAuthError, authenticate_device


class BrokerApi(Api):
    def error_body(self, status: int, message: str, details=None):
        """The error object is {"error": ...}. What the registry itself refuses (no credentials, say) is worded as
        the broker's other guards in auth.py word it."""
        words = {401: "unauthorized", 403: "forbidden"}
        return super().error_body(status, words.get(status, message), details)


api = BrokerApi()


@api.authenticator("deviceSecret", provides=("device_id",))
def device_secret(context: AuthContext) -> dict[str, str]:
    """A device's own bearer secret. The contract's scopes for the operation are the roles it allows; with none,
    any role will do. What it provides is the device's id, which the operation's handler receives."""
    # The raw header, not context.credential: authenticate_device is exact about the "Bearer " prefix.
    try:
        device = authenticate_device(request.headers.get("Authorization", ""), frozenset(context.scopes) or ROLES)
    except DeviceAuthError as error:
        raise ApiHTTPError(error.status, message=error.message) from error
    return {"device_id": device.device_id}
