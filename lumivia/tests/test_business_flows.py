import asyncio
import base64
import hashlib
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

import bridge
from cloud_storage import now_ms
from cos_storage import Cos
from managed_keys import PREFIX, managed_secret
from media_gateway import safe_url
from canvas_policy import CanvasError

PNG_IO = io.BytesIO()
Image.new("RGB", (640, 480), (20, 80, 160)).save(PNG_IO, "PNG")
PNG = PNG_IO.getvalue()
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"fallback-content"
IMAGE = "gpt-image-2"
VIDEO = "doubao-seedance-2-0"
CHAT = "gpt-test-chat"
IDENTITIES = {"member": (7, "user"), "other": (8, "user"), "admin": (9, "admin")}


class BusinessFlows(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        credential = self.root / "credential"
        credential.mkdir()
        (credential / "canvas-key-seed").write_bytes(b"x" * 32)
        patcher = patch.dict("os.environ", {"STATE_DIRECTORY": str(self.root), "CREDENTIALS_DIRECTORY": str(credential)})
        patcher.start()
        self.addCleanup(patcher.stop)
        bridge.grants.clear()
        self.calls, self.keys = [], {}
        self.mode = "async"
        self.image_ready = False
        self.video_ready = True
        self.reply_size = None
        upstream = web.Application()
        upstream.router.add_get("/api/v1/auth/me", self.identity)
        upstream.router.add_get("/api/v1/groups/available", lambda r: web.json_response({"data": [
            {"id": 88, "name": "生图", "rate_multiplier": 1, "image_price_1k": 0.05},
            {"id": 99, "name": "视频"}, {"id": 18, "name": "对话"}]}))
        upstream.router.add_get("/api/v1/groups/rates", lambda r: web.json_response({"data": {}}))
        upstream.router.add_get("/api/v1/keys", self.list_keys)
        upstream.router.add_post("/api/v1/keys", self.create_key)
        upstream.router.add_get("/api/v1/keys/17", self.chat_key)
        upstream.router.add_get("/v1/models", self.models)
        upstream.router.add_post("/v1/images/{action:generations|edits}{async:(?:/async)?}", self.submit_image)
        upstream.router.add_get("/v1/images/tasks/image-ref", self.poll_image)
        upstream.router.add_post("/api/v3/contents/generations/tasks", self.submit_video)
        upstream.router.add_get("/api/v3/contents/generations/tasks/video-ref", self.poll_video)
        upstream.router.add_post("/v1/videos", self.submit_compat_video)
        upstream.router.add_get("/v1/videos/compat-ref", lambda r: web.json_response({"id": "compat-ref", "status": "completed"}))
        upstream.router.add_get("/v1/videos/compat-ref/content", lambda r: web.Response(body=MP4, content_type="video/mp4"))
        upstream.router.add_get("/v1/videos/minimax-ref", lambda r: web.json_response({
            "id": "minimax-ref", "status": "completed", "metadata": {"url": "https://assets.test/video.mp4"}}))
        self.upstream = TestServer(upstream)
        await self.upstream.start_server()
        self.addAsyncCleanup(self.upstream.close)
        up = patch.object(bridge, "UP", str(self.upstream.make_url("/")).rstrip("/"))
        up.start()
        self.addCleanup(up.stop)
        await self.start_canvas()

    async def start_canvas(self):
        self.app = web.Application(middlewares=[bridge.protect])
        self.app.cleanup_ctx.append(bridge.lifecycle)
        self.app.add_routes(bridge.routes)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.app["storage"].reserve = 0

    def uid(self, req):
        return IDENTITIES[req.headers["Authorization"].split(" ")[1]][0]

    async def identity(self, req):
        uid, role = IDENTITIES[req.headers["Authorization"].split(" ")[1]]
        return web.json_response({"data": {"id": uid, "email": f"{uid}@example.test", "role": role}})

    def user_chat_key(self, user):
        return {"id": 17, "user_id": user, "group_id": 18, "status": "active", "key": "fixture-chat"}

    async def chat_key(self, req):
        return web.json_response({"data": self.user_chat_key(self.uid(req))})

    async def list_keys(self, req):
        uid = self.uid(req)
        items = [k for k in self.keys.values() if k["user_id"] == uid]
        if req.query.get("search") != PREFIX:
            items += [self.user_chat_key(uid)]
        return web.json_response({"data": {"items": items, "total": len(items), "page_size": 100}})

    async def create_key(self, req):
        uid, d = self.uid(req), await req.json()
        item = {"id": len(self.keys) + 20, "user_id": uid, "group_id": d["group_id"], "status": "active", "key": d["custom_key"]}
        self.keys[item["key"]] = item
        return web.json_response({"data": item})

    async def models(self, req):
        token = req.headers["Authorization"].removeprefix("Bearer ")
        catalog = [CHAT] if token == "fixture-chat" else {[88][0]: [IMAGE], 99: [VIDEO]}[self.keys[token]["group_id"]]
        return web.json_response({"data": [{"id": m} for m in catalog]})

    async def submit_image(self, req):
        action = req.match_info["action"]
        native = bool(req.match_info["async"])
        self.calls.append(("post-image", action, native, req.headers["Authorization"]))
        if self.mode == "uncertain":
            return web.Response(status=502)
        if native and self.mode == "disabled":
            return web.json_response({"error": {"message": "async image tasks are not enabled"}}, status=404)
        if action == "edits":
            form = await req.post()
            refs = form.getall("image[]", []) or form.getall("image", [])
            assert refs and all(ref.file.read() == PNG for ref in refs) and form["model"] == IMAGE
        else:
            form = await req.json()
            assert form["model"] == IMAGE
        if native:
            return web.json_response({"id": "image-ref", "status": "processing"}, status=202)
        return web.json_response({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

    async def poll_image(self, req):
        self.calls.append(("get-image", req.headers["Authorization"]))
        if not self.image_ready:
            return web.json_response({"status": "processing"})
        return web.json_response({"status": "completed", "result": {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}})

    async def submit_video(self, req):
        self.calls.append(("post-seedance", req.headers["Authorization"]))
        body = await req.json()
        assert body["duration"] == 4 and body["resolution"] == "720p" and body["content"][0]["text"] == "motion"
        return web.json_response({"id": "video-ref"})

    async def poll_video(self, req):
        self.calls.append(("get-video", req.headers["Authorization"]))
        return web.json_response({"id": "video-ref", "status": "succeeded", "content": {"video_url": "https://assets.test/video.mp4"}})

    async def submit_compat_video(self, req):
        if req.content_type == "application/json":
            self.calls.append(("post-minimax", req.headers["Authorization"]))
            body = await req.json()
            assert body == {"seconds": "10", "size": "1366x768", "model": VIDEO, "prompt": "motion"}, body
            return web.json_response({"task_id": "minimax-ref"})
        assert req.content_type == "multipart/form-data"
        body = await req.post()
        assert body["seconds"] == "4" and body["model"] == VIDEO
        return web.json_response({"id": "compat-ref"})

    async def grant(self, who, action, *, model=None, channel_id=None):
        verify = "6" * 64
        uid = IDENTITIES[who][0]
        approve = await self.client.post("/canvas-connect/approve", headers={"Origin": bridge.MAIN, "Authorization": "Bearer " + who}, json={
            "action": action, "model": model, "account_id": uid, "challenge": hashlib.sha256(verify.encode()).hexdigest(), "channel_id": channel_id})
        data = await approve.json()
        return approve.status, data, {"Origin": bridge.CANVAS, "X-Lumivia-Grant": data.get("grant", ""), "X-Lumivia-Verifier": verify}

    async def post(self, who, path, payload=None, *, form=None, headers=None, model=None):
        action = path.rsplit("/", 1)[-1]
        kind = "job-" + action if path.startswith("jobs/") and action in ("image", "video") else "storage-admin" if action.startswith("admin-") else "storage"
        code, data, grant = await self.grant(who, kind, model=model)
        assert code == 200, data
        params = {"data": form} if form is not None else {"json": payload or {}}
        return await self.client.post("/lumivia/" + path, headers={**grant, **(headers or {})}, **params)

    async def policy(self, changes, cos=None):
        config = await (await self.post("admin", "storage/admin-settings")).json()
        response = await self.post("admin", "storage/admin-save", {"revision": config["revision"], "changes": changes, **({"cos": cos} if cos else {})})
        data = await response.json()
        self.assertEqual(response.status, 200, data)

    async def wait_state(self, task, states, timeout=8):
        async with asyncio.timeout(timeout):
            while True:
                row = self.app["jobs"].db.execute("SELECT * FROM canvas_jobs WHERE id=?", (task,)).fetchone()
                if row and row["status"] in states:
                    return row
                await asyncio.sleep(0.025)

    async def image(self, key=None, reference=False):
        job = {"id": key or uuid.uuid4().hex, "model": IMAGE, "prompt": "a fixture image", "size": "1024x1024"}
        form = None
        if reference:
            form = aiohttp.FormData()
            form.add_field("spec", json.dumps(job))
            form.add_field("reference", PNG, filename="fixture.png", content_type="image/png")
        response = await self.post("member", "jobs/image", job, form=form, model=IMAGE)
        self.assertEqual(response.status, 202, await response.text())
        return (await response.json())["id"]

    async def test_async_restart_and_idempotency(self):
        key = await self.image(reference=True)
        await self.wait_state(key, ("waiting",))
        await self.image(key)
        self.assertEqual(len([c for c in self.calls if c[0] == "post-image"]), 1)
        await self.client.close()
        self.image_ready = True
        await self.start_canvas()
        row = await self.wait_state(key, ("completed", "review", "failed"))
        self.assertEqual(row["status"], "completed", row["message"])
        self.assertEqual([c[0] for c in self.calls], ["post-image", "get-image"])
        self.assertTrue(all(c[-1] == "Bearer " + managed_secret(7, 88) for c in self.calls))
        own = await (await self.post("member", "jobs/list")).json()
        self.assertEqual(len(own["items"]), 1)
        asset = own["items"][0]["assets"][0]
        self.assertEqual(asset["bytes"], len(PNG))
        self.assertTrue(asset["thumbUrl"])
        other = await (await self.post("other", "jobs/list")).json()
        self.assertEqual(other["items"], [])
        denied = await self.post("other", "storage/access", {"id": asset["id"]})
        self.assertEqual(denied.status, 404)
        access = await (await self.post("member", "storage/access", {"id": asset["id"]})).json()
        response = await self.client.get(access["url"])
        self.assertEqual(await response.read(), PNG)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        thumbs = await self.client.get(asset["thumbUrl"])
        self.assertEqual(thumbs.content_type, "image/jpeg")
        self.assertEqual(Image.open(io.BytesIO(await thumbs.read())).width, 384)
        self.assertEqual(self.app["storage"].held(), 0)

    async def test_fragmented_upload_memory_pause_and_resume(self):
        key = uuid.uuid4().hex
        spec = {"id": key, "model": IMAGE, "prompt": "test multi reference", "count": 2}
        form = aiohttp.MultipartWriter("form-data")
        form.append(json.dumps(spec)).set_content_disposition("form-data", name="spec")
        for n in range(2):
            form.append(PNG, {"Content-Type": "image/png"}).set_content_disposition("form-data", name="reference", filename=f"fixture-{n}.png")
        body = await form.as_bytes()

        async def fragments():
            for start in range(0, len(body), 19):
                yield body[start:start + 19]
                await asyncio.sleep(0)

        with patch("jobs.runtime_memory", return_value=512 * 1024 ** 2):
            denied = await self.post("member", "jobs/image", spec, model=IMAGE)
            self.assertEqual(denied.status, 503)
            self.assertEqual(self.app["storage"].held(), 0)
        with patch("jobs.runtime_memory", side_effect=[0] + [512 * 1024 ** 2] * 20):
            accepted = await self.post("member", "jobs/image", form=fragments(), headers={"Content-Type": form.content_type}, model=IMAGE)
            self.assertEqual(accepted.status, 202, await accepted.text())
            await asyncio.sleep(0.1)
            row = await (await self.post("member", "jobs/get", {"id": key})).json()
            self.assertEqual(row["status"], "queued")
            self.assertEqual(len(list(self.app["storage"].stage.glob(key + ".ref*"))), 2)
            self.assertEqual(self.calls, [])
        self.image_ready = True
        self.app["jobs"].wake.set()
        row = await self.wait_state(key, ("completed", "review", "failed"))
        self.assertEqual(row["status"], "completed", row["message"])
        self.assertEqual(len([call for call in self.calls if call[0] == "post-image"]), 1)
        task = self.app["jobs"].summary(row)
        self.assertEqual(len(task["assets"]), 1)
        fetched = await self.post("member", "storage/material", {"id": task["assets"][0]["id"]})
        self.assertEqual(await fetched.read(), PNG)

    async def test_fallback_explicit_only_and_no_duplicate_after_502(self):
        self.mode = "disabled"
        a = await self.image()
        terminal = await self.wait_state(a, ("completed", "review", "failed"))
        self.assertEqual(terminal["status"], "completed", terminal["message"])
        self.assertEqual([c[2] for c in self.calls], [True, False])
        self.mode = "uncertain"
        b = await self.image()
        checked = await self.wait_state(b, ("completed", "review", "failed"))
        self.assertEqual(checked["status"], "review", checked["message"])
        await self.image(b)
        self.assertEqual([c[2] for c in self.calls], [True, False, True])

    async def test_admin_video_adapters_and_catalog(self):
        # A user cannot promote the browser's asserted role or enable video on their own.
        self.assertEqual((await self.grant("member", "storage-admin"))[0], 403)
        self.assertEqual((await self.grant("member", "job-video", model=VIDEO))[0], 403)
        await self.policy({"videoEnabled": True, "videoGroup": 99, "videoModels": [VIDEO], "chatModels": [CHAT]})
        _, account, _ = await self.grant("member", "account")
        self.assertEqual(account["account"]["videoModels"], [VIDEO])
        self.assertEqual(account["account"]["chatChannels"][0]["models"], [CHAT])
        self.assertEqual((await self.grant("member", "chat", model=CHAT, channel_id="17"))[0], 200)
        await self.policy({"chatEnabled": False})
        self.assertEqual((await self.grant("member", "chat", model=CHAT, channel_id="17"))[0], 400)

        async def mock_media(session, url, allowed, path, maximum):
            self.assertEqual(url, "https://assets.test/video.mp4")
            path.write_bytes(MP4)

        for protocol in ("seedance", "openai"):
            await self.policy({"videoProtocol": protocol})
            key = uuid.uuid4().hex
            with patch("media_gateway.download_media", mock_media):
                accepted = await self.post("member", "jobs/video", {"id": key, "model": VIDEO, "prompt": "motion", "seconds": 4, "ratio": "16:9", "resolution": "720p"}, model=VIDEO)
                self.assertEqual(accepted.status, 202, await accepted.text())
                row = await self.wait_state(key, ("completed", "review", "failed"))
                self.assertEqual(row["status"], "completed", row["message"])
                asset = self.app["jobs"].summary(row)["assets"][0]
                self.assertEqual(asset["mime"], "video/mp4")
                link = await (await self.post("member", "storage/access", {"id": asset["id"], "preview": True})).json()
                range_response = await self.client.get(link["url"], headers={"Range": "bytes=0-7"})
                self.assertEqual(range_response.status, 206)
                self.assertEqual(await range_response.read(), MP4[:8])
        await self.policy({"videoGroup": 0, "videoModels": [], "extraVideoRoutes": [
            {"model": VIDEO, "group": 99, "protocol": "minimax", "resolutions": ["768p"], "durations": [10, 12]}]})
        _, account, _ = await self.grant("member", "account")
        self.assertEqual(account["account"]["videoOptions"][VIDEO], {"resolutions": ["768p"], "durations": [10, 12]})
        payload = {"id": uuid.uuid4().hex, "model": VIDEO, "prompt": "motion", "seconds": 10, "ratio": "16:9", "resolution": "768p"}
        rejected = await self.post("member", "jobs/video", {**payload, "seconds": 6}, model=VIDEO)
        self.assertEqual(rejected.status, 400)
        with patch("media_gateway.download_media", mock_media):
            accepted = await self.post("member", "jobs/video", payload, model=VIDEO)
            self.assertEqual(accepted.status, 202, await accepted.text())
            row = await self.wait_state(payload["id"], ("completed", "review", "failed"))
            self.assertEqual(row["status"], "completed", row["message"])
            self.assertEqual(self.app["jobs"].summary(row)["assets"][0]["mime"], "video/mp4")
        self.assertTrue(all(c[-1] == "Bearer " + managed_secret(7, 99, "video") for c in self.calls))

    async def test_cos_rotation_local_compat_and_expiry(self):
        store = self.app["storage"]
        key = uuid.uuid4().hex
        store.remember(7, "7@example.test")
        (store.files / key).write_bytes(PNG)
        with store.db:
            store.db.execute("INSERT INTO files VALUES(?,?,?,?,?)", (key, 7, "image/png", len(PNG), now_ms()))
        profile = {"bucket": "fixture-12345678", "region": "ap-hongkong", "domain": "", "secretId": "FixtureSecretID99", "secretKey": "FixtureSecretKey99"}
        objects = {}

        def put(cos, name, path, mime):
            objects[(cos.bucket, name)] = path.read_bytes()

        def delete(cos, name):
            del objects[(cos.bucket, name)]

        with patch.object(Cos, "verify"), patch.object(Cos, "put", put), patch.object(Cos, "delete", delete):
            await self.policy({"backend": "cos", "retentionDays": 9}, profile)
            result = await self.post("member", "storage/upload", form=PNG, headers={"X-Canvas-Bytes": str(len(PNG)), "Content-Type": "image/png"})
            self.assertEqual(result.status, 201, await result.text())
            asset = (await result.json())["id"]
            self.assertEqual(objects[(profile["bucket"], asset)], PNG)
            ticket = await (await self.post("member", "storage/access", {"id": asset})).json()
            self.assertEqual(urlsplit(ticket["url"]).hostname, "fixture-12345678.cos.ap-hongkong.myqcloud.com")
            preview = await (await self.post("member", "storage/access", {"id": asset, "preview": True})).json()
            self.assertEqual(preview["previewQuality"], "thumbnail")
            self.assertEqual((await self.client.get(preview["url"])).content_type, "image/jpeg")
            cfg = await (await self.post("admin", "storage/admin-settings")).text()
            self.assertNotIn("FixtureSecret", cfg)
            for enc in store.db.execute("SELECT secret_id,secret_key FROM canvas_cos").fetchone():
                self.assertNotIn(b"FixtureSecret", enc)
            await self.policy({"backend": "local", "retentionDays": 0})
            old = await self.post("member", "storage/read", {"id": key})
            self.assertEqual(await old.read(), PNG)
            listing = await (await self.post("member", "storage/list")).json()
            by_id = {item["id"]: item for item in listing["items"]}
            self.assertIsNone(by_id[key]["expires"])
            self.assertEqual(by_id[asset]["location"], "cos")
            self.assertGreater(by_id[asset]["expires"], now_ms())
            with store.db:
                store.db.execute("UPDATE canvas_media SET expires=? WHERE id=?", (now_ms() - 1, asset))
            self.assertEqual((await self.post("member", "storage/access", {"id": asset})).status, 404)
            await store.sweep()
            self.assertEqual(objects, {})
            self.assertEqual(store.usage(7)["used"], len(PNG))

    async def test_public_https_rules(self):
        self.assertEqual(safe_url("https://cdn.example.test/media.mp4?a=b", ["cdn.example.test"]), "https://cdn.example.test/media.mp4?a=b")
        for address in ("https://127.0.0.1/", "https://[::ffff:127.0.0.1]/", "https://169.254.169.254/", "http://media.example/", "https://u@media.example/"):
            with self.assertRaises(CanvasError):
                safe_url(address, [])
        with self.assertRaises(CanvasError):
            safe_url("https://cdn.example.test/file", ["other.example.test"])
