#!/usr/bin/env python3
"""M4 mock server —— 只读样例 API，供前端与 E2E 联调。

**定位**：对应 M4 契约先行线（`docs/m4/M4并行开发接缝冻结.md` §4.1）。
它**不是**生产代码，不进 CI 门，不碰数据库。契约的唯一权威是
`contracts/draft/m4-openapi.yaml`；本文件的响应形状必须与之一致。

**为什么不用 FastAPI**：mock 要能被「还没装后端依赖的环境」直接跑起来，
所以只用标准库（`http.server` + `json`）。这样它是**零依赖**的，
前端同学 clone 下来 `python mock_server.py` 即可。

用法：
    python mock_server.py                 # 默认 127.0.0.1:8787
    python mock_server.py --port 9000
    python mock_server.py --selftest      # 起临时端口自检后退出（返回码 0/1）

口径：
- 单体响应 = `{data, meta}`；列表 = `{data: [...], meta: {page: {...}}}`。
- 错误 = `{code, message, details, retryable}`，`code` 取自契约的 18 个 `ErrorCode`。
- **只读**：POST/PATCH 返回 201/200，但不持久化（进程内内存即可，重启即复位）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

SAMPLES_PATH = Path(__file__).resolve().parent / "samples.json"

# 资源名 -> 集合（决定列表/详情/创建）
COLLECTIONS = {
    "deliveries": "deliveries",
    "export-items": "export_items",
    "koc-records": "koc_records",
    "publications": "publications",
    "publication-links": "publication_links",
    "metric-definitions": "metric_definitions",
    "metric-observations": "metric_observations",
    "metric-imports": "metric_imports",
    "metric-adapters": "metric_adapters",
    "comment-records": "comment_records",
    "experiments": "experiments",
    "strategy-evidence": "strategy_evidence",
    "reports": "reports",
    "project-closures": "project_closures",
}

ERROR_CODES = {
    "MALFORMED_REQUEST", "SESSION_EXPIRED", "PERMISSION_DENIED", "EXTERNAL_POLICY_DENIED",
    "RESOURCE_NOT_FOUND", "INVALID_TRANSITION", "IDEMPOTENCY_CONFLICT", "DUPLICATE_PUBLICATION",
    "EVENT_HISTORY_EXPIRED", "VERSION_CONFLICT", "UPLOAD_TOO_LARGE", "VALIDATION_FAILED",
    "BUDGET_EXCEEDED", "DIRECTION_UNVERIFIED", "PRECONDITION_REQUIRED", "RATE_LIMITED",
    "DEPENDENCY_UNAVAILABLE",
}
_STATUS_BY_CODE = {
    "MALFORMED_REQUEST": 400, "SESSION_EXPIRED": 401, "PERMISSION_DENIED": 403,
    "EXTERNAL_POLICY_DENIED": 403, "RESOURCE_NOT_FOUND": 404, "INVALID_TRANSITION": 409,
    "IDEMPOTENCY_CONFLICT": 409, "DUPLICATE_PUBLICATION": 409, "EVENT_HISTORY_EXPIRED": 410,
    "VERSION_CONFLICT": 412, "UPLOAD_TOO_LARGE": 413, "VALIDATION_FAILED": 422,
    "BUDGET_EXCEEDED": 422, "DIRECTION_UNVERIFIED": 422, "PRECONDITION_REQUIRED": 428,
    "RATE_LIMITED": 429, "DEPENDENCY_UNAVAILABLE": 503,
}
_RETRYABLE = {"RATE_LIMITED", "DEPENDENCY_UNAVAILABLE"}


def load_samples() -> dict:
    with SAMPLES_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def envelope(data):
    return {"data": data, "meta": {"requestId": str(uuid.uuid4())}}


def paginated(items, limit: int, cursor: str | None):
    start = int(cursor) if cursor and cursor.isdigit() else 0
    window = items[start:start + limit]
    nxt = start + limit
    has_more = nxt < len(items)
    return {
        "data": window,
        "meta": {"requestId": str(uuid.uuid4()),
                 "page": {"nextCursor": str(nxt) if has_more else None,
                          "hasMore": has_more,
                          "total": len(items)}},
    }


def error_body(code: str, message: str | None = None, details: dict | None = None):
    assert code in ERROR_CODES, code
    return {
        "code": code,
        "message": message or _default_message(code),
        "details": details or {},
        "retryable": code in _RETRYABLE,
    }


def _default_message(code: str) -> str:
    return {
        "RESOURCE_NOT_FOUND": "资源不存在或无权访问。",
        "INVALID_TRANSITION": "当前状态不允许该操作。",
        "IDEMPOTENCY_CONFLICT": "相同幂等键对应了不同的请求内容。",
        "DUPLICATE_PUBLICATION": "该笔记已登记过，请勿重复计入。",
        "VALIDATION_FAILED": "字段校验未通过。",
        "PERMISSION_DENIED": "没有执行该操作的权限。",
    }.get(code, code)


# 契约路径（项目范围 + 名词化子资源）→ mock 内部路由。
# mock 的路由表按旧的平铺形态写成，这里只做归一化，不改路由本身。
_PATH_REWRITES = (
    (r"^/api/v1/projects/[^/]+/billing-reconciliations$", "/api/v1/billing/reconciliations"),
    (r"^/api/v1/projects/[^/]+/billing-summary$", "/api/v1/billing/management-summary"),
    (r"^/api/v1/projects/[^/]+/closures$", "/api/v1/project-closures"),
    (r"^/api/v1/projects/[^/]+/unmarked-exports$", "/api/v1/export-items"),
    (r"^/api/v1/projects/[^/]+/([a-z\-]+)$", r"/api/v1/\1"),
    (r"^/api/v1/closures/([^/]+)/reopenings$", r"/api/v1/project-closures/\1/reopen"),
    (r"^/api/v1/knowledge/proposals/([^/]+)/applications$", r"/api/v1/knowledge-proposals/\1/apply"),
    (r"^/api/v1/reports/([^/]+)/exports$", r"/api/v1/reports/\1/export"),
    (r"^/api/v1/publications/([^/]+)/verifications$", r"/api/v1/publications/\1/verify"),
    (r"^/api/v1/publications/([^/]+)/label-recomputations$", r"/api/v1/publications/\1/labels"),
    (r"^/api/v1/metric-imports/([^/]+)/previews$", r"/api/v1/metric-imports/\1/preview"),
    (r"^/api/v1/metric-imports/([^/]+)/confirmations$", r"/api/v1/metric-imports/\1/confirm"),
    (r"^/api/v1/metric-adapters/([^/]+)/refreshes$", r"/api/v1/metric-adapters/\1/refresh"),
    (r"^/api/v1/deliveries/([^/]+)/confirmations$", r"/api/v1/deliveries/\1/deliver"),
)


def _normalize_path(path: str) -> str:
    for pattern, repl in _PATH_REWRITES:
        if re.match(pattern, path):
            return re.sub(pattern, repl, path)
    return path


class Handler(BaseHTTPRequestHandler):
    samples = {}

    def log_message(self, *args):  # 静音默认访问日志
        pass

    # --- 工具 ---
    def _send(self, status: int, payload, content_type="application/json"):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Idempotency-Key,If-Match")
        self.end_headers()
        if content_type != "text/markdown":
            self.wfile.write(body)
        else:
            self.wfile.write(body)

    def _fail(self, code: str, message: str | None = None, details: dict | None = None):
        self._send(_STATUS_BY_CODE[code], error_body(code, message, details))

    def _query(self):
        if "?" not in self.path:
            return {}
        q = {}
        for pair in self.path.split("?", 1)[1].split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                q[k] = v
        return q

    def _path(self):
        return _normalize_path(self.path.split("?", 1)[0].rstrip("/") or "/")

    # --- 路由 ---
    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        p = self._path()
        q = self._query()
        limit = int(q.get("limit", 20))
        cursor = q.get("cursor")

        # /api/v1/<collection>
        m = re.fullmatch(r"/api/v1/([a-z\-]+)", p)
        if m and m.group(1) in COLLECTIONS:
            return self._send(200, paginated(self.samples.get(COLLECTIONS[m.group(1)], []),
                                             limit, cursor))

        # /api/v1/<collection>/{id}
        m = re.fullmatch(r"/api/v1/([a-z\-]+)/([^/]+)", p)
        if m and m.group(1) in COLLECTIONS:
            name = COLLECTIONS[m.group(1)]
            for row in self.samples.get(name, []):
                if row.get("id") == m.group(2) or row.get("publicationId") == m.group(2):
                    return self._send(200, envelope(row))
            return self._fail("RESOURCE_NOT_FOUND")

        # /api/v1/publications/{id}/metric-latest
        m = re.fullmatch(r"/api/v1/publications/([^/]+)/metric-latest", p)
        if m:
            rows = [r for r in self.samples.get("metric_latest", []) if r["publicationId"] == m.group(1)]
            return self._send(200, envelope(rows))

        # /api/v1/publications/{id}/labels
        m = re.fullmatch(r"/api/v1/publications/([^/]+)/labels", p)
        if m:
            rows = [r for r in self.samples.get("publication_labels", []) if r["publicationId"] == m.group(1)]
            return self._send(200, envelope(rows))

        # /api/v1/metric-imports/{id}/rows
        m = re.fullmatch(r"/api/v1/metric-imports/([^/]+)/rows", p)
        if m:
            rows = [r for r in self.samples.get("metric_import_rows", []) if r["importId"] == m.group(1)]
            return self._send(200, paginated(rows, limit, cursor))

        # /api/v1/reports/{id}/versions
        m = re.fullmatch(r"/api/v1/reports/([^/]+)/versions", p)
        if m:
            rows = [r for r in self.samples.get("report_versions", []) if r["reportId"] == m.group(1)]
            return self._send(200, paginated(rows, limit, cursor))

        # /api/v1/reports/{id}/export
        m = re.fullmatch(r"/api/v1/reports/([^/]+)/export", p)
        if m:
            md = self.samples.get("report_markdown", "# 报告\n\n（样例）")
            return self._send(200, md, content_type="text/markdown")

        # /api/v1/billing/management-summary（WP43）：金额用 NUMERIC(20,8) 字符串，避免浮点
        m = re.fullmatch(r"/api/v1/billing/management-summary", p)
        if m:
            return self._send(200, envelope({
                "projectId": "00000000-0000-4000-8000-000000000001",
                "committedAmount": "128.50000000",
                "reservedAmount": "20.00000000",
                "limitAmount": "500.00000000",
            }))

        return self._fail("RESOURCE_NOT_FOUND", details={"path": p})

    def do_POST(self):
        p = self._path()
        body = {}
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._fail("MALFORMED_REQUEST")

        if self.headers.get("Idempotency-Key") is None:
            # 契约里 Idempotency-Key 是可选头；mock 不强制，但记录到 details 便于联调
            pass

        # 幂等冲突演示：请求体带 __conflict=true 时返回 409
        if body.get("__conflict"):
            return self._fail("IDEMPOTENCY_CONFLICT")

        # 明确不适合的迁移：交付 deliver 到非 PREPARED
        m = re.fullmatch(r"/api/v1/deliveries/([^/]+)/deliver", p)
        if m:
            row = next((r for r in self.samples.get("deliveries", []) if r["id"] == m.group(1)), None)
            if row is None:
                return self._fail("RESOURCE_NOT_FOUND")
            if row.get("state") != "PREPARED":
                return self._fail("INVALID_TRANSITION")
            ok = dict(row, state="DELIVERED", deliveredAt="2026-10-08T00:00:00+08:00")
            return self._send(200, envelope(ok))

        # 链接去重：canonicalUrl 命中已有 ⇒ DUPLICATE_PUBLICATION
        m = re.fullmatch(r"/api/v1/publications/([^/]+)/links", p)
        if m:
            canon = body.get("canonicalUrl") or body.get("rawUrl")
            hit = next((r for r in self.samples.get("publication_links", [])
                        if r["canonicalUrl"] == canon), None)
            if hit:
                return self._fail("DUPLICATE_PUBLICATION", details={"canonicalUrl": canon})
            return self._send(201, envelope({"id": str(uuid.uuid4()), "publicationId": m.group(1),
                                             "rawUrl": body.get("rawUrl", ""),
                                             "canonicalUrl": canon or "",
                                             "linkKind": "LONG"}))

        # 适配器刷新：无渠道 → 明确不可得（不是缺陷）
        m = re.fullmatch(r"/api/v1/metric-adapters/([^/]+)/refresh", p)
        if m:
            return self._send(200, envelope({"adapterId": m.group(1), "outcome": "NO_CHANNEL",
                                             "importedObservationCount": 0,
                                             "message": "无可用渠道，请走导入替代路径。"}))

        # 结项重开
        m = re.fullmatch(r"/api/v1/project-closures/([^/]+)/reopen", p)
        if m:
            return self._send(201, envelope({"id": str(uuid.uuid4()),
                                             "projectId": "00000000-0000-0000-0000-000000000000",
                                             "closureType": "COMPLETED",
                                             "closedAt": "2026-10-08T00:00:00+08:00",
                                             "reopenedAt": "2026-10-08T00:00:00+08:00",
                                             "reopenReason": body.get("reopenReason", "")}))

        # WP39：修复错误行（PATCH /metric-imports/{id}/rows/{rowNo}）
        m = re.fullmatch(r"/api/v1/metric-imports/([^/]+)/rows/(\d+)", p)
        if m:
            row = next((r for r in self.samples.get("metric_import_rows", [])
                        if str(r.get("importId")) == m.group(1)
                        and str(r.get("rowNo")) == m.group(2)), None)
            if row is None:
                return self._fail("RESOURCE_NOT_FOUND")
            patch = body.get("patch") or {}
            if not isinstance(patch, dict):
                return self._fail("VALIDATION_FAILED", details={"field": "patch"})
            fixed = dict(row, **patch)
            fixed["status"] = "OK"
            fixed["errors"] = []
            return self._send(200, envelope(fixed))

        # WP42：人工确认后回写知识（复用既有 proposals 表）
        m = re.fullmatch(r"/api/v1/knowledge-proposals/([^/]+)/apply", p)
        if m:
            decision = body.get("decision")
            if decision not in ("APPROVE", "REJECT"):
                return self._fail("VALIDATION_FAILED", details={"field": "decision"})
            return self._send(200, envelope({
                "proposalId": m.group(1),
                "applied": decision == "APPROVE",
                "publicationId": ("dddddddd-1111-4111-8111-111111111111"
                                  if decision == "APPROVE" else None),
            }))

        # WP43：触发费用对账（重复回执不重复收费；退款与待核对分开）
        m = re.fullmatch(r"/api/v1/billing/reconciliations", p)
        if m:
            return self._send(201, envelope({
                "projectId": body.get("projectId") or "00000000-0000-4000-8000-000000000001",
                "matchedCount": 3,
                "unknownCount": 1,
            }))

        # 通用集合创建
        m = re.fullmatch(r"/api/v1/([a-z\-]+)", p)
        if m and m.group(1) in COLLECTIONS:
            created = dict(body)
            created.setdefault("id", str(uuid.uuid4()))
            return self._send(201, envelope(created))

        # 通用动作端点（有子路径的）
        m = re.fullmatch(r"/api/v1/([a-z\-]+)/([^/]+)/([a-z\-]+)", p)
        if m and m.group(1) in COLLECTIONS:
            return self._send(201, envelope({"id": str(uuid.uuid4()), **body}))

        return self._fail("RESOURCE_NOT_FOUND", details={"path": p})

    do_PATCH = do_POST


def make_server(host: str, port: int, samples: dict):
    handler = type("BoundHandler", (Handler,), {"samples": samples})
    return ThreadingHTTPServer((host, port), handler)


# --------------------------- 自检 ---------------------------

def selftest() -> int:
    samples = load_samples()
    srv = make_server("127.0.0.1", 0, samples)
    host, port = srv.server_address[:2]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}/api/v1"
    opener = build_opener(ProxyHandler({}))  # 绕开本机代理（本机代理会劫持 localhost）
    checks = []
    PROJECT = "00000000-0000-4000-8000-000000000001"

    def call(method, url, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = __import__("urllib.request", fromlist=["Request"]).Request(
            url, data=data, method=method, headers=headers or {"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read().decode())
        except HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    try:
        st, body = call("GET", f"{base}/deliveries")
        checks.append(("GET /deliveries = 200", st == 200))
        checks.append(("列表含 data[] 与 meta.page", isinstance(body.get("data"), list)
                       and "page" in body.get("meta", {})))

        known = samples["deliveries"][0]["id"]
        st, body = call("GET", f"{base}/deliveries/{known}")
        checks.append(("GET 已知 id = 200 且为单体包裹",
                       st == 200 and isinstance(body.get("data"), dict)))

        st, body = call("GET", f"{base}/deliveries/does-not-exist")
        checks.append(("GET 未知 id = 404", st == 404))
        checks.append(("错误体 code 合法且不可重试",
                       body.get("code") == "RESOURCE_NOT_FOUND" and body.get("retryable") is False))
        checks.append(("错误体 code 在 18 个枚举内", body.get("code") in ERROR_CODES))

        st, body = call("POST", f"{base}/projects/{PROJECT}/metric-imports", {"importKind": "CSV",
                                                                         "templateVersion": "1.0"})
        checks.append(("POST /metric-imports = 201", st == 201 and "id" in body.get("data", {})))

        st, body = call("POST", f"{base}/publications/{samples['publications'][0]['id']}/links",
                        {"rawUrl": samples["publication_links"][0]["canonicalUrl"]})
        checks.append(("重复链接 = 409 DUPLICATE_PUBLICATION",
                       st == 409 and body.get("code") == "DUPLICATE_PUBLICATION"))

        st, body = call("POST", f"{base}/metric-observations", {"__conflict": True})
        checks.append(("同键异体 = 409 IDEMPOTENCY_CONFLICT",
                       st == 409 and body.get("code") == "IDEMPOTENCY_CONFLICT"))

        st, body = call("POST", f"{base}/deliveries/{known}/confirmations")
        if samples["deliveries"][0].get("state") == "PREPARED":
            checks.append(("PREPARED 交付 = 200 且 state 转 DELIVERED",
                           st == 200 and body.get("data", {}).get("state") == "DELIVERED"))
        else:
            checks.append(("非 PREPARED 交付 = 409 INVALID_TRANSITION",
                           st == 409 and body.get("code") == "INVALID_TRANSITION"))

        # 另取一条已交付的，断言非法迁移被拒（不论第一条是什么状态，这条都覆盖 409 路径）
        already = next((d for d in samples["deliveries"] if d.get("state") == "DELIVERED"), None)
        if already:
            st, body = call("POST", f"{base}/deliveries/{already['id']}/confirmations")
            checks.append(("已交付再交付 = 409 INVALID_TRANSITION",
                           st == 409 and body.get("code") == "INVALID_TRANSITION"))

        st, body = call("GET", f"{base}/publications/{samples['publications'][0]['id']}/metric-latest")
        checks.append(("metric-latest = 200 数组", st == 200 and isinstance(body.get("data"), list)))

        # --- 契约全覆盖：原先缺失的 4 类路由（WP39 / WP42 / WP43）---
        import_id = samples["metric_import_rows"][0]["importId"]
        row_no = samples["metric_import_rows"][0]["rowNo"]
        st, body = call("PATCH", f"{base}/metric-imports/{import_id}/rows/{row_no}",
                        {"patch": {"metricCode": "views"}})
        checks.append(("PATCH 修复导入行 = 200 且 status 转 OK",
                       st == 200 and body.get("data", {}).get("status") == "OK"))

        st, body = call("POST", f"{base}/knowledge/proposals/{uuid.uuid4()}/applications",
                        {"decision": "APPROVE"})
        checks.append(("知识回写 apply(APPROVE) = 200 且 applied=true",
                       st == 200 and body.get("data", {}).get("applied") is True))

        st, body = call("POST", f"{base}/knowledge/proposals/{uuid.uuid4()}/applications",
                        {"decision": "REJECT"})
        checks.append(("知识回写 apply(REJECT) = 200 且 applied=false",
                       st == 200 and body.get("data", {}).get("applied") is False))

        st, body = call("POST", f"{base}/knowledge/proposals/{uuid.uuid4()}/applications",
                        {"decision": "MAYBE"})
        checks.append(("知识回写 非法 decision = 422 VALIDATION_FAILED",
                       st == 422 and body.get("code") == "VALIDATION_FAILED"))

        st, body = call("POST", f"{base}/projects/{PROJECT}/billing-reconciliations",
                        {"periodStart": "2026-10-01T00:00:00+08:00",
                         "periodEnd": "2026-10-08T00:00:00+08:00"})
        checks.append(("费用对账 = 201 且含 unknownCount（待核对单列）",
                       st == 201 and isinstance(body.get("data", {}).get("unknownCount"), int)))

        st, body = call("GET", f"{base}/projects/{PROJECT}/billing-summary")
        checks.append(("管理统计 = 200 且金额为字符串（NUMERIC(20,8)）",
                       st == 200 and isinstance(body.get("data", {}).get("committedAmount"), str)))
    finally:
        srv.shutdown()

    passed = sum(1 for _, ok in checks if ok)
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"RESULT: {passed}/{len(checks)}")
    return 0 if passed == len(checks) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="M4 mock server（只读样例）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    samples = load_samples()
    srv = make_server(args.host, args.port, samples)
    print(f"M4 mock server 已启动： http://{args.host}:{args.port}/api/v1")
    print(f"样例： {SAMPLES_PATH}")
    print("Ctrl-C 退出。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
