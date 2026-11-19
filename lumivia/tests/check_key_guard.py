import sys, asyncio, os, tempfile, json
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import key_guard as guard
import managed_keys as managed
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from urllib.parse import urlsplit, parse_qs


async def main():
    with tempfile.TemporaryDirectory() as td:
        os.environ["CREDENTIALS_DIRECTORY"] = td
        (Path(td) / "canvas-key-seed").write_bytes(os.urandom(32))
        rows = [
            {
                "id": i,
                "key": f"test-key-{i}",
                "name": ("Lumivia Canvas · 88" if i == 1 else managed.NAME),
                "user_id": 7,
                "group_ids": [88],
                "status": "active",
            }
            for i in range(1, 4)
        ]

        async def api(path, payload=None):
            if payload is not None:
                row = dict(rows[0], id=4, key=payload["custom_key"], name=payload["name"])
                rows.insert(1, row)
                return row
            results = [r for r in rows if guard.PREFIX in (r["name"] + r["key"])]
            return {"items": results, "page_size": 100, "total": len(results)}

        req = SimpleNamespace(app={"key_lock": asyncio.Lock()})
        a, b = await asyncio.gather(*[managed.managed_image_key(req, api, 7, 88) for _ in range(2)])
        assert a["id"] == b["id"] == 4 and len(rows) == 4
        token = a["key"]
        hits = []

        async def sub2(r):
            if r.headers.get("Authorization") != "Bearer fixture":
                return web.Response(status=401)
            if r.path.endswith("concurrency"):
                data = {"items": [{"id": int(r.query["ids"])}]}
            elif r.path == "/api/v1/keys":
                if r.method == "POST":
                    hits.append("create")
                    return web.json_response({"data": {"id": 5}})
                out = [x for x in rows if r.query.get("search", "") in (x["key"] + x["name"])]
                data = {"items": out, "total": len(out), "page_size": 1000}
            elif r.path.startswith("/api/v1/keys/"):
                data = next(x for x in rows if x["id"] == int(r.path.split("/")[-1]))
                hits.append((r.method, data["id"]))
            else:
                data = {"items": [{"api_key": a}], "actual_cost": 0.08}
            return web.json_response({"data": data})

        up = web.Application()
        up.router.add_route("*", "/{tail:.*}", sub2)
        async with TestServer(up) as us:
            guard.UP = str(us.make_url("/")).rstrip("/")
            app = web.Application(middlewares=[guard.errors])
            app.cleanup_ctx.append(guard.client)
            app.router.add_route("*", "/{tail:.*}", guard.handle)
            async with TestClient(TestServer(app), headers={"Authorization": "Bearer fixture"}) as client:
                base = "/api/v1/keys"
                data = (await (await client.get(base + "?page_size=2&page=2")).json())["data"]
                assert [r["id"] for r in data["items"]] == [3] and data["total"] == 3 and data["pages"] == 2
                resp = await client.get(base + "?search=sk-lcv1-")
                assert (await resp.json())["data"]["total"] == 0
                for method in ["get", "put", "delete"]:
                    assert (await getattr(client, method)(base + "/4")).status == 404
                assert ("PUT", 4) not in hits and ("DELETE", 4) not in hits
                assert (await client.put(base + "/1", json={"name": "ok"})).status == 200 and (
                    "PUT",
                    1,
                ) in hits
                assert (await client.delete(base + "/2")).status == 200 and ("DELETE", 2) in hits
                assert (
                    await client.post(base, json={"custom_key": token})
                ).status == 400 and "create" not in hits
                assert (await client.post(base, json={"name": "regular"})).status == 200 and "create" in hits
                assert (await client.get(base + "/concurrency?ids=4")).status == 404
                assert (await client.get(base + "/concurrency?ids=1")).status == 200
                resp = await client.get("/api/v1/usage/101")
                body = await resp.text()
                assert token not in body and json.loads(body)["data"]["actual_cost"] == 0.08
                assert (await client.get(base, headers={"Authorization": "Bearer bad"})).status == 401
        print(
            "OK provisioning, concurrent reuse, legacy preservation, hidden pagination/search, CRUD, concurrency, usage redaction, and unauthorized access"
        )


asyncio.run(main())
