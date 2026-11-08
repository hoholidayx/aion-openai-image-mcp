"""Manage the standalone image MCP from its uv environment."""

import argparse
import fcntl
import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from .adapters import validate_profiles
from .config import DEFAULT_CONFIG, SERVICE, load_config


START_TIMEOUT = 15
STOP_TIMEOUT = 10


def _identity(pid):
    result = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "lstart=", "-o", "state=", "-o", "command="],
        env={**os.environ, "LC_ALL": "C"},
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    fields = result.stdout.split(maxsplit=6)
    if not fields and result.returncode in (0, 1):
        return None
    if len(fields) != 7 or result.returncode:
        raise RuntimeError(f"Could not verify process {pid}")
    if fields[5].startswith("Z"):
        return None
    return {"started": " ".join(fields[:5]), "command": fields[6].strip()}


def _listener_owners(port):
    if lsof := shutil.which("lsof"):
        result = subprocess.run(
            [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode in (0, 1) and not result.stderr:
            return {int(line[1:]) for line in result.stdout.splitlines() if re.fullmatch(r"p\d+", line)}
    if ss := shutil.which("ss"):
        result = subprocess.run(
            [ss, "-H", "-ltnp", f"sport = :{port}"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if not result.returncode:
            owners = set()
            for line in result.stdout.splitlines():
                pids = re.findall(r"\bpid=(\d+)", line)
                owners.update(map(int, pids))
                if not pids:
                    owners.add(None)
            return owners
    raise RuntimeError("Cannot verify the TCP listener. Install lsof or iproute2 (ss).")


def _port_available(host, port):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((host, port))
            listener.listen(1)
        except OSError:
            return False
    return True


def _healthy(pid, host, port):
    if _listener_owners(port) != {pid}:
        return False
    connection = http.client.HTTPConnection(host, port, timeout=1)
    try:
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        payload = json.loads(response.read(4096))
        return (
            response.status == 200
            and isinstance(payload, dict)
            and payload.get("status") == "ok"
            and payload.get("service") == SERVICE
            and payload.get("pid") == pid
        )
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


class ServiceManager:
    def __init__(self, config_path, config):
        self.host = config.host
        self.port = config.port
        self.directory = config.data_dir / "service"
        self.state = self.directory / "process.json"
        self.log = self.directory / "mcp.log"
        self.command = [sys.executable, "-m", "aion_openai_image_mcp.server", "--config", str(config_path)]
        # A macOS framework Python re-execs its Python.app binary from the venv launcher.
        self.runtime_command = " ".join([sys.orig_argv[0], *self.command[1:]])

    def _owned_process(self):
        try:
            record = json.loads(self.state.read_text())
        except FileNotFoundError:
            return None
        pid = record["pid"]
        port = record["port"]
        if (type(pid) is not int or pid <= 1 or record["argv"] != self.command
                or record["host"] != self.host or type(port) is not int or not 1024 <= port <= 65535):
            raise RuntimeError(f"Unexpected process record in {self.state}")
        current = _identity(pid)
        if current is None:
            return None
        if current != record["identity"] or current["command"] != self.runtime_command:
            raise RuntimeError(f"PID {pid} does not belong to this image MCP service")
        try:
            owns_session = os.getsid(pid) == os.getpgid(pid) == pid
        except ProcessLookupError:
            return None
        if not owns_session:
            raise RuntimeError(f"PID {pid} is not the recorded detached service")
        return record

    def _require_free_port(self, host, port):
        if _listener_owners(port) or not _port_available(host, port):
            raise RuntimeError(f"Port {port} is occupied by an unmanaged listener")

    def status(self):
        record = self._owned_process()
        if record is None:
            self._require_free_port(self.host, self.port)
            print("Image MCP is stopped")
            return 1
        pid, host, port = record["pid"], record["host"], record["port"]
        ready = _healthy(pid, host, port)
        print(f"Image MCP {'is healthy' if ready else 'is running but unhealthy'} (PID {pid}) at {host}:{port}")
        print(f"Log: {self.log}")
        return 0 if ready else 1

    def start(self):
        if self._owned_process() is not None:
            return self.status()
        self._require_free_port(self.host, self.port)
        previous_handler = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        try:
            with self.log.open("ab") as log:
                child = subprocess.Popen(
                    self.command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        finally:
            signal.signal(signal.SIGHUP, previous_handler)

        try:
            deadline = time.monotonic() + START_TIMEOUT
            while time.monotonic() < deadline and child.poll() is None:
                current = _identity(child.pid)
                if current is not None and current["command"] == self.runtime_command:
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError("Could not verify the launched image MCP process")
            record = {
                "pid": child.pid,
                "identity": current,
                "argv": self.command,
                "host": self.host,
                "port": self.port,
                "log": str(self.log),
            }
            pending = self.state.with_suffix(".tmp")
            pending.write_text(json.dumps(record, indent=2) + "\n")
            pending.replace(self.state)
            while time.monotonic() < deadline and child.poll() is None:
                if _healthy(child.pid, self.host, self.port):
                    return self.status()
                time.sleep(0.2)
            raise RuntimeError(f"Image MCP did not become healthy. See {self.log}")
        except (OSError, RuntimeError, subprocess.TimeoutExpired, KeyboardInterrupt):
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=STOP_TIMEOUT)
            self.state.unlink(missing_ok=True)
            raise

    def stop(self):
        record = self._owned_process()
        if record is not None:
            pid, host, port = record["pid"], record["host"], record["port"]
            owners = _listener_owners(port)
            if owners != {pid} and (owners or not _port_available(host, port)):
                raise RuntimeError(f"Port {port} is not owned by PID {pid}. No signal was sent.")
            if self._owned_process() is not None:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + STOP_TIMEOUT
            while self._owned_process() is not None:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Timed out waiting for image MCP PID {pid} to stop. See {self.log}")
                time.sleep(0.2)
            self._require_free_port(host, port)
        else:
            self._require_free_port(self.host, self.port)
        self.state.unlink(missing_ok=True)
        print("Image MCP is stopped")
        return 0

    def restart(self):
        record = self._owned_process()
        if record is not None and (record["host"], record["port"]) != (self.host, self.port):
            self._require_free_port(self.host, self.port)
        self.stop()
        return self.start()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "restart", "status"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    options = parser.parse_args()
    os.umask(0o077)
    try:
        config_path = options.config.expanduser().resolve()
        starting = options.action in ("start", "restart")
        config = load_config(config_path, require_credentials=starting)
        if starting:
            validate_profiles(config)
        service = ServiceManager(config_path, config)
        service.directory.mkdir(parents=True, exist_ok=True)
        with service.state.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return getattr(service, options.action)()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"Image MCP: {error}", file=sys.stderr)
        return 1
