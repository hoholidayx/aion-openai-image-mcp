"""Private operator configuration for the independently managed service."""

from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

SERVICE = "aion-openai-image-mcp"
DEFAULT_CONFIG = Path.home() / f".{SERVICE}" / "config.toml"
PROFILE_PATTERN = r"[a-z0-9][a-z0-9_-]{0,47}"


@dataclass(frozen=True)
class Connection:
    base_url: str
    api_key: str = field(repr=False)
    result_origins: tuple[str, ...] = ()
    extra_secrets: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Profile:
    name: str
    adapter: str
    connection: Connection
    model: str
    supports_edits: bool = False
    quality: dict[str, str] = field(default_factory=dict)
    options: dict[str, object] = field(default_factory=dict, repr=False)

    def fingerprint(self) -> str:
        values = (
            self.adapter, self.model, self.supports_edits, self.quality, self.connection.base_url,
            self.connection.result_origins, self.connection.api_key, self.connection.extra_secrets, self.options,
        )
        return sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Config:
    data_dir: Path
    token: str = field(repr=False)
    profiles: dict[str, Profile]
    port: int = 8790
    host: str = "127.0.0.1"


def origin(url: str) -> str:
    if re.search(r"[\x00-\x20\\]", url):
        raise ValueError("URL cannot include whitespace, control characters, or backslashes")
    parts = urlsplit(url)
    if (parts.scheme not in {"https", "http"} or not parts.hostname or parts.username is not None
            or parts.password is not None or not parts.netloc.isascii()
            or re.search(r"[\s\\%]", parts.netloc)):
        raise ValueError("Expected an absolute HTTP(S) URL without credentials")
    if parts.scheme == "http" and parts.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("Plain HTTP is restricted to loopback suppliers")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}:{port}"


def _api_base(url: str) -> str:
    origin(url)
    parts = urlsplit(url)
    if parts.query or parts.fragment:
        raise ValueError("Supplier Base URL cannot include a query or fragment")
    return url.rstrip("/")


def _env(name: str, required: bool) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("Invalid environment variable name for a secret")
    if not required:
        return ""
    value = os.environ.get(name, "")
    if not value or not value.isascii() or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError(f"Set a valid nonempty value for {name}")
    return value


def load_config(path: Path = DEFAULT_CONFIG, *, require_credentials: bool = True) -> Config:
    with Path(path).expanduser().open("rb") as handle:
        raw = tomllib.load(handle)
    try:
        port = raw.get("port", 8790)
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("port must be an integer between 1024 and 65535")
        connection_settings = raw["connections"]
        connections = {
            name: Connection(
                _api_base(entry["base_url"]),
                _env(entry["api_key_env"], require_credentials) if "api_key_env" in entry else "",
                tuple(origin(url) for url in entry.get("result_origins", [])),
                {key: _env(env, require_credentials) for key, env in entry.get("secret_env", {}).items()},
            )
            for name, entry in connection_settings.items()
        }
        profiles = {}
        for name, entry in raw["profiles"].items():
            if not re.fullmatch(PROFILE_PATTERN, name):
                raise ValueError("Profile names must be lowercase path-safe identifiers")
            model = entry["model"]
            edits = entry.get("supports_edits", False)
            quality = entry.get("quality", {})
            if not isinstance(model, str) or not model.strip() or type(edits) is not bool:
                raise ValueError("Profiles require a model ID and a boolean supports_edits")
            if not isinstance(quality, dict) or not all(
                key in {"fast", "quality"} and value in {"low", "medium", "high", "auto"}
                for key, value in quality.items()
            ):
                raise ValueError("Quality maps fast or quality to an OpenAI quality value")
            adapter = entry.get("adapter", "openai_images")
            if not isinstance(adapter, str) or not adapter:
                raise ValueError("Expected a nonempty adapter ID")
            connection_name = entry["connection"]
            if adapter == "openai_images" and "api_key_env" not in connection_settings[connection_name]:
                raise ValueError("The OpenAI Images adapter needs an api_key_env")
            options = entry.get("options", {})
            if not isinstance(options, dict):
                raise ValueError("Adapter options must be a table")
            json.dumps(options, allow_nan=False)
            if adapter == "openai_images" and "default_quality" in options:
                if options["default_quality"] not in ("low", "medium", "high", "auto"):
                    raise ValueError("OpenAI default_quality must be low, medium, high, or auto")
            profiles[name] = Profile(name, adapter, connections[connection_name], model, edits, quality, options)
        if not profiles:
            raise ValueError("Configure at least one image profile")
        data_dir = Path(raw.get("data_dir", str(DEFAULT_CONFIG.parent))).expanduser().resolve()
        token = _env(raw.get("token_env", "AION_IMAGE_MCP_TOKEN"), require_credentials)
        if require_credentials and any(
            token in (connection.api_key, *connection.extra_secrets.values()) for connection in connections.values()
        ):
            raise ValueError("The MCP access token must differ from supplier API keys")
        return Config(data_dir, token, profiles, port)
    except (AttributeError, KeyError, TypeError) as exc:
        raise ValueError("Invalid connection or profile configuration") from exc
