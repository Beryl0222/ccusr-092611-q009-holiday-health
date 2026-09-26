"""假期健康提醒编排的轻量 HTTP 边界（仅标准库）。

所有写接口都接受可选 ``request_key`` 实现幂等；存储默认使用 SQLite 文件
（环境变量 ``HOLIDAY_HEALTH_DB``），因此服务重启后历史提醒、待处理升级
与未完成事项仍然可查、可继续执行。
"""
from __future__ import annotations

import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .domain import SystemClock
from .service import ConflictError, ServiceError, TravelHealthService


def build_store(database: str | None = None) -> TravelHealthService:
    database = database or os.environ.get("HOLIDAY_HEALTH_DB", "holiday_health.db")
    return TravelHealthService(database=database, clock=SystemClock())


class Handler(BaseHTTPRequestHandler):
    store: TravelHealthService = None  # type: ignore[assignment]
    lock = threading.Lock()

    # -- 工具 ------------------------------------------------------------

    def _reply(self, code: int, body) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _query(self) -> dict:
        parsed = urllib.parse.urlparse(self.path)
        return {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}

    def log_message(self, *_args) -> None:  # 静默访问日志
        return

    # -- 路由 ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        query = self._query()
        try:
            with self.lock:
                if path == "/health":
                    self._reply(200, {"ok": True})
                elif path.startswith("/plans/") and path.count("/") == 2:
                    self._reply(200, self.store.get_plan(path.split("/")[2]))
                elif path.startswith("/plans/") and path.endswith("/members"):
                    plan_id = path.split("/")[2]
                    self._reply(200, self.store.list_members(plan_id))
                elif path.startswith("/plans/") and path.endswith("/measures"):
                    plan_id = path.split("/")[2]
                    self._reply(200, self.store.list_measures(
                        plan_id, member_id=query.get("member_id"),
                        include_superseded=query.get("include_superseded") == "1"))
                elif path.startswith("/risk/"):
                    self._reply(200, self.store.list_risk_windows(
                        urllib.parse.unquote(path.split("/", 2)[2])))
                elif path.startswith("/members/") and path.endswith("/consent"):
                    member_id = path.split("/")[2]
                    self._reply(200, self.store.get_consent(member_id))
                elif path.startswith("/members/") and path.endswith("/symptoms"):
                    member_id = path.split("/")[2]
                    self._reply(200, self.store.list_symptoms(member_id))
                elif path == "/escalations":
                    self._reply(200, self.store.list_escalations(query.get("status")))
                elif path == "/reminders/pending":
                    self._reply(200, self.store.pending_reminders(query.get("plan_id")))
                elif path == "/events":
                    kinds = query.get("kinds").split(",") if query.get("kinds") else None
                    self._reply(200, self.store.history(
                        query.get("aggregate_id"), kinds=kinds))
                elif path == "/handoff":
                    self._reply(200, self.store.duty_handoff())
                else:
                    self._reply(404, {"error": "未知路径"})
        except ServiceError as exc:
            self._reply(400, {"error": str(exc)})
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._reply(400, {"error": f"请求参数有误: {exc}"})

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]
        try:
            body = self._body()
            with self.lock:
                code, response = self._route_post(parts, body)
                self._reply(code, response)
        except ConflictError as exc:
            self._reply(409, {"error": str(exc)})
        except ServiceError as exc:
            self._reply(400, {"error": str(exc)})
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._reply(400, {"error": f"请求参数有误: {exc}"})

    def _route_post(self, parts: list[str], b: dict) -> tuple[int, dict]:
        key = b.get("request_key")
        # /plans
        if parts == ["plans"]:
            return 201, self.store.create_plan(
                b["owner_id"], b["destination"], b["dest_timezone"],
                b["depart_date"], b["return_date"], request_key=key,
                payload=b.get("payload"))
        # /plans/{id}/cancel | /dates | /members | /sync
        if len(parts) == 3 and parts[0] == "plans":
            plan_id, action = parts[1], parts[2]
            if action == "cancel":
                return 200, self.store.cancel_plan(
                    plan_id, b["owner_id"], request_key=key,
                    expected_version=b.get("expected_version"))
            if action == "dates":
                return 200, self.store.update_plan_dates(
                    plan_id, b["owner_id"], depart_date=b.get("depart_date"),
                    return_date=b.get("return_date"), request_key=key,
                    expected_version=b.get("expected_version"))
            if action == "members":
                return 201, self.store.add_member(
                    plan_id, b["owner_id"], b["name"], b["birth_date"],
                    b["relationship"], request_key=key)
            if action == "sync":
                return 200, self.store.sync_measures(
                    plan_id, b["actor_id"], request_key=key)
        # /risk/{destination}
        if len(parts) == 2 and parts[0] == "risk":
            destination = urllib.parse.unquote(parts[1])
            return 200, self.store.upsert_risk_window(
                destination, b["start_date"], b["end_date"], b["level"],
                actor_id=b.get("actor_id"), request_key=key)
        # /members/{id}/consent | consent/revoke | symptoms
        if len(parts) >= 3 and parts[0] == "members":
            member_id = parts[1]
            if parts[2] == "consent" and len(parts) == 3:
                return 200, self.store.grant_consent(
                    member_id, b["granted_by"], b.get("scope", "health_measures"),
                    request_key=key)
            if parts[2] == "consent" and len(parts) == 4 and parts[3] == "revoke":
                return 200, self.store.revoke_consent(
                    member_id, b["granted_by"], b.get("scope", "health_measures"),
                    request_key=key)
            if parts[2] == "symptoms" and len(parts) == 3:
                return 201, self.store.report_symptoms(
                    member_id, b["symptoms"], b["occurred_at"],
                    note=b.get("note", ""), actor_id=b.get("actor_id"),
                    request_key=key)
        # /measures/{uid}/complete
        if len(parts) == 3 and parts[0] == "measures" and parts[2] == "complete":
            return 200, self.store.complete_measure(parts[1], b["actor_id"], request_key=key)
        # /reminders/generate
        if parts == ["reminders", "generate"]:
            return 200, self.store.generate_due_reminders(request_key=key)
        # /escalations/{id}/ack | /resolve
        if len(parts) == 3 and parts[0] == "escalations":
            if parts[2] == "ack":
                return 200, self.store.acknowledge_escalation(
                    parts[1], actor_id=b.get("actor_id"), request_key=key)
            if parts[2] == "resolve":
                return 200, self.store.resolve_escalation(
                    parts[1], actor_id=b.get("actor_id"), request_key=key)
        return 404, {"error": "未知路径"}


def serve(host: str = "127.0.0.1", port: int = 8080,
          database: str | None = None) -> None:
    Handler.store = build_store(database)
    server = ThreadingHTTPServer((host, port), Handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        Handler.store.close()


if __name__ == "__main__":  # pragma: no cover
    serve()
