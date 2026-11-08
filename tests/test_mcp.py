import asyncio
import base64
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from threading import Thread
import time

from PIL import Image
import httpx
import pytest
from starlette.testclient import TestClient

from aion_openai_image_mcp.adapters import ADAPTERS
from aion_openai_image_mcp.config import Config, Connection, Profile
from aion_openai_image_mcp.images import png_dimensions
from aion_openai_image_mcp.jobs import Jobs
from aion_openai_image_mcp.server import create_app

TASK = "550e8400-e29b-41d4-a716-446655440000"
EDIT = "550e8400-e29b-41d4-a716-446655440001"
HEADERS = {
    "accept": "application/json, text/event-stream",
    "authorization": "Bearer synthetic-mcp",
    "mcp-protocol-version": "2025-11-25",
}


def make_png():
    output = BytesIO()
    Image.new("RGB", (3, 2), "blue").save(output, format="PNG")
    return output.getvalue()


PNG = make_png()


class Supplier(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["content-length"]))
        self.server.calls.append((self.path, self.headers["Authorization"], body, self.headers.get("Cookie")))
        if self.path.endswith("/edits"):
            image = {"url": f"http://127.0.0.1:{self.server.server_port}/generated.png"}
        else:
            image = {"b64_json": base64.b64encode(PNG).decode()}
        data = json.dumps({"data": [image]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Set-Cookie", "provider_session=private; Path=/")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.server.downloads.append((self.path, self.headers.get("Authorization"), self.headers.get("Cookie")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(PNG)


def rpc(client, profile, method, params=None, *, headers=HEADERS):
    return client.post(
        f"/mcp/{profile}", headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


def call(client, profile, name, arguments):
    response = rpc(client, profile, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def wait_state(client, profile, task_id):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        status = call(client, profile, "image_status", {"task_id": task_id})["structuredContent"]
        if status["state"] not in {"pending", "submitting"}:
            return status["state"]
        time.sleep(.02)
    raise AssertionError("Image task did not settle")


def test_streamable_http_generates_edits_and_resumes(tmp_path):
    supplier = ThreadingHTTPServer(("127.0.0.1", 0), Supplier)
    supplier.calls = []
    supplier.downloads = []
    thread = Thread(target=supplier.serve_forever, daemon=True)
    thread.start()
    try:
        connection = Connection(f"http://127.0.0.1:{supplier.server_port}/v1", "synthetic-supplier")
        profiles = {
            "sample": Profile(
                "sample", "openai_images", connection, "example-image", True,
                {"fast": "low"}, {"default_quality": "high"},
            ),
            "other": Profile("other", "openai_images", connection, "other-image"),
        }
        config = Config(tmp_path, "synthetic-mcp", profiles)
        request = {"request_id": TASK, "prompt": "A blue square", "preset": "fast"}
        with TestClient(create_app(config), base_url="http://127.0.0.1:8790") as client:
            assert client.get("/healthz").status_code == 200
            assert rpc(client, "sample", "tools/list", headers={"accept": HEADERS["accept"]}).status_code == 401
            assert rpc(client, "sample", "tools/list", headers={**HEADERS, "origin": "https://other.test"}).status_code == 403
            discovered = rpc(client, "sample", "tools/list").json()["result"]["tools"]
            assert {tool["name"] for tool in discovered} == {"image_submit", "image_status", "image_result"}
            assert "images" in next(tool for tool in discovered if tool["name"] == "image_submit")["inputSchema"]["properties"]
            receipt = call(client, "sample", "image_submit", request)["structuredContent"]
            assert receipt == {"task_id": TASK, "state": "pending"}
            assert wait_state(client, "sample", TASK) == "completed"
            result = call(client, "sample", "image_result", {"task_id": TASK})
            assert result["structuredContent"] == {"task_id": TASK, "width": 3, "height": 2, "sha256": sha256(PNG).hexdigest()}
            assert base64.b64decode(result["content"][0]["data"]) == PNG
            assert call(client, "other", "image_status", {"task_id": TASK})["structuredContent"]["error_code"] == "task_not_found"
            edited = call(client, "sample", "image_submit", {
                "request_id": EDIT, "prompt": "Make it red",
                "images": [base64.b64encode(PNG).decode(), base64.b64encode(PNG).decode()],
            })
            assert edited["structuredContent"]["state"] == "pending"
            assert wait_state(client, "sample", EDIT) == "completed"
        with TestClient(create_app(config), base_url="http://127.0.0.1:8790") as client:
            assert call(client, "sample", "image_submit", request)["structuredContent"]["state"] == "completed"
            mismatch = call(client, "sample", "image_submit", {**request, "prompt": "different"})
            assert mismatch["structuredContent"]["error_code"] == "request_conflict"
        assert len(supplier.calls) == 2
        generate, edit = supplier.calls
        assert generate[0:2] == ("/v1/images/generations", "Bearer synthetic-supplier")
        assert json.loads(generate[2]) == {"model": "example-image", "prompt": "A blue square", "quality": "low"}
        assert edit[0:2] == ("/v1/images/edits", "Bearer synthetic-supplier")
        assert edit[2].index(b"reference-1.png") < edit[2].index(b"reference-2.png")
        assert b'name="quality"\r\n\r\nhigh\r\n' in edit[2]
        assert generate[3] is None and edit[3] is None
        assert supplier.downloads == [("/generated.png", None, None)]
    finally:
        supplier.shutdown()
        thread.join()
        supplier.server_close()


def test_restart_never_resubmits_a_claimed_job_and_adapter_is_replaceable(tmp_path, monkeypatch):
    seen = []

    def vendor_generate(payload):
        seen.append(payload)
        return {"asset": {"body": base64.b64encode(PNG).decode()}}

    class DifferentSupplier:
        def validate_profile(self, profile):
            if not profile.options.get("engine"):
                raise ValueError("An engine is required")

        async def generate(self, profile, request, client):
            response = vendor_generate({
                "deployment": profile.model, "caption": request.prompt,
                "engine": profile.options["engine"], "access_token": profile.connection.extra_secrets["token"],
            })
            await asyncio.sleep(0)
            return base64.b64decode(response["asset"]["body"])

    monkeypatch.setitem(ADAPTERS, "different", DifferentSupplier())
    connection = Connection("https://unused.example", "test-key", extra_secrets={"token": "extra-test-key"})
    profile = Profile("example", "different", connection, "some-model", options={"engine": "custom"})
    config = Config(tmp_path, "synthetic-mcp", {"example": profile})
    store = Jobs(tmp_path)
    request = {"request_id": TASK, "prompt": "First", "size": None, "preset": None, "images": []}
    with store.instance():
        store.submit(profile, request.copy())
        assert store.claim()["task_id"] == TASK
        store.submit(profile, {**request, "request_id": EDIT, "prompt": "Second"})

    with TestClient(create_app(config), base_url="http://127.0.0.1:8790") as client:
        assert wait_state(client, "example", TASK) == "submission_uncertain"
        assert wait_state(client, "example", EDIT) == "completed"
        assert call(client, "example", "image_submit", {"request_id": TASK, "prompt": "First"})[
            "structuredContent"
        ]["state"] == "submission_uncertain"

    assert seen == [{"deployment": "some-model", "caption": "Second", "engine": "custom", "access_token": "extra-test-key"}]


@pytest.mark.parametrize("damaged", [PNG + b"trailing", PNG[:-1] + bytes([PNG[-1] ^ 1]), PNG[:25]])
def test_corrupt_png_is_rejected(damaged):
    with pytest.raises(ValueError):
        png_dimensions(damaged)


@pytest.mark.parametrize("model", ["grok-imagine-image", "grok-imagine-image-quality"])
def test_aiwave_requests_base64_and_normalizes_jpeg(model):
    from aion_openai_image_mcp.adapters.base import ImageRequest

    jpeg = BytesIO()
    Image.new("RGB", (6, 4), "blue").save(jpeg, format="JPEG")
    body = json.dumps({"data": [{"b64_json": base64.b64encode(jpeg.getvalue()).decode()}]}).encode()
    profile = Profile("grok", "aiwave_grok", Connection("https://gateway.example/v1", "test-key"), model)

    async def call():
        async def supplier(request):
            assert request.url.path == "/v1/images/generations"
            assert request.headers["Authorization"] == "Bearer test-key"
            assert json.loads(request.content) == {
                "model": model, "prompt": "Draw a blue square", "aspect_ratio": "3:2", "response_format": "b64_json",
            }
            return httpx.Response(200, stream=httpx.ByteStream(body))

        async with httpx.AsyncClient(transport=httpx.MockTransport(supplier)) as client:
            return await ADAPTERS["aiwave_grok"].generate(
                profile, ImageRequest("Draw a blue square", "1536x1024", "quality", ()), client,
            )

    assert png_dimensions(asyncio.run(call())) == (6, 4)


def test_openai_rejects_unrecognized_quality_at_startup(tmp_path):
    profile = Profile("example", "openai_images", Connection("https://gateway.example/v1", "test-key"),
                      "example-image", quality={"fast": "unknown"})
    with pytest.raises(ValueError, match="OpenAI quality must be"):
        create_app(Config(tmp_path, "synthetic-mcp", {profile.name: profile}))


@pytest.mark.parametrize(
    ("status", "supplier_code", "expected", "retained_code"),
    [
        (403, "insufficient_user_quota", "provider_quota", "insufficient_user_quota"),
        (400, "model_not_found", "provider_model_unavailable", "model_not_found"),
        (400, "private-unknown-code", "provider_request_rejected", None),
        (429, None, "provider_rate_limited", None),
        (502, None, "submission_uncertain", None),
    ],
)
def test_supplier_rejection_keeps_safe_reason(status, supplier_code, expected, retained_code):
    from aion_openai_image_mcp.adapters.base import ImageRequest, ProviderError

    profile = Profile("grok", "aiwave_grok", Connection("https://gateway.example/v1", "test-key"), "fixture")
    body = json.dumps({"error": {"code": supplier_code, "message": "private upstream text"}}).encode()

    async def call():
        async def supplier(_request):
            return httpx.Response(status, stream=httpx.ByteStream(body))

        async with httpx.AsyncClient(transport=httpx.MockTransport(supplier)) as client:
            await ADAPTERS["aiwave_grok"].generate(
                profile, ImageRequest("Draw a square", "3:4", None, ()), client,
            )

    with pytest.raises(ProviderError) as result:
        asyncio.run(call())
    assert result.value.code == expected
    assert result.value.http_status == status
    assert result.value.provider_code == retained_code
    assert str(result.value) == expected
