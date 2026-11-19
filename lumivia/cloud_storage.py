"""Private canvas library, capacity accounting and expiring file access."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

from aiohttp import web

from canvas_policy import CanvasError, CanvasPolicy, GIB, MIB
from cos_storage import Profiles

MAX_FILE = 32 * MIB
MAX_QUOTA = 1024 * GIB
PAGE_SIZE = 20
TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif", "image/avif": "avif",
         "video/mp4": "mp4", "video/webm": "webm"}
FILE_ID = re.compile(r"[0-9a-f]{32}\Z")
TOKEN = re.compile(r"([0-9a-f]{32})-([0-9]{1,12})-(download|preview|thumb)-([0-9a-f]{64})\Z")
LOG = logging.getLogger("canvas.storage")


def error(status, message):
    return web.json_response({"error": {"message": message}}, status=status)


def now_ms():
    return int(time.time() * 1000)


def identify(head):
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    if head[4:8] == b"ftyp":
        return "video/mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return ""


def thumbnail(source, target):
    from PIL import Image
    try:
        with Image.open(source) as picture:
            if picture.width * picture.height > 24_000_000:
                return False
            picture.thumbnail((384, 384))
            picture.convert("RGB").save(target, "JPEG", quality=75)
        return target.stat().st_size <= 128 * 1024
    except (OSError, Image.DecompressionBombError, ValueError):
        return False


def offset(data):
    page = data.get("page")
    return (page - 1) * PAGE_SIZE if type(page) is int and 0 < page <= 100000 else 0


class CloudStorage:
    def __init__(self, directory, reserve=4 * GIB):
        self.root = Path(directory)
        self.files, self.media, self.previews, self.stage = [self.root / part for part in ("images", "media", "previews", "stage")]
        for path in (self.files, self.media, self.previews, self.stage):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.pending = {}
        self.signing_key = secrets.token_bytes(32)
        self.thumb_slot = asyncio.Lock()
        self.sweeping = set()
        self.db = sqlite3.connect(self.root / "storage.sqlite3")
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
            CREATE TABLE IF NOT EXISTS canvas_media (
                id TEXT PRIMARY KEY, owner INTEGER NOT NULL, mime TEXT NOT NULL, bytes INTEGER NOT NULL, created INTEGER NOT NULL,
                expires INTEGER, backend INTEGER NOT NULL, thumb INTEGER NOT NULL, job TEXT, state TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS canvas_media_owner_created ON canvas_media(owner, created DESC, id DESC);
            CREATE INDEX IF NOT EXISTS canvas_media_expiry ON canvas_media(expires) WHERE expires IS NOT NULL;
            CREATE INDEX IF NOT EXISTS canvas_media_job ON canvas_media(job);
            CREATE INDEX IF NOT EXISTS canvas_media_cleanup ON canvas_media(state, created);
            CREATE TABLE IF NOT EXISTS canvas_reservations (id TEXT PRIMARY KEY, owner INTEGER NOT NULL, bytes INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS canvas_reservations_owner ON canvas_reservations(owner);
            CREATE TABLE IF NOT EXISTS canvas_storage_target (id INTEGER PRIMARY KEY CHECK(id=1), profile INTEGER NOT NULL);
            INSERT OR IGNORE INTO canvas_storage_target VALUES (1, 0);
            UPDATE canvas_reservations SET bytes=bytes+COALESCE((SELECT SUM(bytes) FROM canvas_media WHERE job=canvas_reservations.id AND state='writing'),0);
            UPDATE canvas_media SET state='deleting' WHERE state='writing';
            CREATE TEMP VIEW library AS
                SELECT id,owner,mime,bytes,created,expires,backend,thumb,job,state,0 AS legacy FROM canvas_media
                UNION ALL SELECT id,owner,mime,bytes,created,NULL,0,0,NULL,'ready',1 FROM files;
        """)
        self.policy = CanvasPolicy(self.db, reserve)
        self.profiles = Profiles(self.db)
        self.db.commit()

    @property
    def total_limit(self):
        return self.policy.value["totalLimitMiB"] * MIB

    @property
    def reserve(self):
        return self.policy.value["diskReserveMiB"] * MIB

    @reserve.setter
    def reserve(self, value):
        self.policy.value["diskReserveMiB"] = value // MIB

    def target(self):
        profile = self.db.execute("SELECT profile FROM canvas_storage_target WHERE id=1").fetchone()[0]
        return profile if self.policy.value["backend"] == "cos" else 0

    def remember(self, uid, email):
        with self.db:
            self.db.execute("INSERT INTO users(id,email) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET email=excluded.email WHERE users.email!=excluded.email", (uid, email))

    def usage(self, uid):
        quota, default = self.db.execute("SELECT users.quota, settings.quota FROM users CROSS JOIN settings WHERE users.id=?", (uid,)).fetchone()
        used = self.db.execute("SELECT COALESCE(SUM(bytes),0) FROM library WHERE owner=?", (uid,)).fetchone()[0]
        return {"used": used, "quota": default if quota is None else quota, "defaultQuota": default, "reserved": self.held(uid)}

    def total_used(self):
        return self.db.execute("SELECT COALESCE(SUM(bytes),0) FROM library").fetchone()[0]

    def held(self, uid=None):
        clause, params = ("", ()) if uid is None else (" WHERE owner=?", (uid,))
        saved = self.db.execute("SELECT COALESCE(SUM(bytes),0) FROM canvas_reservations" + clause, params).fetchone()[0]
        return saved + sum(n for owner, n in self.pending.values() if uid is None or owner == uid)

    def check_capacity(self, uid, size):
        usage = self.usage(uid)
        held = self.held()
        if usage["used"] + usage["reserved"] + size > usage["quota"]:
            raise CanvasError(413, "可用存储不足，请删除素材或联系管理员调整额度")
        if self.total_used() + held + size > self.total_limit:
            raise CanvasError(507, "画布全站存储容量不足，请联系管理员")
        if shutil.disk_usage(self.root).free - held - size < self.reserve:
            raise CanvasError(507, "服务器磁盘保护已触发，暂时不能新增素材")

    async def upload(self, req, uid):
        mime, size = req.content_type, req.headers.get("X-Canvas-Bytes", "")
        if mime not in TYPES or req.headers.get("Content-Encoding", "identity") != "identity":
            raise CanvasError(415, "请上传 PNG、JPG、WebP、GIF、AVIF、MP4 或 WebM 文件")
        cap = self.policy.value["imageMiB" if mime.startswith("image/") else "videoMiB"]
        if not re.fullmatch(r"[1-9][0-9]{0,8}", size) or int(size) > cap * MIB:
            raise CanvasError(413, f"该类文件最多 {cap} MiB")
        size = int(size)
        if req.content_length is not None and req.content_length != size:
            raise CanvasError(400, "文件长度不符")
        if len(self.pending) >= 12 or sum(owner == uid for owner, _ in self.pending.values()) >= 3:
            raise CanvasError(429, "正在保存的文件较多，请稍后再试")
        self.check_capacity(uid, size)
        key, backend, days = uuid.uuid4().hex, self.target(), self.policy.value["retentionDays"]
        self.pending[key] = (uid, size)
        path = self.stage / (key + ".upload")
        try:
            written = 0
            with path.open("xb") as stream:
                async with asyncio.timeout(120):
                    async for chunk in req.content.iter_chunked(65536):
                        written += len(chunk)
                        if written > size:
                            raise CanvasError(400, "文件长度不符")
                        stream.write(chunk)
            usage = self.usage(uid)
            if written != size:
                raise CanvasError(400, "文件长度不符")
            if usage["used"] + usage["reserved"] > usage["quota"]:
                raise CanvasError(413, "上传期间存储额度已降低，请联系管理员")
            await self.store(path, key, uid, size, backend, days, mime=mime)
            return web.json_response({"id": key, **self.usage(uid)}, status=201)
        except TimeoutError:
            raise CanvasError(408, "上传超时，请重试") from None
        except OSError:
            raise CanvasError(507, "暂时无法写入磁盘") from None
        finally:
            self.pending.pop(key, None)
            path.unlink(missing_ok=True)

    async def store(self, path, key, uid, cap, backend, days, *, mime=None, kind=None, job=None):
        size = path.stat().st_size
        with path.open("rb") as stream:
            detected = identify(stream.read(32))
        if not detected or (mime and mime != detected) or (kind and not detected.startswith(kind + "/")):
            raise CanvasError(400, "文件内容与图片或视频格式不符")
        if not 0 < size <= cap:
            raise CanvasError(413, "文件超过管理员设置的大小上限")
        created = now_ms()
        with self.db:
            if job:
                updated = self.db.execute("UPDATE canvas_reservations SET bytes=bytes-? WHERE id=? AND bytes>=?", (size, job, size))
                if updated.rowcount != 1:
                    raise CanvasError(507, "本次任务预留的保存空间不足")
            self.db.execute("INSERT INTO canvas_media VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (key, uid, detected, size, created, created + days * 86400000 if days else None, backend, 0, job, "writing"))
        self.pending.pop(key, None)
        thumb = self.previews / key
        try:
            # Reserve the original before any cloud/network work. Keep at most one full image decode per process.
            have_thumb = False
            if detected.startswith("image/"):
                async with self.thumb_slot:
                    have_thumb = await asyncio.to_thread(thumbnail, path, thumb)
                if not have_thumb:
                    thumb.unlink(missing_ok=True)
            if backend:
                client = self.profiles.load(backend)
                await asyncio.to_thread(client.put, key, path, detected)
            else:
                path.replace(self.media / key)
            with self.db:
                self.db.execute("UPDATE canvas_media SET state='ready',thumb=? WHERE id=?", (int(have_thumb), key))
            return key
        except Exception as exc:
            with self.db:
                self.db.execute("UPDATE canvas_media SET state='deleting' WHERE id=?", (key,))
            LOG.warning("asset save failed backend=%d kind=%s", backend, type(exc).__name__)
            raise CanvasError(502 if backend else 507, "文件保存失败，请检查存储配置或磁盘空间") from None

    def ticket(self, key, mode):
        expires = (int(time.time()) // 60) * 60 + 300
        prefix = f"{key}-{expires}-{mode}"
        signature = hmac.digest(self.signing_key, prefix.encode(), hashlib.sha256).hex()
        return "/lumivia/file/" + prefix + "-" + signature

    def file_summary(self, row):
        return {"id": row["id"], "mime": row["mime"], "bytes": row["bytes"], "created": row["created"], "expires": row["expires"],
                "location": "cos" if row["backend"] else "local", "thumbUrl": self.ticket(row["id"], "thumb") if row["thumb"] else None}

    def get_file(self, key, uid=None):
        if not isinstance(key, str) or not FILE_ID.fullmatch(key):
            raise CanvasError(400, "无效的素材编号")
        params = (key, now_ms()) if uid is None else (key, now_ms(), uid)
        row = self.db.execute("SELECT * FROM library WHERE id=? AND state='ready' AND (expires IS NULL OR expires>?)" +
                              ("" if uid is None else " AND owner=?"), params).fetchone()
        if not row:
            raise CanvasError(404, "素材不存在或已过期")
        return row

    def local_response(self, row, mode):
        if mode == "thumb" and not row["thumb"]:
            raise CanvasError(404, "没有可用的缩略图")
        key = row["id"]
        path = (self.previews if mode == "thumb" else self.files if row["legacy"] else self.media) / key
        if not path.is_file():
            raise CanvasError(404, "文件不可用")
        mime = "image/jpeg" if mode == "thumb" else row["mime"]
        return web.FileResponse(path, headers={"Content-Type": mime,
            "Content-Disposition": f'{"attachment" if mode == "download" else "inline"}; filename="lumivia-{key[:10]}.{TYPES[mime]}"'})

    def read_link(self, row, mode):
        if row["backend"] and mode != "thumb":
            try:
                cos = self.profiles.load(row["backend"])
                ttl = 300 if row["expires"] is None else min(300, max(1, (row["expires"] - now_ms()) // 1000))
                return cos.link(row["id"], f'lumivia-{row["id"][:10]}.{TYPES[row["mime"]]}', mode == "preview", ttl)
            except CanvasError:
                raise
            except Exception:
                raise CanvasError(502, "暂时无法签发对象存储链接，请联系管理员") from None
        return self.ticket(row["id"], mode)

    def from_ticket(self, token):
        match = TOKEN.fullmatch(token)
        if not match:
            raise CanvasError(404, "无效的素材链接")
        key, expires, mode, signature = match.groups()
        expected = hmac.digest(self.signing_key, token.rsplit("-", 1)[0].encode(), hashlib.sha256).hex()
        if int(expires) <= int(time.time()) or not secrets.compare_digest(signature, expected):
            raise CanvasError(404, "素材链接已失效，请从图库重新打开")
        row = self.get_file(key)
        if row["backend"] and mode != "thumb":
            raise CanvasError(404, "请从图库重新打开素材")
        return self.local_response(row, mode)

    async def remove(self, row):
        key = row["id"]
        if key in self.sweeping:
            return False
        self.sweeping.add(key)
        try:
            if row["legacy"]:
                (self.files / key).unlink(missing_ok=True)
                with self.db:
                    self.db.execute("DELETE FROM files WHERE id=?", (key,))
            else:
                with self.db:
                    self.db.execute("UPDATE canvas_media SET state='deleting' WHERE id=?", (key,))
                if row["backend"]:
                    cos = self.profiles.load(row["backend"])
                    await asyncio.to_thread(cos.delete, key)
                else:
                    (self.media / key).unlink(missing_ok=True)
                (self.previews / key).unlink(missing_ok=True)
                with self.db:
                    self.db.execute("DELETE FROM canvas_media WHERE id=?", (key,))
            return True
        except Exception as exc:
            LOG.warning("asset cleanup deferred kind=%s", type(exc).__name__)
            return False
        finally:
            self.sweeping.remove(key)

    async def sweep(self):
        rows = self.db.execute("""SELECT *,0 AS legacy FROM canvas_media
            WHERE state='deleting' OR (state='ready' AND expires<=?) ORDER BY expires,created LIMIT 24""", (now_ms(),)).fetchall()
        for row in rows:
            await self.remove(row)

    async def user_action(self, action, uid, data):
        if action == "list":
            rows = self.db.execute("""SELECT * FROM library WHERE owner=? AND state='ready' AND (expires IS NULL OR expires>?)
                ORDER BY created DESC,id DESC LIMIT ? OFFSET ?""", (uid, now_ms(), PAGE_SIZE + 1, offset(data))).fetchall()
            return web.json_response({**self.usage(uid), "items": [self.file_summary(r) for r in rows[:PAGE_SIZE]],
                                      "more": len(rows) > PAGE_SIZE, "retentionDays": self.policy.value["retentionDays"],
                                      "imageMiB": self.policy.value["imageMiB"], "videoMiB": self.policy.value["videoMiB"]})
        row = self.get_file(data.get("id"), uid)
        if action == "access":
            mode = "preview" if data.get("preview") is True else "download"
            if mode == "preview" and row["backend"] and not self.profiles.public(row["backend"])["domain"] and row["thumb"]:
                return web.json_response({"url": self.ticket(row["id"], "thumb"), "previewQuality": "thumbnail"})
            return web.json_response({"url": self.read_link(row, mode)})
        if action == "read":
            if row["backend"]:
                return web.Response(status=303, headers={"Location": self.read_link(row, "download")})
            return self.local_response(row, "download")
        if not await self.remove(row):
            raise CanvasError(503, "文件暂时未能删除，将继续清理")
        return web.json_response(self.usage(uid))

    async def admin_action(self, action, uid, data):
        if action == "admin-settings":
            profile = self.db.execute("SELECT profile FROM canvas_storage_target WHERE id=1").fetchone()[0]
            return web.json_response({"policy": self.policy.value, "revision": self.policy.revision, "cos": self.profiles.public(profile),
                "totalUsed": self.total_used(), "reserved": self.held(), "diskFree": shutil.disk_usage(self.root).free,
                "cleanupPending": self.db.execute("SELECT COUNT(*) FROM canvas_media WHERE state='deleting' OR expires<=?", (now_ms(),)).fetchone()[0]})
        if action == "admin-save":
            changes = self.policy.validate(data.get("revision"), data.get("changes"))
            profile = self.db.execute("SELECT profile FROM canvas_storage_target WHERE id=1").fetchone()[0]
            candidate = self.profiles.candidate(data["cos"]) if "cos" in data else None
            if changes["backend"] == "cos" and not (profile or candidate):
                raise CanvasError(400, "请配置私有 COS 存储桶并通过连接检查")
            if candidate:
                try:
                    await asyncio.to_thread(candidate.verify)
                except Exception as exc:
                    LOG.warning("COS verification failed kind=%s", type(exc).__name__)
                    raise CanvasError(502, "COS 测试失败。请检查地域、桶名和专用密钥的上传、签名下载与删除权限") from None
            # Recheck after network I/O to avoid overwriting settings saved by another admin.
            self.policy.validate(data.get("revision"), {})
            with self.db:
                if candidate:
                    profile = self.profiles.save(candidate)
                    self.db.execute("UPDATE canvas_storage_target SET profile=? WHERE id=1", (profile,))
                revision = self.policy.revision + 1
                self.db.execute("UPDATE canvas_policy SET revision=?,value=? WHERE id=1", (revision, json.dumps(changes)))
                self.db.execute("INSERT INTO canvas_policy_log VALUES(?,?,?)", (revision, uid, now_ms()))
            self.policy.reload()
            return web.json_response({"revision": revision, "ok": True})
        if action == "admin-list":
            search = str(data.get("search", ""))[:120].strip()
            rows = self.db.execute("""SELECT u.id,u.email,u.quota,
                (SELECT COALESCE(SUM(bytes),0) FROM library f WHERE f.owner=u.id) AS used
                FROM users u WHERE instr(lower(u.email),lower(?))>0 OR CAST(u.id AS TEXT)=? OR ?=''
                ORDER BY u.id LIMIT ? OFFSET ?""", (search, search, search, PAGE_SIZE + 1, offset(data))).fetchall()
            return web.json_response({"defaultQuota": self.db.execute("SELECT quota FROM settings").fetchone()[0],
                "diskFree": shutil.disk_usage(self.root).free, "reservedFree": self.reserve,
                "totalUsed": self.total_used(), "totalLimit": self.total_limit,
                "items": [dict(row) for row in rows[:PAGE_SIZE]], "more": len(rows) > PAGE_SIZE})
        quota = data.get("quota")
        if "quota" not in data or (quota is not None and (type(quota) is not int or not 0 <= quota <= MAX_QUOTA)):
            raise CanvasError(400, "存储额度必须在 0 至 1024 GiB 内")
        with self.db:
            if action == "admin-default":
                if quota is None:
                    raise CanvasError(400, "默认额度不能为空")
                self.db.execute("UPDATE settings SET quota=?", (quota,))
            else:
                target = data.get("userId")
                if type(target) is not int or not 0 < target < 2 ** 63:
                    raise CanvasError(400, "用户 ID 无效")
                if self.db.execute("UPDATE users SET quota=? WHERE id=?", (quota, target)).rowcount != 1:
                    raise CanvasError(404, "这个账号尚未进入乐米画布")
        return web.json_response({"ok": True})
