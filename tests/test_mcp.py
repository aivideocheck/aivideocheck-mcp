"""MCP server against a fake /v1, over real HTTP and real stdio.

Nothing here touches production: the fake API runs on 127.0.0.1, and both the
remote (Streamable HTTP) and the local (stdio) server are pointed at it
through AVC_API_BASE. The client is the SDK's own, so the test speaks the same
protocol Claude or Cursor would.

Run:  .venv/Scripts/python.exe tests/test_mcp.py      (macOS/Linux: .venv/bin/python)
"""
import asyncio
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import httpx2  # noqa: E402  (the SDK's HTTP client)
import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

GOOD = "avc_live_" + "a" * 32
ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{extra}]" if extra and not cond else ""))
    ok += bool(cond)
    fail += not cond


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── fake /v1 ────────────────────────────────────────────────────────────────
seen = {"posts": [], "uploads": [], "keys": set(), "idem": [], "ips": []}
polls = {}


def _auth(request):
    key = request.headers.get("authorization", "")[7:]
    seen["keys"].add(key)
    seen["ips"].append(request.headers.get("x-real-ip"))
    if key != GOOD:
        return JSONResponse({"error": {"code": "invalid_api_key", "message": "The API key is missing, malformed, unknown or revoked."}}, 401)
    return None


async def create(request: Request):
    if (r := _auth(request)):
        return r
    seen["idem"].append(request.headers.get("idempotency-key"))
    if request.headers.get("content-type", "").startswith("multipart/"):
        form = await request.form()
        f = form["file"]
        seen["uploads"].append((f.filename, len(await f.read())))
    else:
        body = await request.json()
        seen["posts"].append(body.get("url"))
        if "expensive" in body.get("url", ""):
            return JSONResponse({"error": {"code": "insufficient_credits", "message": "Not enough credits for this video.",
                                           "required": 4, "balance": 1}}, 402)
    cid = f"chk{len(polls) + 1}"
    polls[cid] = 0
    return JSONResponse({"id": cid, "status": "queued", "credits_charged": 1, "created_at": "2026-09-30T10:00:00Z"}, 202)


async def get_check(request: Request):
    if (r := _auth(request)):
        return r
    cid = request.path_params["cid"]
    if cid not in polls:
        return JSONResponse({"error": {"code": "not_found", "message": "No check with this id on your account."}}, 404)
    polls[cid] += 1
    if polls[cid] < 2:
        return JSONResponse({"id": cid, "status": "processing", "progress": 0.5, "credits_charged": 1})
    return JSONResponse({"id": cid, "status": "succeeded", "credits_charged": 1, "result": {
        "probability": 72.4, "verdict": "likely_ai", "video_probability": 5.0, "audio_probability": 75.0,
        "provenance": {"present": True, "verified": True, "declares_ai": True, "claim_generator": "Veo",
                       "signer": "Google LLC", "source_type": "generated"},
        "duration_s": 8.0, "file_sha256": "ab" * 32, "model_version": "2.4.2",
        "checked_at": "2026-09-30T10:00:11Z"}})


async def account(request: Request):
    if (r := _auth(request)):
        return r
    return JSONResponse({"key": {"prefix": GOOD[:12]}, "credits": {"balance": 17},
                         "limits": {"requests_per_minute": 60, "checks_in_flight": 5,
                                    "max_video_seconds": 600, "max_upload_mb": 500}})


fake = Starlette(routes=[Route("/v1/checks", create, methods=["POST"]),
                         Route("/v1/checks/{cid}", get_check), Route("/v1/account", account)])


def serve(app, port):
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            return srv
        time.sleep(0.05)
    raise RuntimeError("server did not start")


API_PORT, MCP_PORT = free_port(), free_port()
serve(fake, API_PORT)
os.environ["AVC_API_BASE"] = f"http://127.0.0.1:{API_PORT}"
os.environ["AVC_MCP_WAIT_S"] = "20"
# A key in the server's own environment must NOT be used in HTTP mode.
os.environ["AVC_API_KEY"] = GOOD

import avc_mcp  # noqa: E402
avc_mcp.POLL_S = 0.1
serve(avc_mcp.app, MCP_PORT)

from mcp import Client, StdioServerParameters  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

URL = f"http://127.0.0.1:{MCP_PORT}/mcp"


def text(res):
    return "\n".join(getattr(c, "text", "") for c in res.content)


def http_client(key=None, host=None, ip=None, bare=False):
    headers = {"X-Real-IP": ip} if ip else {}
    if key:
        headers["Authorization"] = key if bare else f"Bearer {key}"
    if host:
        headers["Host"] = host
    return Client(streamable_http_client(URL, http_client=httpx2.AsyncClient(headers=headers, timeout=60)))


async def remote():
    print("\n=== удалённый сервер (Streamable HTTP) ===")
    async with http_client(GOOD) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        check("три инструмента: check_video, get_check, get_balance",
              set(tools) == {"check_video", "get_check", "get_balance"}, str(set(tools)))
        check("check_file удалённо не предлагается — чужой диск сервер не видит", "check_file" not in tools)
        ann = tools["check_video"].annotations
        check("check_video помечен как тратящий (не read-only)", ann is not None and ann.read_only_hint is False)
        check("get_balance помечен как read-only", tools["get_balance"].annotations.read_only_hint is True)

        res = await c.call_tool("check_video", {"url": "https://www.instagram.com/reel/abc"})
        t = text(res)
        check("проверка по ссылке дошла до результата", not res.is_error and "AI signals detected" in t, t[:200])
        check("картинка и звук — отдельными числами", "Picture model: 5.0%" in t and "Sound model: 75.0%" in t, t)
        check("оговорка едет внутри результата", "not proof" in t and "face swapped" in t, t)
        check("подпись C2PA пересказана", "signed by Google LLC" in t and "declares AI generation" in t, t)
        check("запрос к /v1 ушёл с Idempotency-Key", seen["idem"] and seen["idem"][-1], str(seen["idem"]))

        res = await c.call_tool("check_video", {"url": "https://x.com/expensive/1"})
        t = text(res)
        check("нехватка кредитов → понятная ошибка с числами",
              res.is_error and "Not enough credits" in t and "needs 4" in t and "balance is 1" in t, t)

        res = await c.call_tool("get_balance", {})
        check("баланс", not res.is_error and "Balance: 17" in text(res), text(res))

        res = await c.call_tool("get_check", {"check_id": "nope"})
        check("чужой или несуществующий id → ошибка от /v1 без трассировки",
              res.is_error and "No check with this id" in text(res) and "Traceback" not in text(res), text(res))

    async with http_client(GOOD, bare=True) as c:
        res = await c.call_tool("get_balance", {})
        check("ключ без «Bearer » (так его передаёт Smithery) тоже принят",
              not res.is_error and "Balance: 17" in text(res), text(res))

    async with http_client("sk-something-else", bare=True) as c:
        res = await c.call_tool("get_balance", {})
        check("чужая строка без «Bearer » ключом не считается",
              res.is_error and "No AI Video Check API key" in text(res), text(res))

    async with http_client(None) as c:
        res = await c.call_tool("get_balance", {})
        t = text(res)
        check("без ключа → подсказка, где его взять", res.is_error and "No AI Video Check API key" in t, t)
        check("подсказка ведёт на страницу API, а не на старую", "/dashboard/api" in t, t)
    check("ключ из окружения сервера в HTTP-режиме не использован ни разу",
          seen["keys"] == {GOOD} and len(seen["posts"]) == 2, str(seen))

    async with http_client("avc_live_" + "b" * 32) as c:
        res = await c.call_tool("get_balance", {})
        check("неверный ключ → сообщение /v1", res.is_error and "rejected" in text(res), text(res))

    print("\n=== адрес клиента для лимитов /v1 ===")
    seen["ips"].clear()
    async with http_client(GOOD, ip="203.0.113.7") as c:
        await c.call_tool("get_balance", {})
    check("по умолчанию X-Real-IP дальше не передаётся", seen["ips"] == [None], str(seen["ips"]))
    avc_mcp.FORWARD_CLIENT_IP = True
    seen["ips"].clear()
    async with http_client(GOOD, ip="203.0.113.7") as c:
        await c.call_tool("get_balance", {})
    avc_mcp.FORWARD_CLIENT_IP = False
    check("с AVC_MCP_FORWARD_CLIENT_IP=1 адрес клиента доходит до /v1",
          seen["ips"] == ["203.0.113.7"], str(seen["ips"]))

    print("\n=== защита от DNS rebinding ===")
    async with httpx2.AsyncClient() as h:
        r = await h.post(URL, headers={"Host": "evil.example", "Content-Type": "application/json",
                                       "Accept": "application/json, text/event-stream"},
                         content=b'{"jsonrpc":"2.0","id":1,"method":"ping"}')
    check("чужой Host отклонён", r.status_code in (400, 403, 421), f"{r.status_code} {r.text[:100]}")


async def local():
    print("\n=== локальный сервер (stdio) ===")
    tmp = Path(tempfile.mkdtemp(prefix="mcp_local_"))
    clip = tmp / "clip.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 4000)
    secret = tmp / "id_rsa"
    secret.write_bytes(b"-----BEGIN OPENSSH PRIVATE KEY-----\n" + b"x" * 400)
    disguised = tmp / "notes.mp4"
    disguised.write_bytes(b"-----BEGIN OPENSSH PRIVATE KEY-----\n" + b"x" * 400)
    tsfile = tmp / "app.ts"
    tsfile.write_bytes(b"Graph = 1;\n" + b"// typescript\n" * 30)

    env = dict(os.environ, AVC_API_KEY=GOOD, AVC_API_BASE=os.environ["AVC_API_BASE"], AVC_MCP_WAIT_S="20",
               PYTHONIOENCODING="utf-8")
    params = StdioServerParameters(command=sys.executable, args=[str(HERE / "avc_mcp.py")], env=env)
    uploads_before = len(seen["uploads"])
    async with Client(params) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        check("локально есть check_file", "check_file" in tools, str(tools))
        res = await c.call_tool("check_file", {"path": str(clip)})
        check("файл с диска проверен", not res.is_error and "AI signals detected" in text(res), text(res)[:200])
        check("ушёл ровно один файл, с именем", seen["uploads"][uploads_before:] == [("clip.mp4", 4012)],
              str(seen["uploads"]))
        for f, label in ((secret, "id_rsa"), (disguised, "ключ, переименованный в .mp4"), (tsfile, "TypeScript .ts")):
            res = await c.call_tool("check_file", {"path": str(f)})
            check(f"{label} не отправлен", res.is_error and "does not look like a video" in text(res), text(res))
        res = await c.call_tool("check_file", {"path": str(tmp / "missing.mp4")})
        check("нет файла → понятная ошибка", res.is_error and "No file at" in text(res), text(res))
    check("на сервер ушёл только настоящий ролик", len(seen["uploads"]) == uploads_before + 1, str(seen["uploads"]))


asyncio.run(remote())
asyncio.run(local())
print(f"\nитого: {ok} ✓, {fail} ✗")
sys.exit(1 if fail else 0)
