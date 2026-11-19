"""Local filter for the Sub2 user API. Only Nginx and the canvas bridge reach Sub2 directly."""

import asyncio
import json
import os
import re

import aiohttp
from aiohttp import web
from multidict import MultiDict

from managed_keys import PREFIX

UP = os.getenv("SUB2_URL", "http://127.0.0.1:8080")
KEYS = "/api/v1/keys"
TOKEN = re.compile(rb"sk-lcv1-[a-f0-9]{64}")


def private(row):
    return isinstance(row, dict) and str(row.get("key", "")).startswith(PREFIX)


def reject(status, message):
    return web.json_response(
        {"code": status, "message": message}, status=status, headers={"Cache-Control": "no-store"}
    )


def forward_headers(req):
    permitted = (
        "Authorization",
        "Accept-Language",
        "User-Agent",
        "Content-Type",
        "Idempotency-Key",
        "Cookie",
        "X-Request-ID",
    )
    headers = {k: req.headers[k] for k in permitted if k in req.headers}
    addr = req.headers.get("X-Real-IP", req.remote or "")
    headers.update({"X-Real-IP": addr, "X-Forwarded-For": addr})
    return headers


async def upstream(req, path, method="GET", query=None, body=None):
    async with req.app["http"].request(
        method, UP + path, params=query, data=body, headers=forward_headers(req), allow_redirects=False
    ) as response:
        content = await response.read()
        headers = {
            k: response.headers[k]
            for k in ("Content-Type", "Retry-After", "X-Request-ID", "WWW-Authenticate")
            if k in response.headers
        }
        headers["Cache-Control"] = "no-store"
        headers["X-Lumivia-Key-Guard"] = "1"
        return response.status, content, headers


def relay(reply):
    status, content, headers = reply
    return web.Response(status=status, body=TOKEN.sub(b"[managed]", content), headers=headers)


def number(value, default, maximum=(1 << 63) - 1):
    if not value or not re.fullmatch("[0-9]{1,19}", value):
        return default
    parsed = int(value)
    return parsed if 0 < parsed <= maximum else default


async def key_list(req):
    query = MultiDict(req.query)
    size = number(query.get("page_size") or query.get("limit"), 20, 1000)
    page = min(number(query.get("page"), 1), ((1 << 63) - 1) // size)
    offset = (page - 1) * size
    query["page_size"] = "1000"
    query.popall("limit", None)
    items = []
    total = 0
    pos = 0
    while True:
        query["page"] = str(pos // 1000 + 1)
        reply = await upstream(req, KEYS, query=query)
        if reply[0] != 200:
            return relay(reply)
        envelope = json.loads(reply[1])
        data = envelope["data"]
        rows = data["items"] or []
        for row in rows:
            if private(row):
                continue
            if offset <= total < offset + size:
                items.append(row)
            total += 1
        pos += len(rows)
        if pos >= data["total"] or len(rows) < 1000:
            break
    data.update(items=items, page=page, page_size=size, total=total, pages=max(1, (total + size - 1) // size))
    return web.json_response(envelope, headers={"Cache-Control": "no-store"})


async def handle(req):
    path = req.path.rstrip("/")
    if path == KEYS:
        if req.method in ("GET", "HEAD"):
            return await key_list(req)
        if req.method == "POST":
            body = await req.read()
            try:
                fields = json.loads(body)
                if not isinstance(fields, dict):
                    raise ValueError()
                if any(k.lower() == "custom_key" and str(v).startswith(PREFIX) for k, v in fields.items()):
                    return reject(400, "密钥前缀由平台保留")
            except (ValueError, UnicodeDecodeError):
                return reject(400, "无效的密钥请求")
            return relay(await upstream(req, path, req.method, req.query, body))
        return reject(405, "Method not allowed")
    if path.startswith(KEYS + "/"):
        if path == KEYS + "/concurrency":
            reply = await upstream(req, path, req.method, req.query)
            if reply[0] != 200:
                return relay(reply)
            envelope = json.loads(reply[1])
            data = envelope["data"]
            if data.get("items"):
                query = {"search": PREFIX, "page_size": "1000"}
                hidden = set()
                page = 1
                while True:
                    query["page"] = str(page)
                    keys = await upstream(req, KEYS, query=query)
                    if keys[0] != 200:
                        return relay(keys)
                    batch = json.loads(keys[1])["data"]
                    hidden.update(k["id"] for k in batch["items"] or [] if private(k))
                    if page * 1000 >= batch["total"] or not batch["items"]:
                        break
                    page += 1
                if any(item["id"] in hidden for item in data["items"]):
                    return reject(404, "API key not found")
            return relay(reply)
        if not re.fullmatch(re.escape(KEYS) + r"/\+?\d+", path) or req.method not in (
            "GET",
            "HEAD",
            "PUT",
            "DELETE",
        ):
            return reject(404, "API key not found")
        # Sub2 first verifies ownership. Its immutable credential identifies the reserved record.
        reply = await upstream(req, path)
        if reply[0] != 200:
            return relay(reply)
        if private(json.loads(reply[1])["data"]):
            return reject(404, "API key not found")
        if req.method in ("GET", "HEAD"):
            return relay(reply)
    elif not (
        path == "/api/v1/usage"
        or path.startswith("/api/v1/usage/")
        or path in ("/api/v1/auth/me", "/api/v1/user/profile")
    ):
        return reject(404, "Not found")
    return relay(await upstream(req, path, req.method, req.query, await req.read()))


@web.middleware
async def errors(req, handler):
    try:
        return await handler(req)
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, TypeError, ValueError):
        return reject(502, "主站密钥服务暂时不可用")


async def client(app):
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.DummyCookieJar(), timeout=aiohttp.ClientTimeout(total=60)
    ) as http:
        app["http"] = http
        yield


if __name__ == "__main__":
    app = web.Application(middlewares=[errors], client_max_size=4 * 1024 * 1024)
    app.cleanup_ctx.append(client)
    app.router.add_get("/healthz", lambda req: web.Response(text="ok"))
    app.router.add_route("*", "/{path:.*}", handle)
    web.run_app(app, host="127.0.0.1", port=3032, access_log=None)
