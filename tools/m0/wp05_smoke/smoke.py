#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WP05 最小环境冒烟脚本（M0 / G0-18、G0-19、G0-21）

它验证什么
----------
本脚本只回答一个问题：**环境重启之后，元数据与文件还在不在、内容有没有变**。
因此它刻意做成「两腿 + 两阶段」：

  阶段 --write ：PG 腿建表并写入一行（带 UUID、时间戳、内容摘要），
                 文件腿把同一份内容写到对象存储；
  阶段 --verify：重启（容器或服务）之后，重新读库找「最近一次写入」，
                 再按这条记录里的 object_key 把文件取回来，
                 重新算 SHA-256 与库里的 content_digest 比对。

为什么「期望值」放在库里而不是本地文件
--------------------------------------
如果摘要期望值只存在本地临时文件，那校验只证明了「本地文件还在」；
把它落进 PG 之后，校验才能真正覆盖「数据库 + 对象存储两侧同时存活且一致」。
这是 G0-19 要的语义。

为什么要有 fs 后端
------------------
本机 Docker 不可用时（colima 拉不到 VM 镜像），MinIO 起不来。
fs 后端把「文件腿」降级为本地目录，用来验证脚本与 PG 腿本身可用；
**它不代表 MinIO 行为**（不校验签名、bucket 策略、分片上传、版本、生命周期）。
脚本在每次以 fs 运行时都会显式打印这条警告，报告 JSON 里也标 degraded=true。

退出码
------
0 = 所有阻断性检查通过；1 = 存在失败（含未捕获异常）。
非阻断的信息性检查（info）不影响退出码，只在报告里留痕。

环境变量（都有默认值，默认值面向「本机原生 PG」）
-------------------------------------------------
PG_HOST=localhost  PG_PORT=5432  PG_USER=rednote  PG_PASSWORD=（空）  PG_DATABASE=postgres
SMOKE_BACKEND=s3 | fs
S3_ENDPOINT=http://localhost:9000  S3_ACCESS_KEY=minioadmin  S3_SECRET_KEY=minioadmin
S3_BUCKET=rednote-dev  S3_REGION=us-east-1
FS_BLOB_ROOT=evidence/m0/blobs

引号口径：全文使用 ASCII 直引号。
"""

from __future__ import annotations

import abc
import argparse
import hashlib
import json
import os
import platform
import secrets
import socket
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

SCHEMA_VERSION = "wp05.smoke.v1"

# 对象键前缀：集中一处，避免 write/verify 两侧拼错导致「明明写了却说找不到」
OBJECT_PREFIX = "wp05/smoke"

# 内容填充大小。64 KiB 的用意：足够大到不是「一行字符串」级别的假写入，
# 又足够小到不触发 S3 分片上传（分片能力属于 S3 兼容子集，需在 M1 单独验证）。
DEFAULT_FILLER_BYTES = 64 * 1024

# ---------------------------------------------------------------- 工具函数


def now_local() -> datetime:
    """带本地时区的当前时间（日志给人看，报告用 UTC）。"""
    return datetime.now().astimezone()


def log(message: str) -> None:
    """带时间戳输出。日志文件靠 stdout 重定向留存，所以每行都必须自带时间戳。"""
    print(f"[{now_local().isoformat(timespec='seconds')}] {message}", flush=True)


def env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        # 参数写错要立刻炸，不要静默退回默认值 —— 否则会连到一个"看起来对"的库
        raise SystemExit(f"环境变量 {name}={raw!r} 不是整数") from exc


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


# ---------------------------------------------------------------- 检查项收集


class Checker:
    """收集检查结果。blocking=True 的项任一失败 -> 整体失败 -> 退出码 1。

    info 项（非阻断）专门用于「值得记录、但不宜据此判失败」的观察，
    例如 S3 ETag 与 MD5 是否一致（开启服务端加密时二者本来就会不等）。
    """

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, name: str, ok: bool, detail: str, blocking: bool = True) -> bool:
        self.items.append(
            {"name": name, "ok": bool(ok), "blocking": blocking, "detail": detail}
        )
        tag = "PASS" if ok else ("FAIL" if blocking else "INFO")
        log(f"  [{tag}] {name} :: {detail}")
        return ok

    def info(self, name: str, detail: str) -> None:
        self.items.append({"name": name, "ok": True, "blocking": False, "detail": detail})
        log(f"  [INFO] {name} :: {detail}")

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [i for i in self.items if i["blocking"] and not i["ok"]]

    @property
    def passed(self) -> bool:
        return not self.failed

    def counts(self) -> dict[str, int]:
        return {
            "total": len(self.items),
            "blockingPassed": len([i for i in self.items if i["blocking"] and i["ok"]]),
            "blockingFailed": len(self.failed),
            "info": len([i for i in self.items if not i["blocking"]]),
        }


# ---------------------------------------------------------------- PG 腿


class PgLeg:
    """PG 腿：连接、建最小表、写一行、读回。

    表结构与 deploy/dev/initdb/01_init.sql 保持一致。
    为什么脚本自己也建表：本机原生 PG 不走容器 initdb，
    脚本必须能在一个「全新的库」上自举，否则原生环境根本无法验证。
    """

    DDL_SMOKE_META = """
    CREATE TABLE IF NOT EXISTS smoke_meta (
        id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
        run_id         uuid        NOT NULL,
        backend        text        NOT NULL,
        object_key     text        NOT NULL,
        content_digest text        NOT NULL,
        content_bytes  bigint      NOT NULL CHECK (content_bytes > 0),
        created_at     timestamptz NOT NULL DEFAULT now(),
        extra          jsonb       NOT NULL DEFAULT '{}'::jsonb
    )
    """

    DDL_INDEX = """
    CREATE INDEX IF NOT EXISTS smoke_meta_created_at_idx
        ON smoke_meta (created_at DESC)
    """

    def __init__(self) -> None:
        self.host = env_str("PG_HOST", "localhost")
        self.port = env_int("PG_PORT", 5432)
        self.user = env_str("PG_USER", "rednote")
        # 口令允许为空（本机 brew 装的 PG 默认 trust）；空串按 None 传给 libpq
        self.password = os.environ.get("PG_PASSWORD") or None
        self.dbname = env_str("PG_DATABASE", "postgres")
        self.conn: psycopg.Connection | None = None

    def describe(self) -> dict[str, Any]:
        """给报告用的连接信息。刻意不包含口令。"""
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "database": self.dbname,
            "passwordProvided": self.password is not None,
        }

    def connect(self) -> psycopg.Connection:
        # connect_timeout 必须给：没有它，网络不通时会挂到天荒地老，
        # 冒烟脚本卡死比报错更难排查。
        self.conn = psycopg.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            dbname=self.dbname,
            connect_timeout=10,
            application_name="wp05-smoke",
        )
        return self.conn

    def close(self) -> None:
        if self.conn is not None and not self.conn.closed:
            self.conn.close()

    # --- 只读探针 -------------------------------------------------------

    def server_version(self) -> str:
        assert self.conn is not None
        row = self.conn.execute("SELECT version()").fetchone()
        return str(row[0]) if row else "unknown"

    def extensions(self) -> dict[str, str]:
        assert self.conn is not None
        rows = self.conn.execute(
            "SELECT extname, extversion FROM pg_extension ORDER BY extname"
        ).fetchall()
        return {str(r[0]): str(r[1]) for r in rows}

    def available_extensions(self) -> list[str]:
        """PG 二进制里「能装」的扩展。vector 在不在，以此为判据。"""
        assert self.conn is not None
        rows = self.conn.execute(
            "SELECT name FROM pg_available_extensions WHERE name = ANY(%s) ORDER BY name",
            (["vector", "pg_trgm", "pgcrypto", "uuid-ossp"],),
        ).fetchall()
        return [str(r[0]) for r in rows]

    def smoke_meta_pre_exists(self) -> bool:
        """在自举建表之前先探一次：用来区分「重启把表弄丢了」和「本来就没建过」。"""
        assert self.conn is not None
        row = self.conn.execute("SELECT to_regclass('public.smoke_meta')").fetchone()
        return row is not None and row[0] is not None

    def row_count(self) -> int:
        assert self.conn is not None
        if not self.smoke_meta_pre_exists():
            return 0
        row = self.conn.execute("SELECT count(*) FROM smoke_meta").fetchone()
        return int(row[0]) if row else 0

    # --- 读写 -----------------------------------------------------------

    def bootstrap(self) -> None:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute(self.DDL_SMOKE_META)
            cur.execute(self.DDL_INDEX)
        self.conn.commit()

    def insert_run(self, record: dict[str, Any]) -> None:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO smoke_meta
                    (id, run_id, backend, object_key, content_digest, content_bytes, created_at, extra)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record["id"],
                    record["run_id"],
                    record["backend"],
                    record["object_key"],
                    record["content_digest"],
                    record["content_bytes"],
                    record["created_at"],
                    json.dumps(record["extra"], ensure_ascii=False),
                ),
            )
        # 显式提交：对象存储已写入 + 元数据已提交，才算一次完整的 --write
        self.conn.commit()

    def fetch_latest(self) -> dict[str, Any] | None:
        """取最近一次写入。verify 靠它拿到「重启前的期望值」。"""
        assert self.conn is not None
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, run_id, backend, object_key, content_digest,
                       content_bytes, created_at, extra
                  FROM smoke_meta
                 ORDER BY created_at DESC, id DESC
                 LIMIT 1
                """
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "run_id": row[1],
            "backend": row[2],
            "object_key": row[3],
            "content_digest": row[4],
            "content_bytes": row[5],
            "created_at": row[6],
            "extra": row[7],
        }


# ---------------------------------------------------------------- 文件腿


class BlobStore(abc.ABC):
    """文件腿的抽象接口。

    为什么要抽象：本机没有 MinIO 时，必须能换一个等价的最小实现把「写入/读回/摘要比对」
    这条链路走通，同时不让人误以为「本地目录 = MinIO 已验证」。
    因此接口里强制带 degraded / degraded_note 两个字段，把这个区别带到报告里。
    """

    backend_name: str = "abstract"
    degraded: bool = False
    degraded_note: str = ""

    @abc.abstractmethod
    def put(self, key: str, data: bytes) -> dict[str, Any]:
        """写入并返回后端侧元信息（供报告留痕）。"""

    @abc.abstractmethod
    def get(self, key: str) -> bytes:
        """读取；对象不存在必须抛异常，不得返回 None 或空字节。"""

    @abc.abstractmethod
    def exists(self, key: str) -> bool:
        """存在性判断（verify 的第一道证据）。"""

    @abc.abstractmethod
    def describe(self) -> dict[str, Any]:
        """报告用的后端描述。"""

    def announce(self) -> None:
        """把「这是替身还是真对象存储」明确打到日志里。"""
        if self.degraded:
            log("[WP05][降级] 文件腿使用 fs 后端（本地目录）。")
            log("[WP05][降级] 这是降级替身，不代表 MinIO 行为：不校验签名、bucket 策略、"
                "分片上传、生命周期、版本控制。")
            log("[WP05][降级] G0-18 / G0-20 的对象存储部分必须在内网 MinIO 上重跑。")
            log(f"[WP05][降级] 降级原因：{self.degraded_note}")


class FsBlobStore(BlobStore):
    """本地目录实现 —— 降级替身。"""

    backend_name = "fs"
    degraded = True
    degraded_note = "本机 Docker/colima 不可用，无法启动 MinIO；仅用于验证脚本与 PG 腿。"

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path_for(self, key: str) -> Path:
        target = (self.root / key).resolve()
        # 对象键内部生成，理论上不会越界；但显式拦住目录穿越是零成本的好习惯
        if not str(target).startswith(str(self.root) + os.sep):
            raise ValueError(f"对象键越出存储根目录: {key}")
        return target

    def put(self, key: str, data: bytes) -> dict[str, Any]:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {
            "backend": self.backend_name,
            "degraded": True,
            "absolutePath": str(path),
            "bytes": len(data),
        }

    def get(self, key: str) -> bytes:
        path = self._path_for(key)
        if not path.is_file():
            raise FileNotFoundError(f"对象不存在: {path}")
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path_for(key).is_file()

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.backend_name,
            "degraded": True,
            "degradedNote": self.degraded_note,
            "root": str(self.root),
            "rootExists": self.root.is_dir(),
        }


class S3BlobStore(BlobStore):
    """S3 兼容实现（boto3）。

    为什么显式指定 signature_version=s3v4 + addressing_style=path：
      MinIO 部署在 localhost 时，virtual-host 风格会把 bucket 拼进主机名
      （rednote-dev.localhost），本机 DNS 解析不到。path 风格避开这个坑。
    """

    backend_name = "s3"
    degraded = False

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str,
    ) -> None:
        self.endpoint = endpoint
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket = bucket
        self.region = region
        self.client: Any = None

    def connect(self) -> Any:
        import boto3
        from botocore.config import Config

        # proxies={} 显式清空代理：环境若设了 HTTP_PROXY/HTTPS_PROXY，boto3 会把
        # 指向 localhost 的 S3 请求也送去代理，被代理拒成 502 Bad Gateway。
        # 这不是 MinIO 的问题，是代理误拦环回地址——内网环境同样可能设代理，故这里固定绕开。
        self.client = boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=10,
                read_timeout=30,
                proxies={},
            ),
        )
        return self.client

    def ensure_bucket(self) -> bool:
        """bucket 存在则 True，创建了则 True，无法访问则抛异常。"""
        from botocore.exceptions import ClientError

        try:
            self.client.head_bucket(Bucket=self.bucket)
            return True
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchBucket", "NotFound"}:
                self.client.create_bucket(Bucket=self.bucket)
                return True
            raise

    def put(self, key: str, data: bytes) -> dict[str, Any]:
        resp = self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType="application/octet-stream",
            Metadata={"wp05-md5": md5_hex(data)},
        )
        return {
            "backend": self.backend_name,
            "degraded": False,
            "bucket": self.bucket,
            "bytes": len(data),
            "etag": str(resp.get("ETag", "")).strip('"'),
        }

    def get(self, key: str) -> bytes:
        resp = self.client.get_object(Bucket=self.bucket, Key=key)
        return resp["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.backend_name,
            "degraded": False,
            "endpoint": self.endpoint,
            "bucket": self.bucket,
            "region": self.region,
        }


def build_blob_store(backend: str) -> BlobStore:
    if backend == "fs":
        return FsBlobStore(Path(env_str("FS_BLOB_ROOT", "evidence/m0/blobs")))
    if backend == "s3":
        return S3BlobStore(
            endpoint=env_str("S3_ENDPOINT", "http://localhost:9000"),
            access_key=env_str("S3_ACCESS_KEY", "minioadmin"),
            secret_key=env_str("S3_SECRET_KEY", "minioadmin"),
            bucket=env_str("S3_BUCKET", "rednote-dev"),
            region=env_str("S3_REGION", "us-east-1"),
        )
    raise SystemExit(f"未知 backend: {backend!r}（可选 s3 | fs）")


# ---------------------------------------------------------------- 报告


def write_report(path: str | None, payload: dict[str, Any]) -> None:
    if not path:
        return
    target = Path(path)
    if str(target.parent) not in ("", "."):
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log(f"报告已写出: {target.resolve()}")


def build_report_base(stage: str, started: datetime) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "stage": stage,
        "startedAt": started.astimezone(timezone.utc).isoformat(),
        "startedAtLocal": started.isoformat(),
        "tool": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "hostname": socket.gethostname(),
            "cwd": str(Path.cwd()),
            "script": str(Path(__file__).resolve()),
        },
    }


def summarize(stage: str, checker: Checker, report: dict[str, Any]) -> None:
    counts = checker.counts()
    log("-" * 72)
    log(f"阶段 {stage} 结果：阻断项通过 {counts['blockingPassed']} / 失败 "
        f"{counts['blockingFailed']}；信息项 {counts['info']}")
    if checker.failed:
        log("失败明细：")
        for item in checker.failed:
            log(f"  - {item['name']}: {item['detail']}")
    log(f"总体判定：{'PASS' if checker.passed else 'FAIL'}")
    if report.get("blob", {}).get("degraded"):
        log("注意：文件腿为降级替身（fs），对象存储结论不能据此判定。")
    log("-" * 72)


# ---------------------------------------------------------------- 阶段实现


def run_write(args: argparse.Namespace) -> int:
    started = now_local()
    stage = "write"
    log("=" * 72)
    log(f"WP05 冒烟 / {stage} / backend={args.backend} / {started.isoformat(timespec='seconds')}")
    log("=" * 72)

    checker = Checker()
    report = build_report_base(stage, started)
    report["checks"] = checker.items

    pg = PgLeg()
    log(f"PG 目标：{pg.user}@{pg.host}:{pg.port}/{pg.dbname}")

    store = build_blob_store(args.backend)
    store.announce()

    run_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc)
    object_key = f"{OBJECT_PREFIX}/{run_id}.bin"

    payload_text = (
        f"WP05-SMOKE runId={run_id} writtenAt={created_at.isoformat()} "
        f"host={socket.gethostname()}\n"
    ).encode("utf-8")
    # 填充随机字节：让校验真正覆盖「二进制内容没被截断/改写」，而不是只比对一个字符串
    blob = payload_text + secrets.token_bytes(DEFAULT_FILLER_BYTES)
    digest = sha256_hex(blob)

    log(f"run_id={run_id}")
    log(f"object_key={object_key}")
    log(f"内容字节数={len(blob)} sha256={digest}")
    log("检查明细：")

    try:
        # --- PG 腿 ---
        pg.connect()
        server_version = pg.server_version()
        available = pg.available_extensions()
        report["pg"] = {
            **pg.describe(),
            "serverVersion": server_version,
            "extensionsInstalled": pg.extensions(),
            "extensionsAvailable": available,
        }
        checker.add("pg.connect", True, f"已连接 {pg.host}:{pg.port}/{pg.dbname}")
        checker.add("pg.server_version_is_17", server_version.startswith("PostgreSQL 17"),
                    server_version.split(" on ")[0])
        # vector 缺失不阻断：本机原生 PG 没有 pgvector 二进制，
        # 该能力属「容器/内网环境验证项」，把它记为 info 而不是 FAIL，
        # 否则本机永远跑不出一个通过的冒烟。
        if "vector" in available:
            checker.add("pg.extension_vector", True, "pgvector 可用")
        else:
            checker.info("pg.extension_vector",
                         "本机 PG 未提供 vector 扩展（缺 pgvector 二进制）——环境限制，待内网 pgvector 镜像验证")

        pg.bootstrap()
        checker.add("pg.bootstrap_smoke_meta", True, "smoke_meta / 索引已就绪（IF NOT EXISTS）")

        pg.insert_run(
            {
                "id": str(uuid.uuid4()),
                "run_id": run_id,
                "backend": store.backend_name,
                "object_key": object_key,
                "content_digest": digest,
                "content_bytes": len(blob),
                "created_at": created_at,
                "extra": {
                    "schemaVersion": SCHEMA_VERSION,
                    "hostname": socket.gethostname(),
                    "python": sys.version.split()[0],
                },
            }
        )
        checker.add("pg.insert_row", True, "已写入 smoke_meta 并提交事务")

        # 立刻读回一次：先排除"写进去了但读不出来"这种低级问题，
        # 免得把问题拖到重启之后，误判成"重启导致丢失"。
        back = pg.fetch_latest()
        ok_readback = bool(
            back
            and str(back["run_id"]) == run_id
            and back["content_digest"] == digest
            and int(back["content_bytes"]) == len(blob)
        )
        checker.add("pg.readback_matches", ok_readback,
                    "读回一致" if ok_readback else f"读回不一致: {back}")

        # --- 文件腿 ---
        if isinstance(store, S3BlobStore):
            store.connect()
            store.ensure_bucket()
            checker.add("s3.bucket_ready", True, f"bucket={store.bucket} 可用（不存在则已创建）")

        put_info = store.put(object_key, blob)
        checker.add("blob.put", True, f"已写入 {store.backend_name}")

        exists_now = store.exists(object_key)
        checker.add("blob.exists_after_put", exists_now, f"object_key={object_key}")

        fetched = store.get(object_key)
        checker.add("blob.readback_digest", sha256_hex(fetched) == digest,
                    f"读回 {len(fetched)} 字节，sha256 与写入值{'一致' if sha256_hex(fetched) == digest else '不一致'}")

        if isinstance(store, S3BlobStore):
            # 单次 PUT 的 ETag 通常等于内容 MD5；开启服务端加密后会不等，
            # 所以只作 info，不据此判失败。
            etag = str(put_info.get("etag", ""))
            checker.info("s3.etag_vs_md5",
                         f"etag={etag} md5={md5_hex(blob)} "
                         f"{'一致（单次 PUT 且未启用 SSE）' if etag == md5_hex(blob) else '不一致（可能启用了 SSE/分片；不阻断）'}")

        report["blob"] = {**store.describe(), "objectKey": object_key, "put": put_info}
        report["runId"] = run_id
        report["contentDigest"] = digest
        report["contentBytes"] = len(blob)

    except Exception as exc:  # noqa: BLE001 —— 冒烟脚本要"如实记录失败"，不能崩成堆栈了事
        checker.add("unhandled_error", False, f"{type(exc).__name__}: {exc}")
        report["error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
    finally:
        pg.close()

    finished = now_local()
    report["finishedAt"] = finished.astimezone(timezone.utc).isoformat()
    report["durationMs"] = int((finished - started).total_seconds() * 1000)
    report["checks"] = checker.items
    report["counts"] = checker.counts()
    report["passed"] = checker.passed

    summarize(stage, checker, report)
    write_report(args.report, report)
    return 0 if checker.passed else 1


def run_verify(args: argparse.Namespace) -> int:
    started = now_local()
    stage = "verify"
    log("=" * 72)
    log(f"WP05 冒烟 / {stage} / backend={args.backend} / {started.isoformat(timespec='seconds')}")
    log("=" * 72)

    checker = Checker()
    report = build_report_base(stage, started)

    pg = PgLeg()
    log(f"PG 目标：{pg.user}@{pg.host}:{pg.port}/{pg.dbname}")

    store = build_blob_store(args.backend)
    store.announce()

    log("检查明细：")

    try:
        pg.connect()
        server_version = pg.server_version()
        report["pg"] = {
            **pg.describe(),
            "serverVersion": server_version,
            "extensionsInstalled": pg.extensions(),
            "extensionsAvailable": pg.available_extensions(),
        }
        checker.add("pg.connect", True, f"已连接 {pg.host}:{pg.port}/{pg.dbname}")

        # 「重启后表还在吗」必须在自举建表之前探，否则永远为真
        table_existed = pg.smoke_meta_pre_exists()
        checker.add("pg.smoke_meta_survived_restart", table_existed,
                    "smoke_meta 表在重启后仍然存在" if table_existed
                    else "smoke_meta 表不存在 —— 元数据丢失或被重置")

        # 自举是幂等的；即使上面判失败也继续跑，好让报告同时给出「行数」「对象是否还在」
        pg.bootstrap()
        rows = pg.row_count()
        checker.add("pg.row_count_positive", rows > 0, f"smoke_meta 行数={rows}")

        record = pg.fetch_latest()
        if record is None:
            checker.add("pg.fetch_latest", False,
                        "smoke_meta 无任何行 —— 无法确定重启前的期望值，校验失败")
            raise RuntimeError("smoke_meta 中没有任何记录，verify 无法继续")

        checker.add("pg.fetch_latest", True, f"最近一次写入 run_id={record['run_id']}")

        report["runId"] = str(record["run_id"])
        report["contentDigest"] = str(record["content_digest"])
        report["contentBytes"] = int(record["content_bytes"])
        report["writtenAt"] = record["created_at"].isoformat()
        report["writtenBackend"] = str(record["backend"])

        # 写入时用的后端与本次校验的后端必须一致，否则「找不到对象」是必然的，
        # 把这个前置条件显式判出来，避免误导成"数据丢了"。
        backend_match = str(record["backend"]) == store.backend_name
        checker.add("backend.consistency", backend_match,
                    f"写入后端={record['backend']} 校验后端={store.backend_name}"
                    + ("" if backend_match else " —— 不一致，本次校验无效"))

        # --- 文件腿 ---
        if isinstance(store, S3BlobStore):
            store.connect()
            store.ensure_bucket()
            checker.add("s3.bucket_ready", True, f"bucket={store.bucket} 可用（不存在则已创建）")

        object_key = str(record["object_key"])
        exists = store.exists(object_key)
        checker.add("blob.exists_after_restart", exists, f"object_key={object_key}")

        if not exists:
            raise RuntimeError(f"对象缺失：{object_key}")

        data = store.get(object_key)
        actual_digest = sha256_hex(data)
        expected_digest = str(record["content_digest"])
        checker.add("blob.digest_matches", actual_digest == expected_digest,
                    f"对象 sha256={actual_digest} 库中期望={expected_digest}")

        size_match = len(data) == int(record["content_bytes"])
        checker.add("blob.size_matches", size_match,
                    f"对象 {len(data)} 字节 / 库中 {record['content_bytes']} 字节")

        # 读回后再查一次库：确认 verify 自身没有产生副作用（本阶段是只读的）
        again = pg.row_count()
        checker.add("pg.verify_is_read_only", again == rows,
                    f"verify 前后行数 {rows} -> {again}")

        report["blob"] = {**store.describe(), "objectKey": object_key, "bytesRead": len(data)}

    except Exception as exc:  # noqa: BLE001
        checker.add("unhandled_error", False, f"{type(exc).__name__}: {exc}")
        report["error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
    finally:
        pg.close()

    finished = now_local()
    report["finishedAt"] = finished.astimezone(timezone.utc).isoformat()
    report["durationMs"] = int((finished - started).total_seconds() * 1000)
    report["checks"] = checker.items
    report["counts"] = checker.counts()
    report["passed"] = checker.passed

    summarize(stage, checker, report)
    write_report(args.report, report)
    return 0 if checker.passed else 1


# ---------------------------------------------------------------- 入口


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="smoke.py",
        description="WP05 最小环境冒烟：PG 腿 + 文件腿（写 -> 重启 -> 校验）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    stage = parser.add_mutually_exclusive_group(required=True)
    stage.add_argument("--write", action="store_const", const="write", dest="stage",
                       help="写入阶段：建表 + 写一行元数据 + 写一个对象")
    stage.add_argument("--verify", action="store_const", const="verify", dest="stage",
                       help="校验阶段：读回最近一次写入并比对摘要（只读，无副作用）")
    parser.add_argument("--backend", choices=["s3", "fs"],
                        default=env_str("SMOKE_BACKEND", "s3"),
                        help="文件腿后端：s3 = MinIO/S3 兼容；fs = 本地目录降级替身（默认取 SMOKE_BACKEND，否则 s3）")
    parser.add_argument("--report", default=None,
                        help="报告 JSON 输出路径；父目录不存在会自动创建")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.stage == "write":
        return run_write(args)
    return run_verify(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
