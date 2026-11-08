import json
from types import SimpleNamespace

import pytest

from aion_openai_image_mcp import manage
from aion_openai_image_mcp.config import Config, Connection, Profile


@pytest.fixture
def service(tmp_path):
    config = SimpleNamespace(host="127.0.0.1", port=8790, data_dir=tmp_path)
    manager = manage.ServiceManager(tmp_path / "config.toml", config)
    manager.directory.mkdir()
    manager.state.write_text(json.dumps({
        "pid": 1234,
        "identity": {"started": "recorded start", "command": manager.runtime_command},
        "argv": manager.command,
        "host": manager.host,
        "port": manager.port,
        "log": str(manager.log),
    }))
    return manager


def test_stop_rejects_reused_pid(service, monkeypatch):
    monkeypatch.setattr(manage, "_identity", lambda pid: {
        "started": "different start", "command": service.runtime_command,
    })
    with pytest.raises(RuntimeError, match="does not belong"):
        service.stop()
    assert service.state.exists()


def test_stop_rejects_foreign_listener_before_sending_signal(service, monkeypatch):
    monkeypatch.setattr(manage, "_identity", lambda pid: {
        "started": "recorded start", "command": service.runtime_command,
    })
    monkeypatch.setattr(manage.os, "getsid", lambda pid: pid)
    monkeypatch.setattr(manage.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(manage, "_listener_owners", lambda port: {4321})
    monkeypatch.setattr(manage.os, "kill", lambda *_: pytest.fail("unexpected signal"))

    with pytest.raises(RuntimeError, match="No signal was sent"):
        service.stop()
    assert service.state.exists()


def test_start_rejects_unmanaged_port(service, monkeypatch):
    service.state.unlink()
    monkeypatch.setattr(manage, "_listener_owners", lambda port: {4321})
    monkeypatch.setattr(manage.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("unexpected launch"))

    with pytest.raises(RuntimeError, match="occupied by an unmanaged listener"):
        service.start()


def test_restart_rejects_unsupported_aiwave_editing_before_managing_processes(tmp_path, monkeypatch, capsys):
    profile = Profile("example", "aiwave_grok", Connection("https://gateway.example/v1", "test-key"),
                      "example-image", supports_edits=True)
    config = Config(tmp_path, "synthetic-mcp", {profile.name: profile})
    monkeypatch.setattr(manage.os, "umask", lambda _mode: None)
    monkeypatch.setattr(manage, "load_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(manage, "ServiceManager", lambda *_args: pytest.fail("unexpected service operation"))
    monkeypatch.setattr(manage.sys, "argv", ["aion-image-mcp-service", "restart"])

    assert manage.main() == 1
    assert "only supports image generation" in capsys.readouterr().err
