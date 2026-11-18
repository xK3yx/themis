"""Suite-wide guards.

Tests never call live APIs (M1 spec §10.1): any request through a real httpx transport
fails the test. Tests use the fake provider (tests/router/fake.py) or recorded fixtures,
both via httpx.MockTransport, which this guard doesn't touch.
"""

import httpx
import pytest


class NetworkBlocked(AssertionError):
    pass


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch):
    def refuse(self, request):
        raise NetworkBlocked(f"test tried to reach the network: {request.method} {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
