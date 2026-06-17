import os
import socket
import subprocess
import time
from functools import lru_cache

from pymongo import MongoClient
from pymongo.database import Database

_local_port: int | None = None
_tunnel_process: subprocess.Popen[str] | None = None


def ssh_enabled() -> bool:
    return os.getenv("MONGODB_SSH_ENABLED", "true").strip().lower() in {"1", "true", "yes"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"SSH tunnel port {port} not reachable after {timeout}s")


def _process_output(process: subprocess.Popen[str]) -> str:
    try:
        stdout, stderr = process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        return ""
    return (stderr or stdout or "").strip()


def _stop_ssh_tunnel() -> None:
    global _local_port, _tunnel_process

    if _tunnel_process and _tunnel_process.poll() is None:
        _tunnel_process.terminate()
        try:
            _tunnel_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _tunnel_process.kill()

    _tunnel_process = None
    _local_port = None


def _start_ssh_tunnel() -> int:
    global _local_port, _tunnel_process

    ssh_host = os.getenv("MONGODB_SSH_HOST", "mdstudio.oriele.ai")
    ssh_user = os.getenv("MONGODB_SSH_USER", "oriele")
    ssh_key = os.getenv("MONGODB_SSH_KEY_PATH", "/Users/borhan/Desktop/keys/id_ed25519")
    remote_host = os.getenv("MONGODB_SSH_REMOTE_HOST", "127.0.0.1")
    remote_port = os.getenv("MONGODB_SSH_REMOTE_PORT", "27017")

    local_port = int(os.getenv("MONGODB_SSH_LOCAL_PORT", "0")) or _free_port()
    remote_target = f"{remote_host}:{remote_port}"

    if _tunnel_process:
        _stop_ssh_tunnel()

    cmd = [
        "ssh",
        "-i", ssh_key,
        "-N",
        "-L", f"127.0.0.1:{local_port}:{remote_target}",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=30",
        f"{ssh_user}@{ssh_host}",
    ]

    print(f"Opening MongoDB SSH tunnel on 127.0.0.1:{local_port}...", flush=True)
    _tunnel_process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        _wait_for_port(local_port)
    except Exception as err:
        output = _process_output(_tunnel_process)
        _stop_ssh_tunnel()
        raise RuntimeError(output or str(err)) from err

    if _tunnel_process.poll() is not None:
        output = _process_output(_tunnel_process)
        _stop_ssh_tunnel()
        raise RuntimeError(f"SSH tunnel failed: {output or 'process exited'}")

    print("MongoDB SSH tunnel ready.", flush=True)
    _local_port = local_port
    return local_port


def _mongo_uri() -> str:
    if ssh_enabled():
        global _local_port
        if _local_port is None or (
            _tunnel_process is not None and _tunnel_process.poll() is not None
        ):
            _local_port = _start_ssh_tunnel()
        return f"mongodb://127.0.0.1:{_local_port}"

    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise ValueError("MONGODB_URI is not set in .env")
    return uri


@lru_cache
def get_client() -> MongoClient:
    return MongoClient(_mongo_uri(), serverSelectionTimeoutMS=10000)


def get_db() -> Database:
    db_name = os.getenv("MONGODB_DB", "RedditBot")
    return get_client()[db_name]


def close_connection() -> None:
    get_client.cache_clear()
    _stop_ssh_tunnel()
