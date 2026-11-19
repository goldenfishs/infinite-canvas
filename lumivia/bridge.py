"""Single-worker canvas gateway. Sub2 credentials never leave this process for the canvas client."""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from datetime import datetime, timezone
from ipaddress import ip_address, ip_network

from cloud_storage import CloudStorage
from canvas_policy import CanvasError, MODEL
from jobs import Jobs, bounded_json
from media_gateway import PublicResolver
from managed_keys import PREFIX, managed_image_key

import aiohttp
from aiohttp import web
from yarl import URL

MAIN = "https://api.lumivia.cn"
CANVAS = "https://canvas.lumivia.cn"
UP = os.getenv("SUB2_URL", "http://127.0.0.1:8080")
GROUP = int(os.getenv("IMAGE_GROUP_ID", "88"))
grants = {}
IMAGE_ACTIONS = ("generations", "edits")
routes = web.RouteTableDef()


def failure(status, message):
    return web.json_response({"error": {"message": message}}, status=status)


def digest(s):
    return hashlib.sha256(s.encode()).hexdigest()


@web.middleware
async def protect(req, handler):
    if req.method == "POST":
        origin = MAIN if req.path == "/canvas-connect/approve" else CANVAS
        if req.headers.get("Origin") != origin:
            return failure(403, "来源校验失败")
    try:
        resp = await handler(req)
    except CanvasError as e:
        resp = failure(e.status, e.message)
    except (web.HTTPUnauthorized, web.HTTPForbidden) as e:
        resp = failure(e.status, e.text)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        resp = failure(502, "主站暂时无法响应，请重试")
    resp.headers.update(
        {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}
    )
    return resp


@routes.get("/canvas-entry")
async def entry(req):
    # Sub2 can append auth and navigation parameters to custom-menu URLs.
    # Strip the query before rendering any document or requesting other resources.
    if req.query_string:
        return web.Response(status=303, headers={"Location": "/canvas-entry"})
    html = Path(__file__).with_name("entry.html").read_text()
    return web.Response(
        text=html,
        content_type="text/html",
        headers={
            # WebKit checks iframe Blob downloads against the embedding document's frame-src.
            "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; frame-src {CANVAS} blob:; frame-ancestors 'self'; base-uri 'none'; form-action 'none'",
        },
    )


@routes.get("/canvas-connect/")
async def consent(req):
    nonce = secrets.token_urlsafe(24)
    html = Path(__file__).with_name("consent.html").read_text().replace("SCRIPT_NONCE", nonce)
    return web.Response(
        text=html,
        content_type="text/html",
        headers={
            "Content-Security-Policy": f"default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'; frame-ancestors {MAIN} {CANVAS}; base-uri 'none'; form-action 'none'",
        },
    )


# Key names are user-controlled. The public channel label only contains a key ID and group names.
NON_CHAT = re.compile(
    r"(?:image|embedding|rerank|tts|transcrib|whisper|moderation|video|sora|seedance|dall-e|realtime|veo(?:-|$)|imagen|audio-preview|computer-use)",
    re.I,
)


def remote_ip(req):
    return req.headers.get("X-Real-IP", req.remote or "")


def gateway_headers(req, secret):
    return {
        "Authorization": "Bearer " + secret,
        "X-Real-IP": remote_ip(req),
        "X-Forwarded-For": remote_ip(req),
    }


def personal_channel(key, user_id, group_names, addr):
    groups = key.get("group_ids") or [key.get("group_id")]
    if (
        key.get("user_id") != user_id
        or key.get("status") != "active"
        or not key.get("key")
        or key["key"].startswith(PREFIX)
        or not any(g in group_names and g != GROUP for g in groups)
    ):
        return None
    if key.get("expires_at") and datetime.fromisoformat(
        key["expires_at"].replace("Z", "+00:00")
    ) <= datetime.now(timezone.utc):
        return None
    if key.get("quota") and key.get("quota_used", 0) >= key["quota"]:
        return None
    if key.get("ip_whitelist") or key.get("ip_blacklist"):
        try:
            client = ip_address(addr)

            def matches(rules):
                return any(client in ip_network(rule, strict=False) for rule in rules or [])

            if matches(key.get("ip_blacklist")) or (
                key.get("ip_whitelist") and not matches(key["ip_whitelist"])
            ):
                return None
        except ValueError:
            return None
    return (
        " / ".join(group_names[g] for g in groups if g in group_names and g != GROUP)
        + " · Key #"
        + str(key["id"])
    )


async def model_ids(req, key):
    cache = req.app["models_cache"]
    token = digest(key["key"] + ":" + str(key.get("updated_at")) + ":" + remote_ip(req))
    now = time.monotonic()
    if token in cache and cache[token][0] > now:
        return cache[token][1]
    async with req.app["discovery_slots"]:
        try:
            async with req.app["http"].get(
                UP + "/v1/models",
                headers=gateway_headers(req, key["key"]),
                timeout=aiohttp.ClientTimeout(total=5),
                allow_redirects=False,
            ) as r:
                if r.status != 200:
                    return []
                ids = sorted(
                    {
                        m["id"]
                        for m in (await r.json()).get("data", [])
                        if isinstance(m, dict) and isinstance(m.get("id"), str)
                    }
                )
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return []
    cache[token] = (now + 45, ids)
    return ids


async def chat_channels(req, api, uid, group_names):
    keys = []
    page = 1
    while True:
        batch = await api(f"keys?status=active&page_size=100&page={page}")
        items = batch["items"] or []
        keys.extend(items)
        if not items or len(keys) >= batch["total"]:
            break
        page += 1

    async def discover(key):
        name = personal_channel(key, uid, group_names, remote_ip(req))
        policy = req.app["storage"].policy
        if name and policy.chat_key_allowed(key):
            models = sorted(
                (m for m in await model_ids(req, key) if not NON_CHAT.search(m) and policy.permits("chat", m)),
                # For this group the advertised snapshot can be unavailable while its rolling alias serves.
                key=lambda m: (
                    m != "deepseek-v4.1-flash",
                    bool(re.search(r"[-_](?:\d{6,8}|\d{4}-\d{2}-\d{2})$", m)),
                    m,
                ),
            )
            if models:
                return {"id": str(key["id"]), "name": name, "models": models}

    return [c for c in await asyncio.gather(*(discover(k) for k in keys)) if c]


def issue(challenge, action, subject, model=None):
    grant = secrets.token_urlsafe(32)
    grants[grant] = (time.monotonic() + 20, challenge, action, subject, model)
    return web.json_response({"grant": grant})


@routes.post("/canvas-connect/approve")
async def approve(req):
    data = await req.json()
    if not isinstance(data, dict):
        return failure(400, "账号同步请求格式错误")
    action = data.get("action")
    challenge = data.get("challenge")
    auth = req.headers.get("Authorization", "")
    if action not in ("account", "chat", "storage", "storage-admin", "job-image", "job-video", *IMAGE_ACTIONS) or not auth.startswith("Bearer "):
        return failure(400, "账号同步请求格式错误")
    if action != "account" and (
        not isinstance(challenge, str)
        or not re.fullmatch("[a-f0-9]{64}", challenge)
        or type(data.get("account_id")) is not int
    ):
        return failure(400, "画布授权请求格式错误")
    headers = {
        **gateway_headers(req, ""),
        "Authorization": auth,
        "User-Agent": req.headers.get("User-Agent", ""),
    }

    async def api(path, payload=None):
        async with req.app["http"].request(
            "GET" if payload is None else "POST",
            UP + "/api/v1/" + path,
            json=payload,
            headers=headers,
            allow_redirects=False,
        ) as r:
            if r.status == 401:
                raise web.HTTPUnauthorized(text="请先登录主站")
            if r.status in (403, 404):
                raise web.HTTPForbidden(text="此账号或密钥没有权限，请重新同步")
            if r.status != 200:
                raise ValueError("sub2")
            return (await r.json())["data"]

    user = await api("auth/me")
    uid = user["id"]
    if action != "account" and data["account_id"] != uid:
        return failure(409, "主站账号已切换，请重新同步")
    is_admin = user.get("role") == "admin"
    policy = req.app["storage"].policy
    if action in ("storage", "storage-admin"):
        if action == "storage-admin" and not is_admin:
            return failure(403, "需要乐米主站管理员权限")
        req.app["storage"].remember(uid, user["email"])
        return issue(challenge, action, uid)
    if action == "account" or action.startswith("job-"):
        req.app["storage"].remember(uid, user["email"])
    groups = await api("groups/available")
    names = {g["id"]: g["name"] for g in groups}
    image_group = policy.value["imageGroup"] or GROUP
    group = next((g for g in groups if g["id"] == image_group), None)
    if action == "chat":
        kid = data.get("channel_id")
        model = data.get("model")
        if (
            not isinstance(kid, str)
            or not re.fullmatch("[1-9][0-9]{0,17}", kid)
            or not isinstance(model, str)
            or not MODEL.fullmatch(model)
            or NON_CHAT.search(model)
            or not policy.permits("chat", model)
        ):
            return failure(400, "请选择已同步的对话渠道和模型")
        key = await api("keys/" + kid)
        if not policy.chat_key_allowed(key) or not personal_channel(key, uid, names, remote_ip(req)) or model not in await model_ids(req, key):
            return failure(403, "此密钥或模型已不可用，请重新同步")
        return issue(challenge, action, key["key"], model)
    if action in IMAGE_ACTIONS:
        return failure(409, "画布生图已更新，请刷新页面后重新提交")

    if action in ("job-image", "job-video"):
        kind = action.split("-")[1]
        model = data.get("model")
        if not isinstance(model, str) or not MODEL.fullmatch(model):
            return failure(400, "模型名称无效")
        route = policy.video_routes().get(model) if kind == "video" else (image_group, policy.value["imageMode"])
        if route is None or route[0] not in names or (kind == "image" and not policy.permits(kind, model)):
            return failure(403, "该分组或模型未开放给此账号")
        if kind == "image" and not policy.value["imageModels"] and not re.fullmatch(r"gpt-image-[\w.-]+", model):
            return failure(403, "该分组或模型未开放给此账号")
        key = await managed_image_key(req, api, uid, route[0], kind)
        if model not in await model_ids(req, key):
            return failure(403, "主站分组未提供该模型，请重新同步")
        return issue(challenge, action, (uid, route[0], remote_ip(req)), model)

    async def image_info():
        if not group or not policy.value["imageEnabled"]:
            return [], {}
        key = await managed_image_key(req, api, uid, image_group)
        models = [m for m in await model_ids(req, key) if policy.permits("image", m) and (policy.value["imageModels"] or re.fullmatch(r"gpt-image-[\w.-]+", m))]
        rates = await api("groups/rates")
        rate = (
            group["image_rate_multiplier"]
            if group.get("image_rate_independent")
            else (rates or {}).get(str(image_group), group["rate_multiplier"])
        )
        prices = {
            size: round(group["image_price_" + size] * rate, 6)
            for size in ("1k", "2k", "4k")
            if group.get("image_price_" + size) is not None and not group.get("peak_rate_enabled")
        }
        return models, prices

    async def optional_image_info():
        try:
            models, prices = await image_info()
            return models, prices, "" if models or not group else "暂时无法读取生图模型，请重新同步。"
        except (ValueError, aiohttp.ClientError, asyncio.TimeoutError, web.HTTPForbidden):
            # A full key quota or unavailable image group must not disable personal chat keys.
            return [], {}, "未能同步生图渠道，请检查主站密钥数量或分组权限。"

    async def video_info():
        routes = policy.video_routes()
        eligible = dict.fromkeys(gid for gid, _ in routes.values() if gid in names)

        async def discover(gid):
            try:
                key = await managed_image_key(req, api, uid, gid, "video")
                return gid, set(await model_ids(req, key))
            except (aiohttp.ClientError, TimeoutError, ValueError, web.HTTPForbidden):
                return gid, set()

        catalogs = dict(await asyncio.gather(*(discover(gid) for gid in eligible)))
        return {model: names[gid] for model, (gid, _) in routes.items() if model in catalogs.get(gid, ())}

    async def optional_chat_info():
        if not policy.value["chatEnabled"]:
            return []
        try:
            return await chat_channels(req, api, uid, names)
        except (aiohttp.ClientError, TimeoutError, ValueError, web.HTTPForbidden):
            return []

    (models, prices, image_warning), chat, videos = await asyncio.gather(
        optional_image_info(), optional_chat_info(), video_info()
    )
    return web.json_response(
        {
            "account": {
                "id": uid,
                "isAdmin": is_admin,
                "email": user["email"],
                "group": names.get(image_group, ""),
                "prices": prices,
                "models": models,
                "chatChannels": chat,
                "imageWarning": image_warning,
                "videoModels": list(videos),
                "videoGroups": videos,
                "videoOptions": {m: policy.video_options(m, protocol) for m, (_, protocol) in policy.video_routes().items() if m in videos},
                "videoSeconds": policy.value["videoSeconds"],
            }
        }
    )


def consume(req, action):
    grant_id = req.headers.get("X-Lumivia-Grant", "")
    verifier = req.headers.get("X-Lumivia-Verifier", "")
    grant = grants.get(grant_id)
    if (
        not grant
        or grant[0] <= time.monotonic()
        or grant[2] != action
        or not re.fullmatch("[a-f0-9]{64}", verifier)
        or not secrets.compare_digest(grant[1], digest(verifier))
    ):
        raise web.HTTPUnauthorized(text="当前主站账号授权已失效，请重试")
    return grants.pop(grant_id)


async def relay_chat(req, key, body):
    headers = {**gateway_headers(req, key), "Content-Type": "application/json"}
    async with req.app["http"].post(UP + "/v1/chat/completions", data=body, headers=headers, allow_redirects=False) as source:
        if source.content_type not in ("application/json", "text/event-stream"):
            return failure(502, "主站响应格式异常，请重试")
        response = web.StreamResponse(status=source.status, headers={
            "Content-Type": source.headers["Content-Type"], "Cache-Control": "no-store",
            "X-Accel-Buffering": "no", "X-Content-Type-Options": "nosniff",
        })
        await response.prepare(req)
        async for chunk in source.content.iter_any():
            await response.write(chunk)
        await response.write_eof()
        return response


@routes.post("/lumivia/v1/images/{action:generations|edits}")
async def legacy_images(req):
    return failure(409, "画布生图已更新，请刷新页面后重新提交")


@routes.post("/lumivia/v1/chat/completions")
async def chat(req):
    grant = consume(req, "chat")
    if req.content_type != "application/json":
        return failure(415, "对话需要 JSON 格式")
    body = await req.read()
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        return failure(400, "无效的对话请求")
    if (
        not isinstance(payload, dict)
        or payload.get("model") != grant[4]
        or not req.app["storage"].policy.permits("chat", grant[4])
        or not isinstance(payload.get("messages"), list)
    ):
        return failure(400, "对话模型与本次授权不一致")
    # Re-encode the parsed payload so gateway and verifier see the same model (duplicate JSON keys).
    return await relay_chat(req, grant[3], json.dumps(payload, ensure_ascii=False).encode())


@routes.post("/lumivia/storage/{action:list|upload|read|material|access|delete|admin-list|admin-default|admin-user|admin-settings|admin-save}")
async def storage(req):
    action = req.match_info["action"]
    admin = action.startswith("admin-")
    uid = consume(req, "storage-admin" if admin else "storage")[3]
    store = req.app["storage"]
    if action == "upload":
        return await store.upload(req, uid)
    if req.content_type != "application/json":
        return failure(400, "无效的存储操作")
    data = await bounded_json(req.content)
    if action == "material":
        row = store.get_file(data.get("id"), uid)
        if not row["mime"].startswith("image/"):
            return failure(415, "只支持将图片导入画布")
        if not row["backend"]:
            return store.local_response(row, "preview")
        # Canvas nodes need the actual bytes for local editing. Ordinary downloads use COS directly.
        url = URL(store.read_link(row, "download"), encoded=True)
        async with req.app["public_http"].get(url, allow_redirects=False) as source:
            if source.status != 200 or source.content_length != row["bytes"] or source.headers.get("Content-Encoding", "identity") != "identity":
                return failure(502, "无法读取对象存储原图")
            response = web.StreamResponse(headers={"Content-Type": row["mime"], "Content-Length": str(row["bytes"]),
                "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})
            await response.prepare(req)
            async for chunk in source.content.iter_chunked(65536):
                await response.write(chunk)
            await response.write_eof()
            return response
    return await (store.admin_action(action, uid, data) if admin else store.user_action(action, uid, data))


@routes.post("/lumivia/jobs/{action:image|video|list|get|cancel|admin-jobs}")
async def jobs(req):
    action = req.match_info["action"]
    queue = req.app["jobs"]
    if action in ("image", "video"):
        if req.headers.get("Content-Encoding", "identity") != "identity":
            return failure(415, "不支持压缩的生成请求")
        grant = consume(req, "job-" + action)
        try:
            async with asyncio.timeout(120):
                return await queue.create(req, action, grant[3], grant[4])
        except TimeoutError:
            return failure(408, "任务上传超时，尚未提交主站")
    uid = consume(req, "storage-admin" if action == "admin-jobs" else "storage")[3]
    data = await bounded_json(req.content)
    if action == "admin-jobs":
        return queue.admin_summary(data)
    if action == "list":
        return queue.list(uid, data)
    return queue.get(uid, data) if action == "get" else queue.cancel(uid, data)


@routes.get("/lumivia/file/{ticket}")
async def signed_file(req):
    return req.app["storage"].from_ticket(req.match_info["ticket"])


@routes.get("/lumivia/health")
async def health(req):
    return web.json_response({"ready": not req.app["worker"].done()}, status=200 if not req.app["worker"].done() else 503)


async def lifecycle(app):
    app["storage"] = CloudStorage(os.environ["STATE_DIRECTORY"])
    app["http"] = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=600), cookie_jar=aiohttp.DummyCookieJar()
    )
    app["public_http"] = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=160), connector=aiohttp.TCPConnector(resolver=PublicResolver()),
        cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False, headers={"Accept-Encoding": "identity"},
    )
    app["jobs"] = Jobs(app["storage"], UP, app["http"], app["public_http"], GROUP)
    app["worker"] = asyncio.create_task(app["jobs"].run())
    app["key_lock"] = asyncio.Lock()
    app["models_cache"] = {}
    app["discovery_slots"] = asyncio.Semaphore(12)

    async def expire():
        while True:
            await asyncio.sleep(60)
            for store in (grants, app["models_cache"]):
                for key, value in list(store.items()):
                    if value[0] <= time.monotonic():
                        store.pop(key, None)

    async def cleanup_files():
        while True:
            await app["storage"].sweep()
            await asyncio.sleep(60)

    tasks = [asyncio.create_task(expire()), asyncio.create_task(cleanup_files()), app["worker"]]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await app["http"].close()
        await app["public_http"].close()
        app["storage"].db.close()


if __name__ == "__main__":
    logging.basicConfig()
    logging.getLogger("canvas.images").setLevel(logging.INFO)
    app = web.Application(middlewares=[protect], client_max_size=32 * 1024 * 1024)
    app.cleanup_ctx.append(lifecycle)
    app.add_routes(routes)
    web.run_app(app, host="127.0.0.1", port=3031, access_log=None)
