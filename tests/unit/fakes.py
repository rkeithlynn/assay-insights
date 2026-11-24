"""Stand-ins for the API's two AWS calls (assay_api.services), for the route tests."""

from __future__ import annotations

import uuid
from typing import Any

from assay_api.services import CognitoRefused


class FakeCognito:
    """Records every call. `fail` makes the next call of an operation raise that code."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail: dict[str, str] = {}
        # What `mfa_status` reports (the real function reads it from Cognito).
        self.totp = True

    def call(self, operation: str, **fields: Any) -> dict[str, Any]:
        self.calls.append((operation, fields))
        code = self.fail.pop(operation, None)
        if code:
            raise CognitoRefused(code)
        if operation == "invite":
            return {"sub": f"sub-{fields['email']}", "created": True}
        if operation == "mfa_status":
            return {"totp": self.totp}
        return {"ok": True}

    def last(self, operation: str) -> dict[str, Any]:
        return next(f for o, f in reversed(self.calls) if o == operation)


class FakeProvisioning:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.error: Exception | None = None

    def start(self, *, tenant_id: uuid.UUID, request: dict[str, Any]) -> str:
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        self.started.append(request)
        return f"arn:aws:states:us-east-1:000000000000:execution:sm:t-{tenant_id}"
