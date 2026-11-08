"""Streamable HTTP entrypoint for Aion's image_jobs_v1 protocol."""

import argparse
import asyncio
import base64
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from hmac import compare_digest
import json
import logging
import os
from pathlib import Path
import signal
from typing import Literal

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
import uvicorn

from .adapters import ADAPTERS
from .config import Config, DEFAULT_CONFIG, Profile, SERVICE, load_config
from .images import MAX_REQUEST_BODY, validate_references
from .jobs import JobError, Jobs

UUID_PATTERN = r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SubmitArguments(Arguments):
    request_id: str = Field(pattern=UUID_PATTERN)
    prompt: str = Field(min_length=1, max_length=8000)
    size: str | None = Field(default=None, max_length=24)
    preset: Literal["fast", "quality"] | None = None


class EditArguments(SubmitArguments):
    images: list[str] = Field(default_factory=list, max_length=10)


class TaskArguments(Arguments):
    task_id: str = Field(pattern=UUID_PATTERN)


def structured(value: dict, *, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        structured_content=value,
        content=[types.TextContent(type="text", text=json.dumps(value))],
        is_error=error,
    )


def profile_app(profile: Profile, jobs: Jobs, wake: asyncio.Event, is_ready, hosts: list[str]):
    submit_model = EditArguments if profile.supports_edits else SubmitArguments
    tools = {
        "image_submit": (submit_model, "Submit a single image generation task."),
        "image_status": (TaskArguments, "Read task state."),
        "image_result": (TaskArguments, "Get the completed PNG."),
    }
    schemas = {name: model.model_json_schema() for name, (model, _) in tools.items()}

    async def list_tools(_, params):
        return types.ListToolsResult(tools=[
            types.Tool(name=name, description=description, input_schema=schemas[name])
            for name, (_, description) in tools.items()
        ])

    async def call_tool(_, params):
        name = params.name
        if name not in tools:
            return structured({"error_code": "invalid_request", "message": "Unknown image tool"}, error=True)
        try:
            args = tools[name][0].model_validate(params.arguments or {})
            if name == "image_submit":
                if not is_ready():
                    raise JobError("worker_unavailable", "Image job worker is unavailable")
                if not args.prompt.strip():
                    raise ValueError("Missing prompt")
                request = args.model_dump()
                request.setdefault("images", [])
                await asyncio.to_thread(validate_references, request["images"])
                receipt = await asyncio.to_thread(jobs.submit, profile, request)
                wake.set()
                return structured(receipt)
            if name == "image_status":
                return structured(await asyncio.to_thread(jobs.status, profile.name, args.task_id))
            metadata, image = await asyncio.to_thread(jobs.result, profile.name, args.task_id)
            return types.CallToolResult(
                structured_content=metadata,
                content=[types.ImageContent(type="image", mime_type="image/png", data=base64.b64encode(image).decode())],
            )
        except (ValidationError, ValueError):
            return structured({"error_code": "invalid_request", "message": "Invalid image request"}, error=True)
        except JobError as exc:
            return structured({"error_code": exc.code, "message": str(exc)}, error=True)

    server = Server(SERVICE, version="0.1.0", on_list_tools=list_tools, on_call_tool=call_tool,
                    get_tool_input_schema=schemas.get)
    return server.streamable_http_app(
        streamable_http_path=f"/mcp/{profile.name}", stateless_http=True, json_response=True,
        max_request_body_size=MAX_REQUEST_BODY,
        transport_security=TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=[]),
    )


def create_app(config: Config):
    if any(profile.adapter not in ADAPTERS for profile in config.profiles.values()):
        raise ValueError("A configured profile has an unknown adapter")
    jobs = Jobs(config.data_dir)
    wake = asyncio.Event()
    worker = None
    hosts = [f"{config.host}:{config.port}", f"localhost:{config.port}"]

    def is_ready():
        return worker is not None and not worker.done()

    apps = {
        f"/mcp/{profile.name}": profile_app(profile, jobs, wake, is_ready, hosts)
        for profile in config.profiles.values()
    }

    async def health(_):
        healthy = is_ready()
        return JSONResponse(
            {"status": "ok" if healthy else "unavailable", "service": SERVICE, "pid": os.getpid()},
            status_code=200 if healthy else 503,
        )

    @asynccontextmanager
    async def lifespan(_):
        nonlocal worker
        with jobs.instance():
            async with AsyncExitStack() as stack:
                for app in apps.values():
                    await stack.enter_async_context(app.router.lifespan_context(app))
                worker = asyncio.create_task(jobs.run(config, wake))
                try:
                    yield
                finally:
                    worker.cancel()
                    with suppress(asyncio.CancelledError):
                        await worker

    router = Starlette(lifespan=lifespan, routes=[Route("/healthz", health)])

    async def app(scope, receive, send):
        if scope["type"] != "http":
            await router(scope, receive, send)
            return
        headers = scope["headers"]
        incoming_hosts = [value for key, value in headers if key == b"host"]
        if len(incoming_hosts) != 1 or incoming_hosts[0] not in [host.encode() for host in hosts]:
            await PlainTextResponse("Invalid Host", status_code=421)(scope, receive, send)
            return
        if any(key == b"origin" for key, _ in headers):
            await PlainTextResponse("Browser origins are not allowed", status_code=403)(scope, receive, send)
            return
        target = apps.get(scope["path"])
        if target is None:
            await router(scope, receive, send)
            return
        auth = [value for key, value in headers if key == b"authorization"]
        if len(auth) != 1 or not compare_digest(auth[0], f"Bearer {config.token}".encode()):
            await PlainTextResponse("Unauthorized", status_code=401)(scope, receive, send)
            return
        await target(scope, receive, send)

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    os.umask(0o077)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        config = load_config(args.config)
        app = create_app(config)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    logger = logging.getLogger(__package__)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    uvicorn.run(app, host=config.host, port=config.port, access_log=False, timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()
