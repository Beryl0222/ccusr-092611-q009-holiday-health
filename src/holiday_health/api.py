"""假期健康提醒编排的轻量 HTTP 边界。

保留旧版通用记录接口（``GET /records/{id}``），并提供旅行健康编排
接口。所有写接口都要求 JSON 请求体中带 ``request_key`` 以保证重复
提交幂等；写接口的 ``operator_id`` 用于鉴权。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .service import DomainStore, ServiceError
from .travel import Clock, TravelHealthError, TravelHealthService


def _json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class Handler(BaseHTTPRequestHandler):
    store = DomainStore()
    travel_store = None
    # 多线程共享同一连接：事务由 BEGIN IMMEDIATE 串行化，
    # 另用进程内锁避免内存库上的并发竞争。
    tx_lock = threading.RLock()

    # ------------------------------------------------------------------
    def _reply(self, code, body):
        data = json.dumps(body, ensure_ascii=False,
                          default=_json_default).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            raise TravelHealthError("请求体必须是 JSON")

    # ------------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = parse_qs(parsed.query)
        try:
            with self.tx_lock:
                self._route_get(parts, query)
        except (ServiceError, TravelHealthError) as exc:
            code = 404 if "不存在" in str(exc) or "未知" in str(exc) else 400
            self._reply(code, {"error": str(exc)})

    def do_POST(self):
        self._write_route("POST")

    def do_PUT(self):
        self._write_route("PUT")

    def do_DELETE(self):
        self._write_route("DELETE")

    def _write_route(self, method):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = parse_qs(parsed.query)
        try:
            body = self._body()
            with self.tx_lock:
                self._route_write(method, parts, query, body)
        except (ServiceError, TravelHealthError, ValueError) as exc:
            self._reply(400, {"error": str(exc)})

    # ------------------------------------------------------------------
    # GET 路由
    # ------------------------------------------------------------------
    def _route_get(self, parts, query):
        svc = self.travel_store
        if len(parts) == 2 and parts[0] == "records":
            self._reply(200, self.store.get(parts[1]).__dict__)
            return
        if parts and parts[0] == "plans":
            if len(parts) == 2:
                self._reply(200, svc.plan_summary(parts[1]))
                return
            plan_id = parts[1]
            resource = parts[2]
            if resource == "events":
                self._reply(200, svc.plan_events(plan_id))
            elif resource == "members" and len(parts) == 3:
                self._reply(200, svc.list_members(plan_id))
            elif resource == "risk-windows" and len(parts) == 3:
                self._reply(200, svc.list_risk_windows(plan_id))
            elif resource == "measures":
                self._reply(200, svc.list_measures(
                    plan_id,
                    member_id=query.get("member_id", [None])[0],
                    status=query.get("status", [None])[0],
                    advice_class=query.get("advice_class", [None])[0]))
            elif resource == "reminders":
                self._reply(200, svc.reminder_history(
                    plan_id, query.get("member_id", [None])[0]))
            elif resource == "symptoms":
                self._reply(200, svc.list_symptom_reports(
                    plan_id, query.get("member_id", [None])[0]))
            else:
                self._reply(404, {"error": "未知路径"})
            return
        if parts and parts[0] == "escalations":
            if len(parts) == 1:
                self._reply(200, svc.list_escalations(
                    plan_id=query.get("plan_id", [None])[0],
                    status=query.get("status", [None])[0]))
            else:
                self._reply(200, svc.get_escalation(parts[1]))
            return
        self._reply(404, {"error": "未知路径"})

    # ------------------------------------------------------------------
    # 写路由
    # ------------------------------------------------------------------
    def _route_write(self, method, parts, query, body):
        svc = self.travel_store
        key = body.get("request_key")

        if method == "POST" and parts == ["plans"]:
            self._reply(200, svc.create_plan(
                body["plan_id"], body["operator_id"],
                body["destination"], body["destination_tz"],
                body["home_tz"], body["depart_date"], body["return_date"],
                key, payload=body.get("payload")))
            return

        if len(parts) >= 3 and parts[0] == "plans":
            plan_id = parts[1]
            resource = parts[2]

            if method == "POST" and resource == "activate" and len(parts) == 3:
                self._reply(200, svc.activate_plan(
                    plan_id, body["operator_id"], key,
                    expected_version=body.get("expected_version")))
                return
            if method == "POST" and resource == "cancel" and len(parts) == 3:
                self._reply(200, svc.cancel_plan(
                    plan_id, body["operator_id"], key,
                    expected_version=body.get("expected_version"),
                    reason=body.get("reason")))
                return
            if method == "POST" and resource == "sync" and len(parts) == 3:
                self._reply(200, svc.sync_measures(
                    plan_id, only_window=body.get("only_window"),
                    reason=body.get("reason", "http")))
                return
            if resource == "members" and len(parts) >= 4:
                member_id = parts[3]
                if method == "PUT" and len(parts) == 4:
                    self._reply(200, svc.upsert_member(
                        plan_id, member_id, body["category"], body["name"],
                        body["consent_status"], body["operator_id"], key,
                        payload=body.get("payload"),
                        expected_version=body.get("expected_version")))
                    return
                if method == "POST" and len(parts) == 5:
                    sub = parts[4]
                    if sub == "complete":
                        self._reply(200, svc.complete_measure(
                            plan_id, member_id, body["kind"],
                            body["scheduled_date"], key,
                            disease=body.get("disease"),
                            note=body.get("note")))
                        return
                    if sub == "symptoms":
                        self._reply(200, svc.report_symptoms(
                            plan_id, member_id, body["symptoms"], key,
                            reported_at=body.get("reported_at"),
                            payload=body.get("payload")))
                        return
            if resource == "risk-windows" and len(parts) == 4:
                window_id = parts[3]
                if method == "PUT":
                    self._reply(200, svc.upsert_risk_window(
                        plan_id, window_id, body["region"],
                        body["start_date"], body["end_date"],
                        body["risk_level"], body.get("diseases", []),
                        body["operator_id"], key,
                        expected_version=body.get("expected_version")))
                    return
                if method == "DELETE":
                    self._reply(200, svc.remove_risk_window(
                        plan_id, window_id, body["operator_id"], key))
                    return

        if method == "POST" and parts == ["reminders", "due"]:
            self._reply(200, svc.due_reminders(
                mark_sent=query.get("send", ["0"])[0] in ("1", "true")))
            return
        if method == "POST" and parts == ["escalations", "scan"]:
            self._reply(200, svc.pending_escalations())
            return
        if (method == "POST" and len(parts) == 3
                and parts[0] == "escalations" and parts[2] == "resolve"):
            self._reply(200, svc.resolve_escalation(
                parts[1], body["operator_id"], key,
                resolution=body.get("resolution")))
            return

        self._reply(404, {"error": "未知路径"})

    def log_message(self, *_):
        return


def serve(host="127.0.0.1", port=8080, database=":memory:", clock=None):
    """启动 HTTP 服务；``database`` 指向文件时可跨重启恢复全部状态。"""
    Handler.travel_store = TravelHealthService(
        database=database, clock=clock or Clock(), check_same_thread=False)
    server = ThreadingHTTPServer((host, port), Handler)
    server.serve_forever()
    return server
