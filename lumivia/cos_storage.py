"""Private Tencent COS storage; credentials are encrypted using the existing server seed."""

import hmac
import os
import re
import time
import uuid
from base64 import urlsafe_b64encode
from pathlib import Path

from canvas_policy import CanvasError, HOST

BUCKET = re.compile(r"[a-z0-9][a-z0-9-]{0,48}-[0-9]{5,20}\Z")
REGION = re.compile(r"(?:ap|na|sa|eu|me)-[a-z][a-z0-9-]{1,29}\Z")
PREFIX = "lumivia-canvas/assets/"


class Profiles:
    def __init__(self, db):
        self.db = db
        db.execute("""CREATE TABLE IF NOT EXISTS canvas_cos (
            id INTEGER PRIMARY KEY, bucket TEXT NOT NULL, region TEXT NOT NULL, domain TEXT NOT NULL,
            secret_id BLOB NOT NULL, secret_key BLOB NOT NULL, checked INTEGER NOT NULL, UNIQUE(bucket, region))""")

    def cipher(self):
        from cryptography.fernet import Fernet
        seed = (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "canvas-key-seed").read_bytes()
        if len(seed) != 32:
            raise ValueError("Invalid canvas seed")
        return Fernet(urlsafe_b64encode(hmac.digest(seed, b"canvas-cos-v1", "sha256")))

    def public(self, profile_id):
        if not profile_id:
            return None
        row = self.db.execute("SELECT id, bucket, region, domain, checked FROM canvas_cos WHERE id=?", (profile_id,)).fetchone()
        return dict(row) if row else None

    def load(self, profile_id):
        row = self.db.execute("SELECT * FROM canvas_cos WHERE id=?", (profile_id,)).fetchone()
        if not row:
            raise CanvasError(503, "对象存储配置不可用")
        cipher = self.cipher()
        return Cos(row["bucket"], row["region"], row["domain"], cipher.decrypt(row["secret_id"]).decode(), cipher.decrypt(row["secret_key"]).decode())

    def candidate(self, body):
        if not isinstance(body, dict):
            raise CanvasError(400, "对象存储信息无效")
        bucket, region, domain, sid, skey = (body.get(k, "") for k in ("bucket", "region", "domain", "secretId", "secretKey"))
        if not all(isinstance(v, str) for v in (bucket, region, domain, sid, skey)):
            raise CanvasError(400, "对象存储信息无效")
        bucket, region, domain, sid, skey = (v.strip() for v in (bucket, region, domain, sid, skey))
        if not BUCKET.fullmatch(bucket) or not REGION.fullmatch(region) or (domain and not HOST.fullmatch(domain)):
            raise CanvasError(400, "请填写带 APPID 的 COS 桶名、地域和可选的 HTTPS 图片域名（仅主机名）")
        if domain in ("canvas.lumivia.cn", "api.lumivia.cn"):
            raise CanvasError(400, "图片域名不能占用现有画布或主站域名")
        if not sid and not skey:
            existing = self.db.execute("SELECT id FROM canvas_cos WHERE bucket=? AND region=?", (bucket, region)).fetchone()
            if existing:
                old = self.load(existing[0])
                sid, skey = old.sid, old.skey
        if not all(re.fullmatch(r"[a-zA-Z0-9]{12,128}", secret) for secret in (sid, skey)):
            raise CanvasError(400, "请填写该桶专用的 SecretId 和 SecretKey")
        # Check the durable key before issuing a write to the bucket.
        self.cipher()
        return Cos(bucket, region, domain, sid, skey)

    def save(self, profile):
        cipher = self.cipher()
        bucket, region = profile.bucket, profile.region
        self.db.execute("""
            INSERT INTO canvas_cos(bucket,region,domain,secret_id,secret_key,checked) VALUES(?,?,?,?,?,?)
            ON CONFLICT(bucket,region) DO UPDATE SET domain=excluded.domain, secret_id=excluded.secret_id,
                secret_key=excluded.secret_key, checked=excluded.checked
        """, (bucket, region, profile.domain, cipher.encrypt(profile.sid.encode()), cipher.encrypt(profile.skey.encode()), int(time.time() * 1000)))
        return self.db.execute("SELECT id FROM canvas_cos WHERE bucket=? AND region=?", (bucket, region)).fetchone()[0]


class Cos:
    def __init__(self, bucket, region, domain, sid, skey):
        self.bucket, self.region, self.domain, self.sid, self.skey = bucket, region, domain, sid, skey

    def client(self, domain=None):
        from qcloud_cos import CosConfig, CosS3Client
        return CosS3Client(CosConfig(Region=self.region, Scheme="https", Timeout=25, SecretId=self.sid, SecretKey=self.skey,
                                     **({"Domain": domain} if domain else {})), retry=1)

    def verify(self):
        key = PREFIX + "check-" + uuid.uuid4().hex
        client = self.client()
        client.put_object(Bucket=self.bucket, Key=key, Body=b"lumivia-canvas-check", ContentType="application/octet-stream",
                          ServerSideEncryption="AES256")
        try:
            result = client.get_object(Bucket=self.bucket, Key=key)["Body"].get_raw_stream()
            try:
                if result.read(32) != b"lumivia-canvas-check":
                    raise ValueError("COS check did not return uploaded content")
            finally:
                result.close()
        finally:
            client.delete_object(Bucket=self.bucket, Key=key)

    def put(self, key, path, mime):
        with path.open("rb") as stream:
            self.client().put_object(Bucket=self.bucket, Key=PREFIX + key, Body=stream, ContentType=mime,
                                     ServerSideEncryption="AES256")

    def delete(self, key):
        self.client().delete_object(Bucket=self.bucket, Key=PREFIX + key)

    def link(self, key, filename, preview=False, expires=300):
        if preview and not self.domain:
            raise CanvasError(409, "这个对象存储未配置预览域名，请下载原文件")
        disposition = "inline" if preview else "attachment"
        return self.client(self.domain if preview else None).get_presigned_url(
            Method="GET", Bucket=self.bucket, Key=PREFIX + key, Expired=expires,
            Params={"response-content-disposition": f'{disposition}; filename="{filename}"', "response-cache-control": "private, no-store"},
        )
