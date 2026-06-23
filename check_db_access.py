import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient


def truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_port(port: int, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"Timed out waiting for local SSH tunnel port {port}")


def read_process_output(process: subprocess.Popen[str]) -> str:
    try:
        stdout, stderr = process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        return ""
    return (stderr or stdout or "").strip()


def start_ssh_tunnel(timeout: float) -> tuple[subprocess.Popen[str], str]:
    ssh_exe = shutil.which("ssh")
    if not ssh_exe:
        raise RuntimeError("ssh executable not found on PATH")

    ssh_host = os.getenv("MONGODB_SSH_HOST", "mdstudio.oriele.ai")
    ssh_user = os.getenv("MONGODB_SSH_USER", "oriele")
    ssh_key = os.getenv("MONGODB_SSH_KEY_PATH", "/Users/borhan/Desktop/keys/id_ed25519")
    remote_host = os.getenv("MONGODB_SSH_REMOTE_HOST", "127.0.0.1")
    remote_port = os.getenv("MONGODB_SSH_REMOTE_PORT", "27017")
    local_port = int(os.getenv("MONGODB_SSH_LOCAL_PORT", "0") or "0") or free_port()

    key_path = Path(ssh_key)
    if not key_path.exists():
        raise FileNotFoundError(f"SSH key not found: {key_path}")

    cmd = [
        ssh_exe,
        "-i",
        str(key_path),
        "-N",
        "-L",
        f"127.0.0.1:{local_port}:{remote_host}:{remote_port}",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={int(timeout)}",
        f"{ssh_user}@{ssh_host}",
    ]

    print(f"Starting SSH tunnel: 127.0.0.1:{local_port} -> {remote_host}:{remote_port}")
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        wait_for_port(local_port, timeout)
    except Exception:
        output = read_process_output(process)
        process.terminate()
        raise RuntimeError(output or f"SSH tunnel did not open port {local_port}")

    if process.poll() is not None:
        output = read_process_output(process)
        raise RuntimeError(output or "SSH process exited before MongoDB check")

    return process, f"mongodb://127.0.0.1:{local_port}"


def check_mongo(uri: str, db_name: str, timeout_ms: int) -> None:
    client = MongoClient(uri, serverSelectionTimeoutMS=timeout_ms)
    try:
        client.admin.command("ping")
        collections = client[db_name].list_collection_names()
    finally:
        client.close()

    print("MongoDB ping: OK")
    print(f"Database: {db_name}")
    print(f"Collections visible: {len(collections)}")
    if collections:
        print("First collections: " + ", ".join(collections[:10]))


def main() -> int:
    parser = argparse.ArgumentParser(description="Check MongoDB access using .env settings.")
    parser.add_argument("--timeout", type=float, default=10.0, help="SSH/Mongo timeout in seconds")
    args = parser.parse_args()

    load_dotenv(override=True)

    db_name = os.getenv("MONGODB_DB", "RedditBot")
    ssh_enabled = truthy(os.getenv("MONGODB_SSH_ENABLED"), default=False)

    print("DB access check")
    print(f"SSH enabled: {ssh_enabled}")
    print(f"MongoDB URI set: {bool(os.getenv('MONGODB_URI'))}")
    print(f"MongoDB DB: {db_name}")

    tunnel_process: subprocess.Popen[str] | None = None
    try:
        if ssh_enabled:
            print(f"SSH executable: {shutil.which('ssh') or 'not found'}")
            print(f"SSH host set: {bool(os.getenv('MONGODB_SSH_HOST'))}")
            print(f"SSH user set: {bool(os.getenv('MONGODB_SSH_USER'))}")
            print(f"SSH key exists: {Path(os.getenv('MONGODB_SSH_KEY_PATH', '')).exists()}")
            tunnel_process, uri = start_ssh_tunnel(args.timeout)
        else:
            from db.connection import direct_mongo_uri

            uri = direct_mongo_uri()
            print(f"Direct URI: {uri}")

        check_mongo(uri, db_name, int(args.timeout * 1000))
        print("DB access: OK")
        return 0
    except Exception as error:
        print(f"DB access: FAILED - {error}", file=sys.stderr)
        return 1
    finally:
        if tunnel_process and tunnel_process.poll() is None:
            tunnel_process.terminate()
            try:
                tunnel_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                tunnel_process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
