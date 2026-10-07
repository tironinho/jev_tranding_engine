from __future__ import annotations

from app.providers.jev.schemas import JevAssessment, JevMarketRequest, JevNotImplemented


class RealJevProvider:
    """Stub for the future official adapter.

    Do not add an HTTP path, host, or payload here until the operator supplies
    Jev's documentation. `base_url` is stored so the process can show that a
    credential was configured, and it is intentionally unused.
    """

    provider_name = "real"
    provider_version = "unimplemented"

    def __init__(self, *, prompt_version: str, base_url: str, api_key: str, model: str) -> None:
        self.prompt_version = prompt_version
        self.base_url = base_url
        self.api_key_configured = bool(api_key)
        self.model = model or None

    async def evaluate_market_state(self, request: JevMarketRequest) -> JevAssessment:
        raise JevNotImplemented(
            "RealJevProvider has no HTTP client. "
            "Official Jev documentation was not provided, so no endpoint was invented. "
            "Implement the call inside this class only, then leave strategies unchanged."
        )
