"""Decoder data and local subtitles must not widen executable origins."""

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tgdrive.web.http_guard import SecurityHeadersMiddleware


@pytest.mark.parametrize("path", ["/", "/static/decoder.js", "/api/v1/preview"])
def test_decoder_data_fetch_keeps_network_and_execution_policy_restricted(path):
    async def respond(request):
        return PlainTextResponse("fixture")

    app = Starlette(routes=[Route(path, respond)])
    app.add_middleware(SecurityHeadersMiddleware, secure=False)
    with TestClient(app) as client:
        response = client.get(path)
    values = {}
    for directive in response.headers["content-security-policy"].split(";"):
        tokens = directive.split()
        if tokens:
            values[tokens[0]] = set(tokens[1:])
    assert values["connect-src"] == {"'self'", "data:", "blob:"}
    assert values["script-src"] == {"'self'", "'wasm-unsafe-eval'"}
    assert values["worker-src"] == {"'self'", "blob:"}
    assert values["default-src"] == {"'self'"}
    assert values["object-src"] == {"'none'"}
    assert values["base-uri"] == {"'none'"}
    assert values["style-src-elem"] == {"'self'"}
    assert "data:" not in values["script-src"]
    assert "'unsafe-eval'" not in values["script-src"]
