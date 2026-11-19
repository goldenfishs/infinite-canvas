"""Sub2 async image / Seedance / compatible video protocols and safe result downloads."""

import asyncio
import base64
import binascii
import json
import re
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit

import aiohttp
import ijson

from canvas_policy import CanvasError, MIB

REMOTE_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,191}\Z")
SEEDANCE = "/api/v3/contents/generations/tasks"
ID_FIELDS = ("id", "task_id", "data.id", "data.task_id")
VIDEO_URL = ("content.video_url", "video.url", "metadata.url", "video_url", "result_url", "url", "data.content.video_url", "data.video.url", "data.video_url", "data.result_url")
SUCCESS = {"completed", "succeeded", "success", "done"}
FAILED = {"failed", "error", "cancelled", "canceled", "expired", "failure"}


class Pending(Exception):
    """Later polling may succeed. No POST is retried."""


class Review(CanvasError):
    """Sub2 may already have accepted or billed the request."""


class RetryableDownload(CanvasError):
    """A later download can succeed without starting a new generation."""


def retryable_download_status(status):
    return status in (403, 404, 408, 410, 429) or 500 <= status < 600


def safe_ip(value):
    address = ip_address(value)
    if address.version == 6 and address.ipv4_mapped:
        return safe_ip(str(address.ipv4_mapped))
    if address.version == 6 and (address.sixtofour or address.teredo):
        return False
    return address.is_global and not (address.is_multicast or address.is_reserved or address.is_loopback or address.is_link_local)


class PublicResolver(aiohttp.ThreadedResolver):
    async def resolve(self, *args, **kwargs):
        result = await super().resolve(*args, **kwargs)
        if not result or any(not safe_ip(record["host"]) for record in result):
            raise OSError("Result hostname is not public")
        return result


def safe_url(url, allowed):
    if not isinstance(url, str) or len(url) > 8192 or any(ch in url for ch in ("\r", "\n", "\\", "\x00")):
        raise CanvasError(502, "上游返回了无效的素材地址")
    split = urlsplit(url)
    host = split.hostname or ""
    if split.scheme != "https" or split.username is not None or split.password is not None or split.port not in (None, 443) or not host or (allowed and host not in allowed):
        raise CanvasError(502, "素材域名不在允许范围，请联系管理员")
    try:
        if not safe_ip(host):
            raise CanvasError(502, "素材必须来自公网 HTTPS 地址")
    except ValueError:
        if "." not in host or host.endswith(".local"):
            raise CanvasError(502, "素材必须来自公网 HTTPS 地址") from None
    return url


async def stream_file(response, path, limit):
    if response.content_length and response.content_length > limit:
        raise CanvasError(413, "生成结果超过设置的大小上限")
    size = 0
    with path.open("wb") as output:
        async for chunk in response.content.iter_chunked(65536):
            size += len(chunk)
            if size > limit:
                raise CanvasError(413, "生成结果超过设置的大小上限")
            output.write(chunk)
    if not size:
        raise CanvasError(502, "上游未返回文件内容")


async def download_media(session, url, allowed, path, limit):
    for _ in range(4):
        async with session.get(safe_url(url, allowed), allow_redirects=False) as response:
            if response.status in (301, 302, 303, 307, 308) and response.headers.get("Location"):
                url = urljoin(url, response.headers["Location"])
                continue
            if retryable_download_status(response.status):
                raise RetryableDownload(502, "上游文件暂时无法读取")
            if response.status != 200 or response.headers.get("Content-Encoding", "identity") != "identity":
                raise CanvasError(502, "暂时无法从上游取回结果，请联系管理员")
            return await stream_file(response, path, limit)
    raise CanvasError(502, "结果下载跳转次数过多")


def field(path, *keys):
    for key in keys:
        with path.open("rb") as stream:
            value = next(ijson.items(stream, key), None)
        if isinstance(value, str) and value:
            return value
    return ""


def response_error(path):
    try:
        return field(path, "error.message", "error.error.message", "message", "msg")
    except (ValueError, ijson.JSONError):
        return ""


def remote_id(path):
    value = field(path, *ID_FIELDS)
    return value if REMOTE_ID.fullmatch(value) else ""


def build_arguments(row, spec, stage):
    references = [(stage / f'{row["id"]}.ref{index}', ref) for index, ref in enumerate(spec.get("references", []))]
    handles = []
    if row["kind"] == "image":
        payload = {k: spec[k] for k in ("model", "prompt", "size", "quality", "n", "background") if k in spec}
        payload["output_format"] = "png"
        if not re.search(r"gpt-image", spec["model"]):
            payload["response_format"] = "b64_json"
        if not references:
            return "/v1/images/generations", {"json": payload}, handles
        form = aiohttp.FormData()
        for key, value in payload.items():
            form.add_field(key, str(value))
        field_name = "image[]" if len(references) > 1 else "image"
        try:
            for index, (reference, ref) in enumerate(references):
                handle = reference.open("rb")
                handles.append(handle)
                form.add_field(field_name, handle, filename=f'reference-{index + 1}.{ref["ext"]}', content_type=ref["mime"])
        except Exception:
            for handle in handles:
                handle.close()
            raise
        return "/v1/images/edits", {"data": form}, handles
    if row["protocol"] == "seedance":
        payload = {"model": spec["model"], "content": [{"type": "text", "text": spec["prompt"]}],
                   "duration": spec["seconds"], "ratio": spec["ratio"], "resolution": spec["resolution"], "watermark": False}
        if references:
            reference, ref = references[0]
            value = f'data:{ref["mime"]};base64,' + base64.b64encode(reference.read_bytes()).decode()
            payload["content"].append({"type": "image_url", "image_url": {"url": value}, "role": "first_frame"})
        return SEEDANCE, {"json": payload}, handles
    height = int(spec["resolution"][:-1])
    wide = round(height * 16 / 9 / 2) * 2
    dims = {"16:9": (wide, height), "9:16": (height, wide), "1:1": (height, height)}
    w, h = dims[spec["ratio"]]
    payload = {"model": spec["model"], "prompt": spec["prompt"], "seconds": str(spec["seconds"]), "size": f"{w}x{h}"}
    if row["protocol"] == "minimax":
        if references:
            reference, ref = references[0]
            url = f'data:{ref["mime"]};base64,' + base64.b64encode(reference.read_bytes()).decode()
            payload["metadata"] = {"content": [{"type": "image_url", "image_url": {"url": url}}]}
        return "/v1/videos", {"json": payload}, handles
    # Keep the multipart format of existing compatible channels.
    form = aiohttp.MultipartWriter("form-data")
    for key, value in payload.items():
        form.append(value).set_content_disposition("form-data", name=key)
    if references:
        reference, ref = references[0]
        handle = reference.open("rb")
        handles.append(handle)
        form.append(handle, {"Content-Type": ref["mime"]}).set_content_disposition(
            "form-data", name="input_reference", filename="reference." + ref["ext"])
    return "/v1/videos", {"data": form}, handles


class MediaGateway:
    def __init__(self, base, http, public):
        self.base, self.http, self.public = base, http, public

    @staticmethod
    def headers(row, key):
        return {"Authorization": "Bearer " + key, "X-Real-IP": row["address"], "X-Forwarded-For": row["address"],
                "Accept-Encoding": "identity", "X-Client-Request-ID": row["id"]}

    async def json_to_file(self, method, route, headers, path, limit, timeout, **args):
        async with self.http.request(method, self.base + route, headers=headers, allow_redirects=False,
                                     timeout=aiohttp.ClientTimeout(total=timeout), **args) as response:
            if 200 <= response.status < 300 and not (response.content_type == "application/json" or response.content_type.endswith("+json")):
                raise Review(502, "主站返回了无法识别的任务结果，请先查主站用量")
            if not 200 <= response.status < 300:
                # Keep error parsing bounded, including empty/generic proxy errors.
                with path.open("wb") as output:
                    output.write(await response.content.read(32 * 1024))
            else:
                try:
                    await stream_file(response, path, limit)
                except CanvasError as exc:
                    raise Review(exc.status, exc.message + "；请先查主站用量") from None
            return response.status

    async def submit(self, row, key, stage, result):
        spec = json.loads(row["input"])
        path, args, handles = await asyncio.to_thread(build_arguments, row, spec, stage)
        headers = self.headers(row, key)
        try:
            native = row["kind"] == "image" and row["protocol"] != "sync"
            suffix = "/async" if native else ""
            status = await self.json_to_file("POST", path + suffix, headers, result, row["result_limit"], 45 if native or row["kind"] == "video" else 600, **args)
            if (native and status == 404 and row["protocol"] == "auto" and
                    response_error(result) == "async image tasks are not enabled"):
                # This specific BPS error is returned before request admission. An ambiguous failure never falls back.
                for handle in handles:
                    handle.close()
                path, args, handles = await asyncio.to_thread(build_arguments, row, spec, stage)
                native = False
                status = await self.json_to_file("POST", path, headers, result, row["result_limit"], 600, **args)
            if not 200 <= status < 300:
                if status == 404 and native and response_error(result) == "async image tasks are not enabled":
                    raise CanvasError(409, "主站尚未开启异步生图，请在画布后台改为自动或同步上游")
                if status in (400, 401, 402, 403, 404, 413, 422, 429):
                    raise CanvasError(status, f"主站拒绝受理（HTTP {status}），请检查余额、模型或分组权限")
                raise Review(502, f"主站返回 HTTP {status}，受理情况不明，请先检查用量记录")
            if native or row["kind"] == "video":
                external = remote_id(result)
                if not external:
                    raise Review(502, "主站没有返回有效任务编号，请先检查用量记录")
                return external
            return None
        finally:
            for handle in handles:
                handle.close()

    def poll_path(self, row):
        prefix = "/v1/images/tasks" if row["kind"] == "image" else (SEEDANCE if row["protocol"] == "seedance" else "/v1/videos")
        return prefix + "/" + row["external"]

    async def poll(self, row, key, result):
        status = await self.json_to_file("GET", self.poll_path(row), self.headers(row, key), result, row["result_limit"], 45)
        if status in (401, 403, 404):
            raise Review(502, f"无法查询主站任务（HTTP {status}），请检查任务编号与分组")
        if status != 200:
            raise Pending()
        state = field(result, "status", "data.status")
        if state.lower() in FAILED:
            raise CanvasError(502, "主站报告任务失败，请用上游任务编号排查")
        if state.lower() not in SUCCESS:
            raise Pending()

    async def images(self, row, result, save, saved, path, allowed):
        source = "result.data.item" if row["external"] else "data.item"
        count = 0
        with result.open("rb") as stream:
            for item in ijson.items(stream, source):
                if not isinstance(item, dict) or count >= row["count"]:
                    break
                count += 1
                if saved(count):
                    continue
                url, b64 = item.get("url"), item.get("b64_json")
                if b64:
                    await asyncio.to_thread(decode_image, b64, row["cap"], path)
                elif url:
                    await download_media(self.public, url, allowed, path, row["cap"])
                else:
                    raise CanvasError(502, "主站返回的图片缺少内容")
                await save(count)
        if not count and row["external"]:
            if url := field(result, "image_url"):
                if not saved(1):
                    await download_media(self.public, url, allowed, path, row["cap"])
                    await save(1)
                count = 1
        if not count:
            raise CanvasError(502, "主站未返回可保存的图片")
        return count

    async def video(self, row, key, result, path, allowed):
        url = field(result, *VIDEO_URL)
        status_route = self.poll_path(row)
        # A BPS status response may rewrite video.url to the same gateway's content route.
        # Only the constructed authenticated path is eligible for a request with the managed key.
        if url and urlsplit(url).path != status_route + "/content":
            return await download_media(self.public, url, allowed, path, row["cap"])
        if row["protocol"] in ("openai", "minimax"):
            async with self.http.get(self.base + status_route + "/content", headers=self.headers(row, key),
                                     allow_redirects=False, timeout=aiohttp.ClientTimeout(total=160)) as response:
                if response.status == 200:
                    return await stream_file(response, path, row["cap"])
                if response.status in (302, 303, 307, 308) and response.headers.get("Location"):
                    return await download_media(self.public, response.headers["Location"], allowed, path, row["cap"])
                if retryable_download_status(response.status):
                    raise RetryableDownload(502, "主站的视频文件暂时无法读取")
        raise CanvasError(502, "主站未返回可保存的视频")


def decode_image(encoded, cap, path):
    if not isinstance(encoded, str) or len(encoded) > (cap + 2) // 3 * 4:
        raise CanvasError(413, "生成图片超过单图上限")
    try:
        with path.open("wb") as stream:
            for start in range(0, len(encoded), 131072):
                stream.write(base64.b64decode(encoded[start:start + 131072], validate=True))
    except binascii.Error:
        raise CanvasError(502, "主站图片编码有误") from None
