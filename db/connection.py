import os
import socket
import subprocess
import time
from functools import lru_cache

from pymongo import MongoClient
from pymongo.database import Database

_local_port: int | None = None


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


def _kill_tunnel_on_port(port: int) -> None:
    subprocess.run(
        ["sh", "-c", f"kill $(lsof -t -iTCP:{port} -sTCP:LISTEN) 2>/dev/null || true"],
        check=False,
    )


def _start_ssh_tunnel() -> int:
    global _local_port

    ssh_host = os.getenv("MONGODB_SSH_HOST", "mdstudio.oriele.ai")
    ssh_user = os.getenv("MONGODB_SSH_USER", "oriele")
    ssh_key = os.getenv("MONGODB_SSH_KEY_PATH", "/Users/borhan/Desktop/keys/id_ed25519")
    remote_host = os.getenv("MONGODB_SSH_REMOTE_HOST", "127.0.0.1")
    remote_port = os.getenv("MONGODB_SSH_REMOTE_PORT", "27017")

    local_port = int(os.getenv("MONGODB_SSH_LOCAL_PORT", "0")) or _free_port()
    remote_target = f"{remote_host}:{remote_port}"

    if _local_port:
        _kill_tunnel_on_port(_local_port)

    cmd = [
        "ssh",
        "-f",
        "-i", ssh_key,
        "-N",
        "-L", f"127.0.0.1:{local_port}:{remote_target}",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=30",
        f"{ssh_user}@{ssh_host}",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"SSH tunnel failed: {result.stderr.strip() or result.stdout.strip()}")

    _wait_for_port(local_port)
    _local_port = local_port
    return local_port


def _mongo_uri() -> str:
    if ssh_enabled():
        global _local_port
        if _local_port is None:
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
    global _local_port
    get_client.cache_clear()
    if _local_port:
        _kill_tunnel_on_port(_local_port)
        _local_port = None
