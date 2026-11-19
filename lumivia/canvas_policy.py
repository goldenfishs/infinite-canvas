"""Administrator settings. Existing assets keep the backend and retention chosen at write time."""

import json
import os
import re

MIB = 1024 ** 2
GIB = 1024 ** 3
MODEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,127}\Z")
HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\.[a-z]{2,63}\Z")
VIDEO_RESOLUTIONS = ("480p", "720p", "768p", "1080p", "1440p")
VIDEO_PROTOCOLS = ("seedance", "openai", "minimax")
DEFAULT = {
    "backend": "local", "retentionDays": 0, "totalLimitMiB": 8 * 1024, "diskReserveMiB": 4 * 1024,
    "imageMiB": 32, "videoMiB": 128, "workers": 1, "perUser": 2, "queueSize": 30, "memoryGuardMiB": 384,
    "imageEnabled": True, "imageMode": "auto", "imageGroup": 0, "imageModels": [],
    "chatEnabled": True, "chatGroups": [], "chatModels": [],
    "videoEnabled": False, "videoProtocol": "seedance", "videoGroup": 0, "videoModels": [], "videoSeconds": 12, "extraVideoRoutes": [],
    "resultHosts": [],
}
NUMBERS = {
    "retentionDays": (0, 3650), "totalLimitMiB": (1, 1024 * 1024), "diskReserveMiB": (0, 1024 * 1024),
    "imageMiB": (1, 32), "videoMiB": (1, 512), "workers": (1, 4), "perUser": (1, 12), "queueSize": (1, 300),
    "memoryGuardMiB": (0, 16384),
    "imageGroup": (0, 2 ** 53 - 1), "videoGroup": (0, 2 ** 53 - 1), "videoSeconds": (4, 15),
}
CHOICES = {
    "backend": ("local", "cos"), "imageMode": ("auto", "sync", "native"), "videoProtocol": VIDEO_PROTOCOLS,
}


class CanvasError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


class CanvasPolicy:
    def __init__(self, db, reserve):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS canvas_policy (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS canvas_policy_log (revision INTEGER PRIMARY KEY, actor INTEGER NOT NULL, created INTEGER NOT NULL);
        """)
        defaults = {**DEFAULT, "diskReserveMiB": reserve // MIB,
                    "totalLimitMiB": int(os.getenv("CANVAS_STORAGE_LIMIT_BYTES", 8 * GIB)) // MIB}
        db.execute("INSERT OR IGNORE INTO canvas_policy VALUES (1, 0, ?)", (json.dumps(defaults),))
        self.reload()

    def reload(self):
        self.revision, raw = self.db.execute("SELECT revision, value FROM canvas_policy WHERE id=1").fetchone()
        self.value = {**DEFAULT, **json.loads(raw)}

    def validate(self, revision, changes):
        if type(revision) is not int or revision != self.revision:
            raise CanvasError(409, "设置已变更，请重新读取后再保存")
        if not isinstance(changes, dict) or not changes.keys() <= DEFAULT.keys():
            raise CanvasError(400, "配置字段无效")
        result = {**self.value, **changes}
        for key, (low, high) in NUMBERS.items():
            if type(result[key]) is not int or not low <= result[key] <= high:
                raise CanvasError(400, f"{key} 必须在 {low} 至 {high} 之间")
        for key, values in CHOICES.items():
            if result[key] not in values:
                raise CanvasError(400, f"{key} 的选项无效")
        for key in ("imageEnabled", "chatEnabled", "videoEnabled"):
            if type(result[key]) is not bool:
                raise CanvasError(400, f"{key} 需要开关值")
        for key, pattern in (("imageModels", MODEL), ("chatModels", MODEL), ("videoModels", MODEL), ("resultHosts", HOST)):
            vals = result[key]
            if not isinstance(vals, list) or len(vals) > 40 or any(not isinstance(v, str) or not pattern.fullmatch(v) for v in vals):
                raise CanvasError(400, f"{key} 最多可配置 40 个有效名称")
            result[key] = list(dict.fromkeys(vals))
        groups = result["chatGroups"]
        if not isinstance(groups, list) or len(groups) > 40 or any(type(g) is not int or not 0 < g < 2 ** 53 for g in groups):
            raise CanvasError(400, "对话分组需填写有效的数字 ID")
        routes = result["extraVideoRoutes"]
        if not isinstance(routes, list) or len(routes) > 12:
            raise CanvasError(400, "额外视频渠道最多 12 条")
        models = set(result["videoModels"])
        for route in routes:
            required = {"model", "group", "protocol"}
            if not isinstance(route, dict) or not required <= route.keys() <= required | {"resolutions", "durations"}:
                raise CanvasError(400, "额外视频渠道格式无效")
            model, group, protocol = route["model"], route["group"], route["protocol"]
            if (not isinstance(model, str) or not MODEL.fullmatch(model) or model in models or type(group) is not int
                    or not 0 < group < 2 ** 53 or protocol not in CHOICES["videoProtocol"]):
                raise CanvasError(400, "额外视频渠道需要不重复的模型名、有效分组和协议")
            resolutions, durations = route.get("resolutions", []), route.get("durations", [])
            if not isinstance(resolutions, list) or len(resolutions) > len(VIDEO_RESOLUTIONS) or any(r not in VIDEO_RESOLUTIONS for r in resolutions):
                raise CanvasError(400, "视频分辨率无效")
            if not isinstance(durations, list) or len(durations) > 12 or any(type(d) is not int or not 4 <= d <= result["videoSeconds"] for d in durations):
                raise CanvasError(400, "可选视频时长必须在 4 秒与全站上限之间")
            models.add(model)
        if bool(result["videoGroup"]) != bool(result["videoModels"]):
            raise CanvasError(400, "默认视频渠道的分组和模型必须同时配置")
        if result["videoEnabled"] and not models:
            raise CanvasError(400, "启用视频前请配置至少一个 Sub2 视频模型和分组")
        return result

    def video_routes(self):
        if not self.value["videoEnabled"]:
            return {}
        primary = {model: (self.value["videoGroup"], self.value["videoProtocol"]) for model in self.value["videoModels"]}
        return {**primary, **{r["model"]: (r["group"], r["protocol"]) for r in self.value["extraVideoRoutes"]}}

    def video_options(self, model, protocol):
        route = next((r for r in self.value["extraVideoRoutes"] if r["model"] == model), {})
        resolutions = route.get("resolutions") or (["768p"] if protocol == "minimax" else ["480p", "720p", "1080p"])
        duration = route.get("durations") or []
        return {"resolutions": list(dict.fromkeys(resolutions)), "durations": sorted(set(duration))}

    def permits(self, kind, model):
        return self.value[kind + "Enabled"] and (not self.value[kind + "Models"] or model in self.value[kind + "Models"])

    def chat_key_allowed(self, key):
        groups = self.value["chatGroups"]
        ids = key.get("group_ids") or [key.get("group_id")]
        return self.value["chatEnabled"] and (not groups or all(g in groups for g in ids))
