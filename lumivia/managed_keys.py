"""Provision a private image key using the authenticated user's normal group permissions."""

import hmac
import os
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import web

PREFIX = "sk-lcv1-"
NAME = "乐米画布 · 平台生图"


def managed_secret(user_id, group):
    seed = (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "canvas-key-seed").read_bytes()
    if len(seed) != 32:
        raise RuntimeError("Invalid canvas credential seed")
    return PREFIX + hmac.digest(seed, f"image:{user_id}:{group}".encode(), "sha256").hex()


async def managed_image_key(req, api, user_id, group):
    secret = managed_secret(user_id, group)
    async with req.app["key_lock"]:
        # Only the public prefix enters upstream URLs and access logs.
        page = 1
        while True:
            batch = await api("keys?" + urlencode({"search": PREFIX, "page_size": 100, "page": page}))
            for item in batch["items"]:
                if hmac.compare_digest(item["key"].encode(), secret.encode()):
                    return checked(item, user_id, group, secret)
            if not batch["items"] or page * batch["page_size"] >= batch["total"]:
                break
            page += 1
        item = await api("keys", {"name": NAME, "group_id": group, "custom_key": secret})
        return checked(item, user_id, group, secret)


def checked(item, user_id, group, secret):
    if (
        item.get("user_id") != user_id
        or item.get("status") != "active"
        or (item.get("group_ids") or [item.get("group_id")]) != [group]
        or not hmac.compare_digest(str(item.get("key", "")).encode(), secret.encode())
    ):
        raise web.HTTPForbidden(text="平台生图密钥状态或所属分组异常")
    return item
