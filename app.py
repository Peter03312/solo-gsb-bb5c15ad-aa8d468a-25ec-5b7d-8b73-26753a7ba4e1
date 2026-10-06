#!/usr/bin/env python3
"""博物馆借展运输环境监控 — 应用入口（仅依赖 Python 标准库）。

页面：批次风险摘要、批次详情、版本快照、版本差异。
API：/api 前缀，JSON 交互。
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlparse

from museum import db, pages, service
from museum.riskgroups import find_risk_groups
from museum.web import Response, Router, json_response, redirect

router = Router()


def _int(value, name="id"):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise service.ValidationError(f"{name} 必须为整数: {value!r}")


# ================================================================ 页面

@router.route("GET", "/")
def home(ctx):
    return Response(pages.home(service.list_batches(ctx.conn),
                               ctx.query.get("msg"), ctx.query.get("err")))


@router.route("POST", "/batches")
def create_batch(ctx):
    try:
        b = service.create_batch(ctx.conn, ctx.body.get("name"),
                                 ctx.body.get("description", ""))
    except service.ValidationError as e:
        return redirect("/?err=" + quote(e.message))
    return redirect(f"/batches/{b['id']}?msg=" + quote("批次已创建"))


@router.route("GET", "/batches/<bid>")
def batch_page(ctx):
    bid = _int(ctx.params["bid"], "批次 id")
    batch = service.get_batch(ctx.conn, bid)
    summary = service.batch_summary(ctx.conn, bid)
    cur = service.current_version(ctx.conn, bid)
    events, gaps, risk_groups = [], [], []
    if cur:
        names = {a["id"]: a["name"] for a in service.list_artifacts(ctx.conn, bid)}
        events = service.version_events(ctx.conn, cur["id"], names)
        gaps = service.version_gaps(ctx.conn, cur["id"], names)
        risk_groups = find_risk_groups(events)
    return Response(pages.batch_detail(
        batch, service.list_nodes(ctx.conn, bid), service.list_artifacts(ctx.conn, bid),
        summary, events, gaps, risk_groups, service.list_versions(ctx.conn, bid),
        ctx.query.get("msg"), ctx.query.get("err")))


@router.route("POST", "/batches/<bid>/nodes")
def add_node(ctx):
    bid = _int(ctx.params["bid"], "批次 id")
    try:
        service.add_node(ctx.conn, bid, ctx.body.get("name"), ctx.body.get("seq"),
                         ctx.body.get("location", ""))
    except service.ValidationError as e:
        return redirect(f"/batches/{bid}?err=" + quote(e.message))
    return redirect(f"/batches/{bid}?msg=" + quote("节点已添加"))


@router.route("POST", "/batches/<bid>/artifacts")
def add_artifact(ctx):
    bid = _int(ctx.params["bid"], "批次 id")
    try:
        service.add_artifact(ctx.conn, bid, ctx.body.get("name"),
                             ctx.body.get("temp_min"), ctx.body.get("temp_max"),
                             ctx.body.get("hum_min"), ctx.body.get("hum_max"),
                             ctx.body.get("sample_interval"))
    except service.ValidationError as e:
        msg = e.message + "：" + "；".join(e.details) if e.details else e.message
        return redirect(f"/batches/{bid}?err=" + quote(msg))
    return redirect(f"/batches/{bid}?msg=" + quote("文物已登记"))


@router.route("POST", "/batches/<bid>/readings")
def submit_reading(ctx):
    bid = _int(ctx.params["bid"], "批次 id")
    item = {k: ctx.body.get(k) for k in
            ("artifact_id", "node_id", "ts", "temperature", "humidity")}
    try:
        result = service.submit_readings(ctx.conn, bid, [item], ctx.body.get("note", ""))
    except service.ValidationError as e:
        reasons = "；".join(d["reason"] for d in e.details) or e.message
        return redirect(f"/batches/{bid}?err=" + quote(f"{e.message}：{reasons}"))
    return redirect(f"/batches/{bid}?msg=" + quote(
        f"读数已保存，生成评估版本 v{result['version']}"))


@router.route("POST", "/events/<eid>/close")
def close_event(ctx):
    eid = _int(ctx.params["eid"], "事件 id")
    nxt = ctx.body.get("next") or "/"
    try:
        service.close_event(ctx.conn, eid, ctx.body.get("note"),
                            ctx.body.get("evidence_no"))
    except (service.ValidationError, service.NotFoundError) as e:
        return redirect(nxt + ("&" if "?" in nxt else "?") + "err=" + quote(e.message))
    return redirect(nxt + ("&" if "?" in nxt else "?") + "msg=" + quote("事件已关闭"))


@router.route("GET", "/batches/<bid>/versions/<vno>")
def version_page(ctx):
    bid = _int(ctx.params["bid"], "批次 id")
    vno = _int(ctx.params["vno"], "版本号")
    batch = service.get_batch(ctx.conn, bid)
    detail = service.version_detail(ctx.conn, bid, vno)
    return Response(pages.version_page(batch, detail))


@router.route("GET", "/batches/<bid>/diff")
def diff_page(ctx):
    bid = _int(ctx.params["bid"], "批次 id")
    from_no = _int(ctx.query.get("from"), "from")
    to_no = _int(ctx.query.get("to"), "to")
    batch = service.get_batch(ctx.conn, bid)
    diff = service.version_diff(ctx.conn, bid, from_no, to_no)
    return Response(pages.diff_page(batch, diff))


# ================================================================ API

@router.route("GET", "/api/health")
def api_health(ctx):
    return json_response({"status": "ok"})


@router.route("POST", "/api/batches")
def api_create_batch(ctx):
    b = service.create_batch(ctx.conn, ctx.body.get("name"),
                             ctx.body.get("description", ""))
    return json_response(b, 201)


@router.route("GET", "/api/batches")
def api_list_batches(ctx):
    return json_response(service.list_batches(ctx.conn))


@router.route("GET", "/api/batches/<bid>")
def api_batch_detail(ctx):
    bid = _int(ctx.params["bid"], "批次 id")
    batch = dict(service.get_batch(ctx.conn, bid))
    return json_response({
        "batch": batch,
        "nodes": [dict(n) for n in service.list_nodes(ctx.conn, bid)],
        "artifacts": [dict(a) for a in service.list_artifacts(ctx.conn, bid)],
        "summary": service.batch_summary(ctx.conn, bid),
    })


@router.route("POST", "/api/batches/<bid>/nodes")
def api_add_node(ctx):
    bid = _int(ctx.params["bid"], "批次 id")
    node = service.add_node(ctx.conn, bid, ctx.body.get("name"), ctx.body.get("seq"),
                            ctx.body.get("location", ""))
    return json_response(node, 201)


@router.route("POST", "/api/batches/<bid>/artifacts")
def api_add_artifact(ctx):
    bid = _int(ctx.params["bid"], "批次 id")
    art = service.add_artifact(ctx.conn, bid, ctx.body.get("name"),
                               ctx.body.get("temp_min"), ctx.body.get("temp_max"),
                               ctx.body.get("hum_min"), ctx.body.get("hum_max"),
                               ctx.body.get("sample_interval"))
    return json_response(art, 201)


@router.route("POST", "/api/batches/<bid>/readings")
def api_submit_readings(ctx):
    bid = _int(ctx.params["bid"], "批次 id")
    body = ctx.body
    if isinstance(body, list):
        items, note = body, ""
    else:
        items, note = body.get("readings"), body.get("note", "")
    result = service.submit_readings(ctx.conn, bid, items, note)
    return json_response(result, 201)


@router.route("GET", "/api/batches/<bid>/summary")
def api_summary(ctx):
    bid = _int(ctx.params["bid"], "批次 id")
    return json_response(service.batch_summary(ctx.conn, bid))


@router.route("GET", "/api/batches/<bid>/events")
def api_events(ctx):
    bid = _int(ctx.params["bid"], "批次 id")
    service.get_batch(ctx.conn, bid)
    vno = ctx.query.get("version")
    if vno:
        version = service.get_version(ctx.conn, bid, _int(vno, "版本号"))
    else:
        version = service.current_version(ctx.conn, bid)
        if not version:
            return json_response([])
    names = {a["id"]: a["name"] for a in service.list_artifacts(ctx.conn, bid)}
    events = service.version_events(ctx.conn, version["id"], names)
    status = ctx.query.get("status")
    if status:
        events = [e for e in events if e["status"] == status]
    return json_response(events)


@router.route("GET", "/api/batches/<bid>/versions")
def api_versions(ctx):
    bid = _int(ctx.params["bid"], "批次 id")
    service.get_batch(ctx.conn, bid)
    return json_response([dict(v) for v in service.list_versions(ctx.conn, bid)])


@router.route("GET", "/api/batches/<bid>/versions/<vno>")
def api_version_detail(ctx):
    bid = _int(ctx.params["bid"], "批次 id")
    vno = _int(ctx.params["vno"], "版本号")
    return json_response(service.version_detail(ctx.conn, bid, vno))


@router.route("GET", "/api/batches/<bid>/versions/<vno>/risk-groups")
def api_version_risk_groups(ctx):
    bid = _int(ctx.params["bid"], "批次 id")
    vno = _int(ctx.params["vno"], "版本号")
    return json_response(service.version_risk_groups(ctx.conn, bid, vno))


@router.route("GET", "/api/batches/<bid>/risk-groups")
def api_risk_groups(ctx):
    bid = _int(ctx.params["bid"], "批次 id")
    vno = ctx.query.get("version")
    if vno:
        vno = _int(vno, "版本号")
    else:
        vno = None
    return json_response(service.version_risk_groups(ctx.conn, bid, vno))


@router.route("GET", "/api/batches/<bid>/diff")
def api_diff(ctx):
    bid = _int(ctx.params["bid"], "批次 id")
    from_no = _int(ctx.query.get("from"), "from")
    to_no = _int(ctx.query.get("to"), "to")
    return json_response(service.version_diff(ctx.conn, bid, from_no, to_no))


@router.route("POST", "/api/events/<eid>/close")
def api_close_event(ctx):
    eid = _int(ctx.params["eid"], "事件 id")
    event = service.close_event(ctx.conn, eid, ctx.body.get("note"),
                                ctx.body.get("evidence_no"))
    return json_response(event)


# ================================================================ 服务器

def make_server(host, port, db_path):
    db.init_db(db_path)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            ctype = self.headers.get("Content-Type", "")
            if "application/json" in ctype:
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    return self._send(json_response(
                        {"error": "请求体不是合法 JSON"}, 400))
            else:
                body = {k: v[0] for k, v in
                        parse_qs(raw.decode("utf-8", "replace")).items()}

            fn, params = router.dispatch(self.command, path)
            if fn is None:
                return self._send(self._error(path, 404, f"路径不存在: {self.command} {path}"))
            conn = db.connect(self.server.db_path)
            try:
                ctx = SimpleNamespace(conn=conn, params=params, query=query, body=body)
                resp = fn(ctx)
            except service.ValidationError as e:
                resp = self._error(path, 400, e.message, e.details)
            except service.NotFoundError as e:
                resp = self._error(path, 404, e.message)
            except Exception as e:  # noqa: BLE001
                resp = self._error(path, 500, f"服务器内部错误: {e}")
            finally:
                conn.close()
            self._send(resp)

        def _error(self, path, status, message, details=None):
            if path.startswith("/api"):
                payload = {"error": message}
                if details:
                    payload["details"] = details
                return json_response(payload, status)
            return Response(pages.error_page(status, message), status)

        def _send(self, resp):
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Content-Length", str(len(resp.body)))
            for k, v in resp.headers.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(resp.body)

        def log_message(self, fmt, *args):
            print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")

        do_GET = _handle
        do_POST = _handle

    server = ThreadingHTTPServer((host, port), Handler)
    server.db_path = db_path
    return server


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    db_path = os.environ.get("DB_PATH", "museum.db")
    server = make_server(host, port, db_path)
    print(f"博物馆借展运输环境监控已启动: http://{host}:{port}  数据库: {db_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
