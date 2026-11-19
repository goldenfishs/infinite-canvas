import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import bridge
from cloud_storage import GIB

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c020000000b4944415478da6360f80f00010201002718e3660000000049454e44ae426082")


class StorageTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        env = patch.dict("os.environ", {"STATE_DIRECTORY": directory.name})
        env.start()
        self.addCleanup(env.stop)
        bridge.grants.clear()

        async def identity(req):
            token = req.headers.get("Authorization")
            names = {"Bearer member": (7, "user"), "Bearer other": (8, "user"), "Bearer admin": (9, "admin")}
            if token not in names:
                return web.Response(status=401)
            uid, role = names[token]
            return web.json_response({"data": {"id": uid, "email": f"{uid}@example.test", "role": role}})

        upstream = web.Application()
        upstream.router.add_get("/api/v1/auth/me", identity)
        self.upstream = TestServer(upstream)
        await self.upstream.start_server()
        up = patch.object(bridge, "UP", str(self.upstream.make_url("/")).rstrip("/"))
        up.start()
        self.addCleanup(up.stop)
        self.addAsyncCleanup(self.upstream.close)
        self.app = web.Application(middlewares=[bridge.protect])
        self.app.cleanup_ctx.append(bridge.lifecycle)
        self.app.add_routes(bridge.routes)
        self.client = TestClient(TestServer(self.app))
        self.addAsyncCleanup(self.client.close)
        await self.client.start_server()
        self.app["storage"].reserve = 0

    async def grant(self, who, kind, *, role="user", account_id=None):
        uid = {"member": 7, "other": 8, "admin": 9}[who]
        verifier = "6" * 64
        reply = await self.client.post("/canvas-connect/approve", headers={"Origin": bridge.MAIN, "Authorization": "Bearer " + who}, json={
            "action": kind, "challenge": hashlib.sha256(verifier.encode()).hexdigest(), "account_id": uid if account_id is None else account_id, "role": role,
        })
        body = await reply.json()
        return reply.status, {"Origin": bridge.CANVAS, "X-Lumivia-Grant": body.get("grant", ""), "X-Lumivia-Verifier": verifier}

    async def operation(self, who, action, payload=None, *, raw=None, headers=None):
        if headers is None:
            status, headers = await self.grant(who, "storage-admin" if action.startswith("admin-") else "storage")
            self.assertEqual(status, 200)
        form = {"json": payload or {}} if raw is None else {"data": raw}
        return await self.client.post("/lumivia/storage/" + action, headers=headers, **form)

    async def upload(self, who, blob=PNG):
        status, headers = await self.grant(who, "storage")
        self.assertEqual(status, 200)
        headers.update({"X-Canvas-Bytes": str(len(blob)), "Content-Type": "image/png"})
        return await self.operation(who, "upload", raw=blob, headers=headers)

    async def test_owner_roles_quota_and_delete(self):
        self.assertEqual((await self.grant("member", "storage-admin", role="admin"))[0], 403)
        self.assertEqual((await self.grant("member", "storage", account_id=8))[0], 409)
        _, headers = await self.grant("member", "storage")
        self.assertEqual((await self.operation("member", "admin-list", headers=headers)).status, 401)
        listing = await self.operation("member", "list", headers=headers)
        self.assertEqual((await listing.json())["quota"], GIB)
        self.assertEqual((await self.operation("member", "list", headers=headers)).status, 401)
        self.assertEqual((await self.upload("member", b"no image")).status, 400)
        self.assertEqual((await self.operation("admin", "admin-default", {"quota": len(PNG)})).status, 200)
        stored = await self.upload("member")
        self.assertEqual(stored.status, 201)
        key = (await stored.json())["id"]
        own = await self.operation("member", "list")
        self.assertEqual((await own.json())["used"], len(PNG))
        self.assertEqual((await self.upload("member")).status, 413)
        self.assertEqual((await self.operation("other", "read", {"id": key})).status, 404)
        self.assertEqual((await self.operation("other", "delete", {"id": key})).status, 404)
        self.assertEqual((await self.operation("admin", "admin-user", {"userId": 7, "quota": 0})).status, 200)
        read = await self.operation("member", "read", {"id": key})
        self.assertEqual(await read.read(), PNG)
        self.assertEqual(read.headers["Content-Type"], "image/png")
        self.assertEqual(read.headers["Cache-Control"], "no-store")
        deleted = await self.operation("member", "delete", {"id": key})
        self.assertEqual((await deleted.json())["used"], 0)
        self.assertEqual((await self.upload("member")).status, 413)
        self.assertEqual((await self.operation("admin", "admin-user", {"userId": 7, "quota": None})).status, 200)
        self.assertEqual((await self.upload("member")).status, 201)
        report = await (await self.operation("admin", "admin-list", {"search": "7@example.test"})).json()
        self.assertEqual(report["items"], [{"id": 7, "email": "7@example.test", "quota": None, "used": len(PNG)}])

    async def test_pending_upload_reserves_capacity_and_disk(self):
        self.assertEqual((await self.operation("admin", "admin-default", {"quota": len(PNG)})).status, 200)
        status, headers = await self.grant("member", "storage")
        self.assertEqual(status, 200)
        headers.update({"X-Canvas-Bytes": str(len(PNG)), "Content-Type": "image/png"})
        release = asyncio.Event()

        async def slowly():
            yield PNG[:8]
            await release.wait()
            yield PNG[8:]

        inflight = asyncio.create_task(self.operation("member", "upload", raw=slowly(), headers=headers))
        try:
            storage = self.app["storage"]
            async with asyncio.timeout(5):
                while not storage.pending:
                    await asyncio.sleep(0.01)
            self.assertEqual((await self.upload("member")).status, 413)
            storage.total_limit = len(PNG)
            self.assertEqual((await self.upload("other")).status, 507)
            storage.total_limit = GIB
            storage.reserve = 10 ** 20
            self.assertEqual((await self.upload("other")).status, 507)
        finally:
            release.set()
            self.assertEqual((await inflight).status, 201)
        self.assertEqual(len(list((self.path / "images").iterdir())), 1)
