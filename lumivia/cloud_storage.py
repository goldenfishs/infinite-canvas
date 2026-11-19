"""Private, disk-backed canvas images. Only the bridge may supply an authenticated owner."""

import asyncio
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

from aiohttp import web

GIB = 1024 ** 3
MIB = 1024 ** 2
MAX_FILE = 32 * MIB
MAX_QUOTA = 1024 * GIB
TOTAL_LIMIT = 8 * GIB
TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif", "image/avif": "avif"}
PAGE_SIZE = 20


def error(status, message):
    return web.json_response({"error": {"message": message}}, status=status)


def is_image(mime, prefix):
    return {
        "image/png": prefix.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": prefix.startswith(b"\xff\xd8\xff"),
        "image/webp": prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP",
        "image/gif": prefix.startswith((b"GIF87a", b"GIF89a")),
        "image/avif": prefix[4:12] in (b"ftypavif", b"ftypavis"),
    }[mime]


class CloudStorage:
    def __init__(self, directory, reserve=4 * GIB):
        self.files = Path(directory) / "images"
        self.files.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.reserve = reserve
        self.total_limit = int(os.getenv("CANVAS_STORAGE_LIMIT_BYTES", TOTAL_LIMIT))
        self.pending = {}
        self.db = sqlite3.connect(Path(directory) / "storage.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id = 1), quota INTEGER NOT NULL);
            INSERT OR IGNORE INTO settings VALUES (1, 1073741824);
            CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, quota INTEGER);
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY, owner INTEGER NOT NULL, mime TEXT NOT NULL, bytes INTEGER NOT NULL, created INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS files_owner_created ON files(owner, created DESC, id DESC);
        """)
        # Recover a write interrupted between filesystem rename and SQLite commit.
        ids = {row[0] for row in self.db.execute("SELECT id FROM files")}
        for path in self.files.iterdir():
            if re.fullmatch(r"[0-9a-f]{32}(?:\.part)?", path.name) and path.name not in ids:
                path.unlink(missing_ok=True)
        with self.db:
            self.db.executemany("DELETE FROM files WHERE id = ?", [(i,) for i in ids if not (self.files / i).is_file()])

    def remember(self, uid, email):
        with self.db:
            self.db.execute("INSERT INTO users(id, email) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET email = excluded.email WHERE users.email != excluded.email", (uid, email))

    def usage(self, uid):
        quota, default = self.db.execute("SELECT users.quota, settings.quota FROM users CROSS JOIN settings WHERE users.id = ?", (uid,)).fetchone()
        used = self.db.execute("SELECT COALESCE(SUM(bytes), 0) FROM files WHERE owner = ?", (uid,)).fetchone()[0]
        return {"used": used, "quota": default if quota is None else quota, "defaultQuota": default}

    def total_used(self):
        return self.db.execute("SELECT COALESCE(SUM(bytes), 0) FROM files").fetchone()[0]

    async def upload(self, req, uid):
        mime = req.content_type
        size = req.headers.get("X-Canvas-Bytes", "")
        if mime not in TYPES or req.headers.get("Content-Encoding", "identity") != "identity":
            return error(415, "只支持 PNG、JPG、WebP、GIF 或 AVIF 图片")
        if not re.fullmatch(r"[1-9][0-9]{0,8}", size) or not 0 < int(size) <= MAX_FILE:
            return error(413, "单张图片最多 32 MiB")
        size = int(size)
        if req.content_length is not None and req.content_length != size:
            return error(400, "文件长度不符")
        usage = self.usage(uid)
        if usage["used"] + sum(n for owner, n in self.pending.values() if owner == uid) + size > usage["quota"]:
            return error(413, "云图库容量已满，请删除旧图或联系管理员")
        if len(self.pending) >= 12 or sum(owner == uid for owner, _ in self.pending.values()) >= 3:
            return error(429, "正在保存的图片较多，请稍后重试")
        prospective = sum(n for _, n in self.pending.values()) + size
        if self.total_used() + prospective > self.total_limit:
            return error(507, "云图库总容量已满，请联系管理员")
        if shutil.disk_usage(self.files).free - prospective < self.reserve:
            return error(507, "服务器磁盘空间不足，暂时不能存图")
        key = uuid.uuid4().hex
        self.pending[key] = (uid, size)
        temp, final = self.files / (key + ".part"), self.files / key
        written = 0
        prefix = b""
        try:
            with temp.open("xb") as stream:
                async with asyncio.timeout(120):
                    async for chunk in req.content.iter_chunked(65536):
                        written += len(chunk)
                        if written > size:
                            return error(400, "文件长度不符")
                        if len(prefix) < 16:
                            prefix += chunk[:16 - len(prefix)]
                        stream.write(chunk)
                if written != size or not is_image(mime, prefix):
                    return error(400, "文件长度或图片格式不符")
                stream.flush()
                os.fsync(stream.fileno())
            # Recheck in case the limit was lowered during upload.
            usage = self.usage(uid)
            if usage["used"] + size > usage["quota"]:
                return error(413, "云图库容量已满，请删除旧图或联系管理员")
            temp.replace(final)
            try:
                with self.db:
                    self.db.execute("INSERT INTO files VALUES (?, ?, ?, ?, ?)", (key, uid, mime, size, int(time.time() * 1000)))
            except BaseException:
                final.unlink(missing_ok=True)
                raise
            return web.json_response({"id": key, **self.usage(uid)}, status=201)
        except TimeoutError:
            return error(408, "上传超时，请重试")
        except OSError:
            return error(507, "暂时无法写入服务器磁盘")
        finally:
            self.pending.pop(key, None)
            temp.unlink(missing_ok=True)

    def user_action(self, action, uid, data):
        if action == "list":
            rows = self.db.execute("SELECT id, mime, bytes, created FROM files WHERE owner = ? ORDER BY created DESC, id DESC LIMIT ? OFFSET ?", (uid, PAGE_SIZE + 1, offset(data))).fetchall()
            return web.json_response({**self.usage(uid), "items": [dict(r) for r in rows[:PAGE_SIZE]], "more": len(rows) > PAGE_SIZE})
        key = data.get("id")
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key):
            return error(400, "无效的图片编号")
        item = self.db.execute("SELECT mime FROM files WHERE id = ? AND owner = ?", (key, uid)).fetchone()
        if item is None:
            return error(404, "图片不存在")
        path = self.files / key
        if action == "read":
            mime = item["mime"]
            return web.FileResponse(path, headers={"Content-Type": mime, "Content-Disposition": f'attachment; filename="lumivia-{key[:10]}.{TYPES[mime]}"'})
        # Delete metadata first; any file left by an interrupted deletion is recovered on startup.
        with self.db:
            self.db.execute("DELETE FROM files WHERE id = ? AND owner = ?", (key, uid))
        path.unlink(missing_ok=True)
        return web.json_response(self.usage(uid))

    def admin_action(self, action, data):
        if action == "admin-list":
            search = str(data.get("search", ""))[:120].strip()
            rows = self.db.execute("""
                SELECT u.id, u.email, u.quota,
                    (SELECT COALESCE(SUM(bytes), 0) FROM files f WHERE f.owner = u.id) AS used
                FROM users u WHERE instr(lower(u.email), lower(?)) > 0 OR CAST(u.id AS TEXT) = ? OR ? = ''
                ORDER BY u.id LIMIT ? OFFSET ?
            """, (search, search, search, PAGE_SIZE + 1, offset(data))).fetchall()
            return web.json_response({
                "defaultQuota": self.db.execute("SELECT quota FROM settings").fetchone()[0],
                "diskFree": shutil.disk_usage(self.files).free,
                "reservedFree": self.reserve,
                "totalUsed": self.total_used(),
                "totalLimit": self.total_limit,
                "items": [dict(row) for row in rows[:PAGE_SIZE]], "more": len(rows) > PAGE_SIZE,
            })
        quota = data.get("quota")
        if "quota" not in data or (quota is not None and (type(quota) is not int or not 0 <= quota <= MAX_QUOTA)):
            return error(400, "存储额度必须在 0 至 1024 GiB 内")
        with self.db:
            if action == "admin-default":
                if quota is None:
                    return error(400, "默认额度不能为空")
                self.db.execute("UPDATE settings SET quota = ?", (quota,))
            else:
                uid = data.get("userId")
                if type(uid) is not int or not 0 < uid < 2 ** 63:
                    return error(400, "用户 ID 无效")
                if self.db.execute("UPDATE users SET quota = ? WHERE id = ?", (quota, uid)).rowcount != 1:
                    return error(404, "这个账号尚未进入乐米画布")
        return web.json_response({"ok": True})


def offset(data):
    page = data.get("page")
    return (page - 1) * PAGE_SIZE if type(page) is int and 0 < page <= 100000 else 0
