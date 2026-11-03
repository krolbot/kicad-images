"""Build-time authenticated Streamable HTTP smoke test."""
import asyncio
import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

HOST = "127.0.0.1"
PORT = 18080
URL = f"http://{HOST}:{PORT}/mcp"
TOKEN = "build-only-mcp-token"
BOARD_DIR = Path("/projects/http-mcp-smoke")
BOARD = BOARD_DIR / "fixture.kicad_pcb"


def wait_for_auth_gate() -> None:
    for _ in range(100):
        try:
            urllib.request.urlopen(URL, timeout=0.2)
        except urllib.error.HTTPError as error:
            if error.code == 401 and error.headers.get("WWW-Authenticate") == "Bearer":
                return
        except OSError:
            pass
        time.sleep(0.05)
    raise RuntimeError("Authenticated MCP endpoint did not become ready")


def assert_server_is_unprivileged(pid: int) -> None:
    status = Path(f"/proc/{pid}/status").read_text()
    uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
    effective_uid = int(uid_line.split()[2])
    if effective_uid == 0:
        raise RuntimeError("MCP HTTP server must not run as root")


async def authenticated_probe() -> None:
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30) as client:
        async with streamable_http_client(URL, http_client=client) as streams:
            read, write = streams[:2]
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                names = {tool.name for tool in tools}
                required = {"get_version", "get_toolkit_status", "run_erc", "run_drc",
                            "add_copper_zone", "fill_zones"}
                assert required <= names, sorted(required - names)
                result = await session.call_tool("get_version", {})
                assert not result.is_error, result
                zone = await session.call_tool("add_copper_zone", {
                    "pcb_path": str(BOARD),
                    "net_name": "/N1",
                    "layer": "F.Cu",
                    "corners": [{"x": 5, "y": 5}, {"x": 40, "y": 5},
                                {"x": 40, "y": 30}, {"x": 5, "y": 30}],
                })
                assert not zone.is_error, zone
                filled = await session.call_tool("fill_zones", {"pcb_path": str(BOARD)})
                assert not filled.is_error, filled


def prepare_board() -> None:
    shutil.rmtree(BOARD_DIR, ignore_errors=True)
    if os.geteuid() == 0:
        subprocess.run(["install", "-d", "-m", "0700", "-o", "abc", "-g", "abc",
                        str(BOARD_DIR)], check=True)
        user_prefix = ["/command/s6-setuidgid", "abc"]
    else:
        BOARD_DIR.mkdir(mode=0o700)
        user_prefix = []
    subprocess.run([
        *user_prefix, "xvfb-run", "-a", "-e", "/dev/stderr",
        "/usr/bin/python3", "/usr/local/share/kicad/mcp_board_batch_fixture.py", str(BOARD),
    ], check=True)


def main() -> None:
    if os.environ.get("MCP_HTTP_SMOKE_CLIENT_ONLY") == "1":
        asyncio.run(authenticated_probe())
        return
    prepare_board()
    env = os.environ.copy()
    env.update({
        "KICAD_MCP_BEARER_TOKEN": TOKEN,
        "KICAD_MCP_ALLOWED_HOSTS": f"{HOST}:{PORT}",
    })
    server = subprocess.Popen(
        ["/usr/local/bin/kicad-mcp-http", "--host", HOST, "--port", str(PORT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        wait_for_auth_gate()
        assert_server_is_unprivileged(server.pid)
        asyncio.run(authenticated_probe())
    finally:
        os.killpg(server.pid, signal.SIGTERM)
        try:
            server.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(server.pid, signal.SIGKILL)
            server.communicate()
        shutil.rmtree(BOARD_DIR, ignore_errors=True)
    if server.returncode not in (0, -15):
        raise RuntimeError(f"MCP HTTP server exited with {server.returncode}")


if __name__ == "__main__":
    main()
