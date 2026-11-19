"""Durable task catalog with quota leases and a single bounded dispatcher."""

import asyncio
import json
import logging
import re
import shutil
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web

from canvas_policy import CanvasError, MIB
from cloud_storage import FILE_ID, PAGE_SIZE, TYPES, identify, now_ms, offset
from managed_keys import managed_secret
from media_gateway import MediaGateway, Pending, RetryableDownload, Review

LOG = logging.getLogger("canvas.jobs")
PENDING_SQL = "('staging','queued','submitting','waiting')"
SIZE = re.compile(r"(?:auto|[1-9][0-9]{2,3}x[1-9][0-9]{2,3})\Z")
MAX_REFERENCES = 8
MAX_REFERENCE_BYTES = 64 * MIB
MAX_DOWNLOAD_RETRIES = 3


async def bounded_json(content, limit=8192):
    data = bytearray()
    while chunk := await content.readany():
        data.extend(chunk)
        if len(data) > limit:
            raise CanvasError(413, "请求参数太多")
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError):
        raise CanvasError(400, "请求数据格式无效") from None
    if not isinstance(result, dict):
        raise CanvasError(400, "请求数据格式无效")
    return result


def safe_path(root, key, suffix):
    return root / (key + "." + suffix)


def runtime_memory():
    try:
        return int(next(line.split()[1] for line in Path("/proc/self/status").read_text().splitlines() if line.startswith("VmRSS:"))) * 1024
    except (OSError, StopIteration, ValueError):
        return None


class Jobs:
    def __init__(self, storage, base, session, public, image_group):
        self.storage, self.db = storage, storage.db
        self.gateway = MediaGateway(base, session, public)
        self.image_group = image_group
        self.active, self.wake = {}, asyncio.Event()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS canvas_jobs (
                id TEXT PRIMARY KEY, owner INTEGER NOT NULL, kind TEXT NOT NULL, model TEXT NOT NULL, group_id INTEGER NOT NULL,
                address TEXT NOT NULL, protocol TEXT NOT NULL, status TEXT NOT NULL, external TEXT, input TEXT NOT NULL,
                label TEXT NOT NULL, count INTEGER NOT NULL, cap INTEGER NOT NULL, result_limit INTEGER NOT NULL,
                backend INTEGER NOT NULL, days INTEGER NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL,
                next INTEGER NOT NULL, polls INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
                download_retries INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS canvas_jobs_owner ON canvas_jobs(owner,created DESC,id DESC);
            CREATE INDEX IF NOT EXISTS canvas_jobs_dispatch ON canvas_jobs(status,next,created);
            CREATE INDEX IF NOT EXISTS canvas_jobs_owner_status ON canvas_jobs(owner,status);
            UPDATE canvas_jobs SET status='review',input='',address='',message='服务重启前未确认是否受理，请先查主站用量' WHERE status='submitting';
            UPDATE canvas_jobs SET status='failed',input='',address='',message='服务重启时上传未完成，尚未发送给主站' WHERE status='staging';
            DELETE FROM canvas_reservations WHERE id IN(SELECT id FROM canvas_jobs WHERE status IN ('review','failed','cancelled','completed'));
        """)
        if "download_retries" not in {column[1] for column in self.db.execute("PRAGMA table_info(canvas_jobs)")}:
            with self.db:
                self.db.execute("ALTER TABLE canvas_jobs ADD COLUMN download_retries INTEGER NOT NULL DEFAULT 0")
        self.recover_stage()

    def recover_stage(self):
        queued = {r[0] for r in self.db.execute("SELECT id FROM canvas_jobs WHERE status='queued'")}
        for path in self.storage.stage.iterdir():
            key, _, suffix = path.name.partition(".")
            if FILE_ID.fullmatch(key) and (not suffix.startswith("ref") or key not in queued):
                path.unlink(missing_ok=True)

    def clear_references(self, key):
        for path in self.storage.stage.glob(key + ".ref*"):
            path.unlink(missing_ok=True)

    def summary(self, row):
        assets = self.db.execute("SELECT * FROM canvas_media WHERE job=? AND state='ready' AND (expires IS NULL OR expires>?) ORDER BY id", (row["id"], now_ms())).fetchall()
        return {"id": row["id"], "kind": row["kind"], "model": row["model"], "status": row["status"], "label": row["label"],
                "created": row["created"], "updated": row["updated"], "external": row["external"], "message": row["message"],
                "assets": [self.storage.file_summary(r) for r in assets]}

    def list(self, uid, data):
        rows = self.db.execute("SELECT * FROM canvas_jobs WHERE owner=? AND status!='staging' ORDER BY created DESC,id DESC LIMIT ? OFFSET ?",
                               (uid, PAGE_SIZE + 1, offset(data))).fetchall()
        return web.json_response({"items": [self.summary(r) for r in rows[:PAGE_SIZE]], "more": len(rows) > PAGE_SIZE})

    def get(self, uid, data):
        key = data.get("id")
        if not isinstance(key, str) or not FILE_ID.fullmatch(key):
            raise CanvasError(400, "任务编号无效")
        row = self.db.execute("SELECT * FROM canvas_jobs WHERE owner=? AND id=? AND status!='staging'", (uid, key)).fetchone()
        if not row:
            raise CanvasError(404, "任务不存在")
        return web.json_response(self.summary(row))

    def admin_summary(self, data):
        counts = {row[0]: row[1] for row in self.db.execute("SELECT status,COUNT(*) FROM canvas_jobs GROUP BY status")}
        rows = self.db.execute("""SELECT id,owner,kind,model,status,external,created,updated,message
              FROM canvas_jobs WHERE status!='staging' ORDER BY created DESC,id DESC LIMIT ? OFFSET ?""", (PAGE_SIZE + 1, offset(data))).fetchall()
        return web.json_response({"counts": counts, "activeWorkers": len(self.active), "memoryBytes": runtime_memory(),
                                  "memoryPaused": self.memory_pressure(), "items": [dict(r) for r in rows[:PAGE_SIZE]], "more": len(rows) > PAGE_SIZE})

    def cancel(self, uid, data):
        key = data.get("id")
        if not isinstance(key, str) or not FILE_ID.fullmatch(key):
            raise CanvasError(400, "任务编号无效")
        row = self.db.execute("SELECT status FROM canvas_jobs WHERE owner=? AND id=?", (uid, key)).fetchone()
        if not row:
            raise CanvasError(404, "任务不存在")
        if row[0] != "queued":
            raise CanvasError(409, "只有尚未提交主站的任务可以取消")
        self.finish(key, "cancelled", "排队任务已取消，尚未发送给主站")
        return web.json_response({"ok": True})

    def route(self, kind, group, model):
        policy = self.storage.policy
        if kind == "video":
            video = policy.video_routes().get(model)
            return video[1] if video and video[0] == group else None
        configured = policy.value["imageGroup"] or self.image_group
        return policy.value["imageMode"] if configured == group and policy.permits(kind, model) else None

    def memory_pressure(self):
        budget = self.storage.policy.value["memoryGuardMiB"] * MIB
        return bool(budget and (runtime_memory() or 0) >= budget)

    async def create(self, req, kind, subject, model):
        uid, group, address = subject
        policy = self.storage.policy
        protocol = self.route(kind, group, model)
        if protocol is None:
            raise CanvasError(403, "管理员已调整此模型的可用范围，请重新同步")
        reader = None
        if req.content_type == "multipart/form-data":
            reader = await req.multipart()
            part = await reader.next()
            if not part or part.name != "spec" or part.filename or part.headers.get("Content-Transfer-Encoding") or part.headers.get("Content-Encoding"):
                raise CanvasError(400, "缺少生成参数")
            text = bytearray()
            while chunk := await part.read_chunk(8192):
                text.extend(chunk)
                if len(text) > 32768:
                    raise CanvasError(413, "生成参数过长")
            try:
                spec = json.loads(text)
            except (ValueError, UnicodeError):
                raise CanvasError(400, "生成参数格式错误") from None
        elif req.content_type == "application/json":
            spec = await bounded_json(req.content, 32768)
        else:
            raise CanvasError(415, "不支持的生成请求格式")
        settings = policy.value
        if not isinstance(spec, dict) or spec.get("model") != model:
            raise CanvasError(400, "生成模型与授权不一致")
        key, prompt = spec.get("id"), spec.get("prompt")
        if not isinstance(key, str) or not FILE_ID.fullmatch(key) or not isinstance(prompt, str) or not 0 < len(prompt.strip()) <= 4000:
            raise CanvasError(400, "请填写 1 至 4000 字的提示词")
        exists = self.db.execute("SELECT * FROM canvas_jobs WHERE id=? AND owner=?", (key, uid)).fetchone()
        if exists:
            if exists["kind"] != kind or exists["model"] != model or exists["status"] == "staging":
                raise CanvasError(409, "此任务编号正在提交或已用于另一请求")
            return web.json_response(self.summary(exists), status=202)
        if self.db.execute("SELECT 1 FROM canvas_jobs WHERE id=?", (key,)).fetchone():
            raise CanvasError(409, "任务编号已被使用，请重新打开生成页面")
        payload = {"prompt": prompt.strip(), "model": model}
        count, cap = 1, settings["imageMiB" if kind == "image" else "videoMiB"] * MIB
        if kind == "image":
            size, quality, count = spec.get("size", "auto"), spec.get("quality", "auto"), spec.get("count", 1)
            if not isinstance(size, str) or not SIZE.fullmatch(size) or quality not in ("auto", "low", "medium", "high") or type(count) is not int or not 1 <= count <= 4:
                raise CanvasError(400, "图片尺寸、画质或张数无效（每任务 1 至 4 张）")
            payload.update(size=size, quality=quality, n=count)
            if "background" in spec:
                if spec["background"] not in ("auto", "opaque", "transparent"):
                    raise CanvasError(400, "图片背景选项无效")
                payload["background"] = spec["background"]
        else:
            seconds, ratio, resolution = spec.get("seconds"), spec.get("ratio"), spec.get("resolution")
            options = policy.video_options(model, protocol)
            if (type(seconds) is not int or not 4 <= seconds <= settings["videoSeconds"]
                    or (options["durations"] and seconds not in options["durations"])
                    or ratio not in ("16:9", "9:16", "1:1") or resolution not in options["resolutions"]):
                raise CanvasError(400, "视频时长、比例或分辨率不在后台允许范围内")
            payload.update(seconds=seconds, ratio=ratio, resolution=resolution)
        if self.memory_pressure():
            raise CanvasError(503, "服务器内存压力较高，暂缓接收新任务；本次未提交主站")
        if self.db.execute(f"SELECT count(*) FROM canvas_jobs WHERE status IN {PENDING_SQL}").fetchone()[0] >= settings["queueSize"]:
            raise CanvasError(429, "生成队列已满，请稍后再试；本次未提交主站")
        if self.db.execute(f"SELECT count(*) FROM canvas_jobs WHERE owner=? AND status IN {PENDING_SQL}", (uid,)).fetchone()[0] >= settings["perUser"]:
            raise CanvasError(429, "账号的在途任务已达上限，请等待现有任务完成")
        reserve = cap * count
        backend = self.storage.target()
        self.storage.check_capacity(uid, reserve)
        created = now_ms()
        with self.db:
            self.db.execute("INSERT INTO canvas_reservations VALUES(?,?,?)", (key, uid, reserve))
            self.db.execute("""INSERT INTO canvas_jobs(id,owner,kind,model,group_id,address,protocol,status,input,label,count,cap,
                         result_limit,backend,days,created,updated,next) VALUES(?,?,?,?,?,?,?,'staging',?,?,?,?,?,?,?,?,?,?)""",
                         (key, uid, kind, model, group, address, protocol, "", prompt.strip()[:120], count, cap,
                          reserve * 4 // 3 + 128 * 1024 if kind == "image" else 256 * 1024, backend, settings["retentionDays"], created, created, created))
        try:
            if reader:
                references = []
                ref_total = 0
                max_refs = 1 if kind == "video" else MAX_REFERENCES
                while part := await reader.next():
                    if len(references) >= max_refs or part.name != "reference" or not part.filename or part.headers.get("Content-Transfer-Encoding") or part.headers.get("Content-Encoding"):
                        raise CanvasError(400, f"请上传最多 {max_refs} 张参考图")
                    ref_cap = min(settings["imageMiB"] * MIB, 8 * MIB) if kind == "video" else settings["imageMiB"] * MIB
                    path = safe_path(self.storage.stage, key, f"ref{len(references)}")
                    ref_size = 0
                    with path.open("xb") as stream:
                        while chunk := await part.read_chunk(65536):
                            ref_size += len(chunk)
                            ref_total += len(chunk)
                            if ref_size > ref_cap or ref_total > MAX_REFERENCE_BYTES:
                                raise CanvasError(413, "参考图超过大小上限")
                            if shutil.disk_usage(self.storage.root).free - self.storage.held() - len(chunk) < self.storage.reserve:
                                raise CanvasError(507, "磁盘保护已触发，无法暂存参考图")
                            stream.write(chunk)
                    with path.open("rb") as stream:
                        mime = identify(stream.read(32))
                    if mime not in ("image/png", "image/jpeg", "image/webp"):
                        raise CanvasError(400, "只接受 PNG、JPG 或 WebP 参考图")
                    references.append({"mime": mime, "ext": TYPES[mime]})
                if references:
                    payload["references"] = references
            with self.db:
                self.db.execute("UPDATE canvas_jobs SET status='queued',input=?,updated=? WHERE id=?", (json.dumps(payload), now_ms(), key))
        except BaseException:
            self.finish(key, "failed", "参考图或任务参数未接收完整，未发送给主站")
            raise
        self.wake.set()
        return web.json_response(self.summary(self.db.execute("SELECT * FROM canvas_jobs WHERE id=?", (key,)).fetchone()), status=202)

    def finish(self, key, state, message=""):
        with self.db:
            self.db.execute("UPDATE canvas_jobs SET status=?,updated=?,message=?,input='',address='' WHERE id=?", (state, now_ms(), message, key))
            self.db.execute("DELETE FROM canvas_reservations WHERE id=?", (key,))
        self.clear_references(key)
        self.wake.set()

    async def run(self):
        try:
            while True:
                self.wake.clear()
                for key, task in list(self.active.items()):
                    if task.done():
                        self.active.pop(key)
                        await asyncio.gather(task, return_exceptions=True)
                current = now_ms()
                slots = self.storage.policy.value["workers"] - len(self.active)
                if slots > 0:
                    rows = self.db.execute("SELECT * FROM canvas_jobs WHERE status IN ('waiting','queued') AND next<=? ORDER BY next,created LIMIT 304", (current,)).fetchall()
                    memory_paused = self.memory_pressure()
                    for row in rows:
                        key = row["id"]
                        if key in self.active or (memory_paused and row["status"] == "queued"):
                            continue
                        if row["status"] == "queued":
                            with self.db:
                                self.db.execute("UPDATE canvas_jobs SET status='submitting',updated=? WHERE id=?", (current, key))
                        task = asyncio.create_task(self.work(row))
                        task.add_done_callback(lambda _: self.wake.set())
                        self.active[key] = task
                        slots -= 1
                        if slots == 0:
                            break
                try:
                    await asyncio.wait_for(self.wake.wait(), 1)
                except TimeoutError:
                    pass
        finally:
            for task in self.active.values():
                task.cancel()
            await asyncio.gather(*self.active.values(), return_exceptions=True)

    def postpone(self, row, external, *, retry_download=False):
        attempts = row["polls"] + 1
        message = "结果下载暂时中断，正在重新取回原任务" if retry_download else ""
        with self.db:
            self.db.execute("""UPDATE canvas_jobs SET status='waiting',external=?,input='',updated=?,next=?,polls=?,
                            download_retries=download_retries+?,message=? WHERE id=?""",
                            (external, now_ms(), now_ms() + min(24000, 3000 * attempts), attempts,
                             int(retry_download), message, row["id"]))
        self.clear_references(row["id"])

    async def work(self, row):
        key = row["id"]
        stage = self.storage.stage
        result, media = [safe_path(stage, key, part) for part in ("result", "media")]
        saving = False
        try:
            if row["status"] == "queued":
                if self.route(row["kind"], row["group_id"], row["model"]) is None:
                    raise CanvasError(403, "管理员已关闭该模型或变更视频分组；未发送给主站")
            if now_ms() - row["created"] > (2 if row["kind"] == "video" else 1) * 3600000:
                if row["external"]:
                    raise Review(504, "任务长时间未返回，请先用编号检查主站用量")
                raise CanvasError(408, "排队超时，未提交主站")
            # Stream spools need breathing room in addition to the leased final asset bytes.
            if shutil.disk_usage(stage).free < self.storage.reserve + row["result_limit"] + row["cap"]:
                if row["external"]:
                    raise Pending()
                raise CanvasError(507, "服务器磁盘不足，未提交主站")
            secret = managed_secret(row["owner"], row["group_id"], row["kind"])
            if row["external"]:
                await self.gateway.poll(row, secret, result)
            else:
                external = await self.gateway.submit(row, secret, stage, result)
                if external:
                    self.postpone(row, external)
                    return
            saving = True
            allowed = self.storage.policy.value["resultHosts"]

            def asset_id(index):
                return uuid.uuid5(uuid.UUID(hex=key), str(index)).hex

            def saved(index):
                return self.db.execute("SELECT 1 FROM canvas_media WHERE id=? AND state='ready'", (asset_id(index),)).fetchone() is not None

            async def save(index):
                asset = asset_id(index)
                row_before = self.db.execute("SELECT *,0 AS legacy FROM canvas_media WHERE id=?", (asset,)).fetchone()
                if row_before and row_before["state"] != "ready" and not await self.storage.remove(row_before):
                    raise Pending()
                if not saved(index):
                    await self.storage.store(media, asset, row["owner"], row["cap"], row["backend"], row["days"], kind=row["kind"], job=key)
                media.unlink(missing_ok=True)

            if row["kind"] == "image":
                count = await self.gateway.images(row, result, save, saved, media, allowed)
            else:
                count = 1
                if not saved(1):
                    await self.gateway.video(row, secret, result, media, allowed)
                    await save(1)
            self.finish(key, "completed", "" if count == row["count"] else f"上游返回 {count} 个结果，少于请求数量")
        except Pending:
            self.postpone(row, row["external"])
        except Review as exc:
            self.finish(key, "review", exc.message)
        except (RetryableDownload, aiohttp.ClientError, TimeoutError):
            if row["external"] and not saving:
                self.postpone(row, row["external"])
            elif row["external"] and row["download_retries"] < MAX_DOWNLOAD_RETRIES:
                self.postpone(row, row["external"], retry_download=True)
            elif saving:
                self.finish(key, "review", "暂时无法取回结果，请用任务编号核对主站用量")
            else:
                self.finish(key, "review", "上游网络中断，受理情况不明，请先查主站用量")
        except CanvasError as exc:
            self.finish(key, "review" if saving else "failed", exc.message + ("；请先查主站用量，避免重复提交" if saving else ""))
        except Exception as exc:
            LOG.warning("task processing failed id=%s type=%s", key, type(exc).__name__)
            self.finish(key, "review", "处理结果时出现异常，请管理员核对主站用量")
        finally:
            for path in (result, media):
                path.unlink(missing_ok=True)
