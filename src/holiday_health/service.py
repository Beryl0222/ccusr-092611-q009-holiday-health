"""假期健康提醒编排的持久化边界与编排服务。

设计要点
========
* 所有业务变化既落实体表也追加 ``events`` 事件表；事件永不更新，
  权限被撤销、计划被取消或服务重启后，值班人员仍可凭事件解释每条
  历史提醒与待处理升级。
* 写操作接受调用方提供的幂等请求键，键内缓存结果，重复同步
  （含“已完成措施”的重复同步）不会再次触发任何状态变化或事件。
* 风险窗口按版本管理：``sync_measures`` 只让覆盖日期上风险版本
  发生变化的措施产生新修订，其余窗口完全不动。
* 措施状态迁移为 pending -> done / superseded，新修订继承未完成
  旧条目的待办语义；已完成的旧条目保持 done 且永不复活。
* 服务实例无内存业务状态，重启后重新打开同一 SQLite 文件即可
  继续执行尚未完成的事项与未确认升级。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Optional

from .domain import (
    Clock, Plan, RiskLevel, RiskProfile, RiskWindow, SymptomReport,
    SystemClock, add_months, build_measure_specs, get_zone, is_child_age,
    local_midnight, parse_local_date, parse_ts, requires_urgent_care,
    to_iso, FEVER_CARE_PATHWAY,
)


class ServiceError(Exception):
    """业务规则冲突（不存在、无权、状态不允许等）。"""


class ConflictError(ServiceError):
    """乐观版本冲突。"""


__all__ = ["ServiceError", "ConflictError", "TravelHealthService"]


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class TravelHealthService:
    def __init__(self, database: str = ":memory:", clock: Optional[Clock] = None):
        self.clock: Clock = clock or SystemClock()
        # check_same_thread=False：HTTP 边界用单把锁串行化访问；
        # SQLite 默认对同连接的并发使用在锁保护下是安全的。
        self.connection = sqlite3.connect(database, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------

    def _now_iso(self) -> str:
        return to_iso(self.clock.now())

    @contextmanager
    def transaction(self):
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _init_schema(self) -> None:
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS plans(
            plan_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
            destination TEXT NOT NULL, dest_timezone TEXT NOT NULL,
            depart_date TEXT NOT NULL, return_date TEXT NOT NULL,
            status TEXT NOT NULL, version INTEGER NOT NULL,
            payload TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS members(
            member_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL,
            name TEXT NOT NULL, birth_date TEXT NOT NULL,
            relationship TEXT NOT NULL, is_child INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(plan_id) REFERENCES plans(plan_id));
        CREATE TABLE IF NOT EXISTS consents(
            member_id TEXT NOT NULL, granted_by TEXT NOT NULL, scope TEXT NOT NULL,
            status TEXT NOT NULL, version INTEGER NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(member_id, scope));
        CREATE TABLE IF NOT EXISTS risk_windows(
            window_id TEXT PRIMARY KEY, destination TEXT NOT NULL,
            start_date TEXT NOT NULL, end_date TEXT NOT NULL,
            level TEXT NOT NULL, risk_version INTEGER NOT NULL,
            superseded_by TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS measures(
            measure_uid TEXT PRIMARY KEY, plan_id TEXT NOT NULL, member_id TEXT NOT NULL,
            spec_key TEXT NOT NULL, revision INTEGER NOT NULL,
            kind TEXT NOT NULL, audience TEXT NOT NULL,
            title TEXT NOT NULL, advice TEXT NOT NULL,
            due_at TEXT NOT NULL, window_start TEXT NOT NULL, window_end TEXT NOT NULL,
            risk_level TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, completed_at TEXT, completion_request TEXT,
            digest TEXT NOT NULL,
            FOREIGN KEY(plan_id) REFERENCES plans(plan_id));
        CREATE TABLE IF NOT EXISTS symptoms(
            report_id TEXT PRIMARY KEY, member_id TEXT NOT NULL,
            occurred_at TEXT NOT NULL, symptoms TEXT NOT NULL, note TEXT NOT NULL,
            from_risk_area INTEGER NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS escalations(
            escalation_id TEXT PRIMARY KEY, member_id TEXT NOT NULL,
            report_id TEXT NOT NULL, level TEXT NOT NULL, status TEXT NOT NULL,
            care_pathway TEXT NOT NULL, created_at TEXT NOT NULL,
            acknowledged_at TEXT, resolved_at TEXT, ack_request TEXT,
            resolve_request TEXT);
        CREATE TABLE IF NOT EXISTS reminder_deliveries(
            measure_uid TEXT NOT NULL, due_slot TEXT NOT NULL,
            delivered_at TEXT NOT NULL, request_key TEXT NOT NULL,
            PRIMARY KEY(measure_uid, due_slot));
        CREATE TABLE IF NOT EXISTS events(
            event_id TEXT PRIMARY KEY, aggregate_id TEXT NOT NULL,
            kind TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS idempotency(
            request_key TEXT PRIMARY KEY, response TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def _event(self, aggregate_id: str, kind: str, body: dict) -> None:
        self.connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?)",
            (_new_id("evt"), aggregate_id, kind, json.dumps(body, ensure_ascii=False), self._now_iso()),
        )

    def _idem_get(self, key: Optional[str]):
        if key is None:
            return None
        row = self.connection.execute(
            "SELECT response FROM idempotency WHERE request_key=?", (key,)).fetchone()
        return None if row is None else json.loads(row["response"])

    def _idem_put(self, key: Optional[str], response: dict) -> None:
        if key is None:
            return
        self.connection.execute(
            "INSERT OR IGNORE INTO idempotency VALUES(?,?,?)",
            (key, json.dumps(response, ensure_ascii=False), self._now_iso()),
        )

    # ------------------------------------------------------------------
    # 计划
    # ------------------------------------------------------------------

    def create_plan(self, owner_id, destination, dest_timezone, depart_date,
                    return_date, request_key=None, payload=None) -> dict:
        depart_date = parse_local_date(depart_date)
        return_date = parse_local_date(return_date)
        get_zone(dest_timezone)  # 校验时区，避免后续提醒错位
        if return_date < depart_date:
            raise ServiceError("回程日期不得早于出发日期")
        with self.transaction():
            cached = self._idem_get(request_key)
            if cached is not None:
                return cached
            plan_id = _new_id("plan")
            now = self._now_iso()
            self.connection.execute(
                "INSERT INTO plans VALUES(?,?,?,?,?,?,?,?,?,?)",
                (plan_id, owner_id, destination, dest_timezone,
                 depart_date.isoformat(), return_date.isoformat(),
                 "active", 1, json.dumps(payload or {}, ensure_ascii=False), now))
            self._event(plan_id, "plan_created", {
                "owner_id": owner_id, "destination": destination,
                "dest_timezone": dest_timezone,
                "depart_date": depart_date.isoformat(),
                "return_date": return_date.isoformat(),
            })
            response = self.get_plan(plan_id)
            self._idem_put(request_key, response)
            return response

    def _row_plan(self, row: sqlite3.Row) -> dict:
        return {
            "plan_id": row["plan_id"], "owner_id": row["owner_id"],
            "destination": row["destination"], "dest_timezone": row["dest_timezone"],
            "depart_date": row["depart_date"], "return_date": row["return_date"],
            "status": row["status"], "version": row["version"],
            "payload": json.loads(row["payload"]), "updated_at": row["updated_at"],
        }

    def get_plan(self, plan_id: str) -> dict:
        row = self.connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise ServiceError("计划不存在")
        return self._row_plan(row)

    def _load_plan_obj(self, plan_id: str) -> Plan:
        p = self.get_plan(plan_id)
        return Plan(
            plan_id=p["plan_id"], owner_id=p["owner_id"],
            destination=p["destination"], dest_timezone=p["dest_timezone"],
            depart_date=parse_local_date(p["depart_date"]),
            return_date=parse_local_date(p["return_date"]),
            status=p["status"], version=p["version"], updated_at=p["updated_at"],
        )

    def update_plan_dates(self, plan_id, owner_id, depart_date=None, return_date=None,
                          request_key=None, expected_version=None) -> dict:
        with self.transaction():
            cached = self._idem_get(request_key)
            if cached is not None:
                return cached
            row = self.connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if row is None:
                raise ServiceError("计划不存在")
            if row["owner_id"] != owner_id:
                raise ServiceError("无权操作此计划")
            if row["status"] != "active":
                raise ServiceError("计划已取消，不能修改")
            if expected_version is not None and row["version"] != expected_version:
                raise ConflictError("版本冲突")
            new_dep = parse_local_date(depart_date) if depart_date else parse_local_date(row["depart_date"])
            new_ret = parse_local_date(return_date) if return_date else parse_local_date(row["return_date"])
            if new_ret < new_dep:
                raise ServiceError("回程日期不得早于出发日期")
            now = self._now_iso()
            self.connection.execute(
                "UPDATE plans SET depart_date=?, return_date=?, version=version+1, updated_at=? WHERE plan_id=?",
                (new_dep.isoformat(), new_ret.isoformat(), now, plan_id))
            self._event(plan_id, "plan_dates_changed", {
                "depart_date": new_dep.isoformat(), "return_date": new_ret.isoformat(),
                "old_version": row["version"], "new_version": row["version"] + 1,
            })
            response = self.get_plan(plan_id)
            self._idem_put(request_key, response)
            return response

    def cancel_plan(self, plan_id, owner_id, request_key=None, expected_version=None) -> dict:
        with self.transaction():
            cached = self._idem_get(request_key)
            if cached is not None:
                return cached
            row = self.connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if row is None:
                raise ServiceError("计划不存在")
            if row["owner_id"] != owner_id:
                raise ServiceError("无权操作此计划")
            if row["status"] == "cancelled":
                response = self.get_plan(plan_id)
                self._idem_put(request_key, response)
                return response
            if expected_version is not None and row["version"] != expected_version:
                raise ConflictError("版本冲突")
            now = self._now_iso()
            # 计划取消：未完成措施全部作废；已完成历史保留可解释；
            # 症状升级是临床安全事项，不受取消影响，仍在交接视图中可处置
            cancelled_uids = []
            measure_rows = self.connection.execute(
                "SELECT measure_uid, status FROM measures WHERE plan_id=?", (plan_id,)).fetchall()
            for m in measure_rows:
                if m["status"] == "pending":
                    self.connection.execute(
                        "UPDATE measures SET status='superseded' WHERE measure_uid=?",
                        (m["measure_uid"],))
                    cancelled_uids.append(m["measure_uid"])
            self.connection.execute(
                "UPDATE plans SET status='cancelled', version=version+1, updated_at=? WHERE plan_id=?",
                (now, plan_id))
            self._event(plan_id, "plan_cancelled", {
                "owner_id": owner_id, "voided_measures": cancelled_uids,
                "new_version": row["version"] + 1,
            })
            response = self.get_plan(plan_id)
            self._idem_put(request_key, response)
            return response

    # ------------------------------------------------------------------
    # 成员与授权
    # ------------------------------------------------------------------

    def add_member(self, plan_id, owner_id, name, birth_date, relationship,
                   request_key=None) -> dict:
        with self.transaction():
            cached = self._idem_get(request_key)
            if cached is not None:
                return cached
            row = self.connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if row is None:
                raise ServiceError("计划不存在")
            if row["owner_id"] != owner_id:
                raise ServiceError("无权操作此计划")
            birth = parse_local_date(birth_date)
            member_id = _new_id("mem")
            now = self._now_iso()
            is_child = is_child_age(birth, parse_local_date(row["depart_date"]))
            self.connection.execute(
                "INSERT INTO members VALUES(?,?,?,?,?,?,?)",
                (member_id, plan_id, name, birth.isoformat(), relationship, int(is_child), now))
            self._event(member_id, "member_added", {
                "plan_id": plan_id, "name": name,
                "birth_date": birth.isoformat(), "is_child": is_child,
            })
            response = self.get_member(member_id)
            self._idem_put(request_key, response)
            return response

    def get_member(self, member_id: str) -> dict:
        row = self.connection.execute("SELECT * FROM members WHERE member_id=?", (member_id,)).fetchone()
        if row is None:
            raise ServiceError("成员不存在")
        return {
            "member_id": row["member_id"], "plan_id": row["plan_id"],
            "name": row["name"], "birth_date": row["birth_date"],
            "relationship": row["relationship"], "is_child": bool(row["is_child"]),
            "created_at": row["created_at"],
        }

    def list_members(self, plan_id: str) -> list[dict]:
        return [
            {"member_id": r["member_id"], "plan_id": r["plan_id"], "name": r["name"],
             "birth_date": r["birth_date"], "relationship": r["relationship"],
             "is_child": bool(r["is_child"]), "created_at": r["created_at"]}
            for r in self.connection.execute(
                "SELECT * FROM members WHERE plan_id=? ORDER BY created_at", (plan_id,))
        ]

    def grant_consent(self, member_id, granted_by, scope="health_measures",
                      request_key=None) -> dict:
        with self.transaction():
            cached = self._idem_get(request_key)
            if cached is not None:
                return cached
            mrow = self.connection.execute("SELECT * FROM members WHERE member_id=?", (member_id,)).fetchone()
            if mrow is None:
                raise ServiceError("成员不存在")
            prow = self.connection.execute("SELECT owner_id FROM plans WHERE plan_id=?", (mrow["plan_id"],)).fetchone()
            if prow["owner_id"] != granted_by:
                raise ServiceError("只有计划负责人可以授权成员")
            now = self._now_iso()
            existing = self.connection.execute(
                "SELECT version, status FROM consents WHERE member_id=? AND scope=?",
                (member_id, scope)).fetchone()
            if existing is None:
                self.connection.execute("INSERT INTO consents VALUES(?,?,?,?,?,?)",
                                        (member_id, granted_by, scope, "granted", 1, now))
                version = 1
            else:
                version = existing["version"] + 1
                self.connection.execute(
                    "UPDATE consents SET status='granted', granted_by=?, version=?, updated_at=? "
                    "WHERE member_id=? AND scope=?",
                    (granted_by, version, now, member_id, scope))
            self._event(member_id, "consent_granted",
                        {"scope": scope, "granted_by": granted_by, "version": version})
            response = self.get_consent(member_id, scope)
            self._idem_put(request_key, response)
            return response

    def revoke_consent(self, member_id, granted_by, scope="health_measures",
                       request_key=None) -> dict:
        with self.transaction():
            cached = self._idem_get(request_key)
            if cached is not None:
                return cached
            existing = self.connection.execute(
                "SELECT * FROM consents WHERE member_id=? AND scope=?", (member_id, scope)).fetchone()
            if existing is None:
                raise ServiceError("授权记录不存在")
            if existing["granted_by"] != granted_by:
                raise ServiceError("只有授权人本人可以撤销")
            if existing["status"] == "revoked":
                response = self.get_consent(member_id, scope)
                self._idem_put(request_key, response)
                return response
            now = self._now_iso()
            version = existing["version"] + 1
            # 撤销后：该成员全部未完成措施作废；已完成历史与已开升级保留，
            # 值班人员仍可凭事件解释并继续临床处置
            voided = []
            for m in self.connection.execute(
                    "SELECT measure_uid, status FROM measures WHERE member_id=?",
                    (member_id,)).fetchall():
                if m["status"] == "pending":
                    self.connection.execute(
                        "UPDATE measures SET status='superseded' WHERE measure_uid=?",
                        (m["measure_uid"],))
                    voided.append(m["measure_uid"])
            self.connection.execute(
                "UPDATE consents SET status='revoked', version=?, updated_at=? WHERE member_id=? AND scope=?",
                (version, now, member_id, scope))
            self._event(member_id, "consent_revoked",
                        {"scope": scope, "version": version, "voided_measures": voided})
            response = self.get_consent(member_id, scope)
            self._idem_put(request_key, response)
            return response

    def get_consent(self, member_id: str, scope: str = "health_measures") -> dict:
        row = self.connection.execute(
            "SELECT * FROM consents WHERE member_id=? AND scope=?", (member_id, scope)).fetchone()
        if row is None:
            return {"member_id": member_id, "scope": scope, "status": "none",
                    "granted_by": None, "version": 0, "updated_at": None}
        return {"member_id": member_id, "scope": scope, "status": row["status"],
                "granted_by": row["granted_by"], "version": row["version"],
                "updated_at": row["updated_at"]}

    def _require_consent(self, member_id: str) -> None:
        row = self.connection.execute(
            "SELECT status FROM consents WHERE member_id=? AND scope='health_measures'",
            (member_id,)).fetchone()
        if row is None or row["status"] != "granted":
            raise ServiceError("成员授权未授予或已被撤销")

    # ------------------------------------------------------------------
    # 风险窗口
    # ------------------------------------------------------------------

    def upsert_risk_window(self, destination, start_date, end_date, level,
                           actor_id=None, request_key=None) -> dict:
        """登记/变更目的地某段日期的风险等级。

        同一 (目的地, 起止日期) 再次设置：等级变化才产生新 risk_version；
        等级相同则版本不变——这是“重复同步不重复触发”在风险侧的保证。
        """
        start_date = parse_local_date(start_date)
        end_date = parse_local_date(end_date)
        if end_date < start_date:
            raise ServiceError("风险窗口结束日期不得早于开始日期")
        level = RiskLevel.of(level)
        with self.transaction():
            cached = self._idem_get(request_key)
            if cached is not None:
                return cached
            now = self._now_iso()
            max_row = self.connection.execute(
                "SELECT COALESCE(MAX(risk_version),0) AS v FROM risk_windows WHERE destination=?",
                (destination,)).fetchone()
            existing = self.connection.execute(
                "SELECT * FROM risk_windows WHERE destination=? AND start_date=? AND end_date=? "
                "AND superseded_by IS NULL",
                (destination, start_date.isoformat(), end_date.isoformat())).fetchone()
            if existing is not None:
                if existing["level"] == level.value:
                    response = self._risk_window_dict(existing)
                    self._idem_put(request_key, response)
                    return response
                window_id = _new_id("risk")
                # 目的地内版本全局单调：任何重叠窗口都能据此选出“最新评估”
                new_version = max_row["v"] + 1
                self.connection.execute(
                    "UPDATE risk_windows SET superseded_by=? WHERE window_id=?",
                    (window_id, existing["window_id"]))
                self.connection.execute(
                    "INSERT INTO risk_windows VALUES(?,?,?,?,?,?,?,?)",
                    (window_id, destination, start_date.isoformat(), end_date.isoformat(),
                     level.value, new_version, None, now))
            else:
                window_id = _new_id("risk")
                new_version = max_row["v"] + 1
                self.connection.execute(
                    "INSERT INTO risk_windows VALUES(?,?,?,?,?,?,?,?)",
                    (window_id, destination, start_date.isoformat(), end_date.isoformat(),
                     level.value, new_version, None, now))
            self._event(destination, "risk_window_changed", {
                "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
                "level": level.value, "risk_version": new_version,
            })
            row = self.connection.execute(
                "SELECT * FROM risk_windows WHERE window_id=?", (window_id,)).fetchone()
            response = self._risk_window_dict(row)
            self._idem_put(request_key, response)
            return response

    def _risk_window_dict(self, row: sqlite3.Row) -> dict:
        return {"window_id": row["window_id"], "destination": row["destination"],
                "start_date": row["start_date"], "end_date": row["end_date"],
                "level": row["level"], "risk_version": row["risk_version"],
                "updated_at": row["updated_at"]}

    def list_risk_windows(self, destination: str) -> list[dict]:
        return [self._risk_window_dict(r) for r in self.connection.execute(
            "SELECT * FROM risk_windows WHERE destination=? AND superseded_by IS NULL "
            "ORDER BY start_date", (destination,))]

    def _profile_for(self, destination: str) -> RiskProfile:
        windows = tuple(
            RiskWindow(destination=r["destination"],
                       start_date=parse_local_date(r["start_date"]),
                       end_date=parse_local_date(r["end_date"]),
                       level=RiskLevel(r["level"]),
                       risk_version=r["risk_version"])
            for r in self.connection.execute(
                "SELECT * FROM risk_windows WHERE destination=? AND superseded_by IS NULL",
                (destination,))
        )
        return RiskProfile(windows=windows)

    # ------------------------------------------------------------------
    # 措施同步：风险增量重算
    # ------------------------------------------------------------------

    def sync_measures(self, plan_id, actor_id, request_key=None) -> dict:
        """按当前风险画像同步计划下全体已授权成员的措施。

        只处理“受影响窗口”：
        * 新出现的 spec_key -> 创建 revision 1；
        * spec_key 已存在但 revision_digest 变化 -> 旧修订置 superseded，
          写入新修订（未完成的待办语义延续，已完成的不复活）；
        * digest 相同（风险未变或重复同步）-> 完全不动，不产生事件。
        因风险下调而不再满足条件的措施（如 low 下不打疫苗）同样作废旧修订。
        授权被撤销的成员跳过；已取消的计划不生成新措施。
        """
        with self.transaction():
            cached = self._idem_get(request_key)
            if cached is not None:
                return cached
            plan = self._load_plan_obj(plan_id)
            if plan.owner_id != actor_id:
                raise ServiceError("无权操作此计划")
            created, superseded, skipped = [], [], []
            now = self._now_iso()
            if plan.status == "active":
                profile = self._profile_for(plan.destination)
                for member_row in self.connection.execute(
                        "SELECT * FROM members WHERE plan_id=?", (plan_id,)).fetchall():
                    member_id = member_row["member_id"]
                    consent = self.connection.execute(
                        "SELECT status FROM consents WHERE member_id=? AND scope='health_measures'",
                        (member_id,)).fetchone()
                    if consent is None or consent["status"] != "granted":
                        skipped.append(member_id)
                        continue
                    specs = build_measure_specs(plan, bool(member_row["is_child"]), profile)
                    live_keys = {s.key for s in specs}
                    existing = {
                        r["spec_key"]: r for r in self.connection.execute(
                            "SELECT * FROM measures WHERE plan_id=? AND member_id=? AND status!='superseded'",
                            (plan_id, member_id))
                    }
                    for spec in specs:
                        prev = existing.get(spec.key)
                        if prev is not None:
                            if prev["status"] == "done":
                                # 已完成措施为终态：任何重复同步/风险重算都不再触发
                                continue
                            if prev["digest"] == spec.revision_digest:
                                continue  # 覆盖窗口风险版本未变（含重复同步）
                        revision = 1 if prev is None else prev["revision"] + 1
                        uid = _new_id("msr")
                        due_dt = local_midnight(spec.due_date, plan.dest_timezone)
                        if prev is not None:
                            self.connection.execute(
                                "UPDATE measures SET status='superseded' WHERE measure_uid=?",
                                (prev["measure_uid"],))
                            superseded.append(prev["measure_uid"])
                        self.connection.execute(
                            "INSERT INTO measures VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (uid, plan_id, member_id, spec.key, revision,
                             spec.kind.value, spec.audience.value,
                             spec.title, spec.advice, to_iso(due_dt),
                             spec.window_start.isoformat(), spec.window_end.isoformat(),
                             spec.risk_level.value, "pending", now, None, None,
                             spec.revision_digest))
                        self._event(uid, "measure_generated", {
                            "plan_id": plan_id, "member_id": member_id,
                            "spec_key": spec.key, "kind": spec.kind.value,
                            "revision": revision,
                            "window_start": spec.window_start.isoformat(),
                            "window_end": spec.window_end.isoformat(),
                            "risk_level": spec.risk_level.value,
                            "supersedes": None if prev is None else prev["measure_uid"],
                        })
                        created.append(uid)
                    # 风险下调导致模板消失：未完成条目作废，已完成的保留历史
                    for key, row in existing.items():
                        if key not in live_keys and row["status"] == "pending":
                            self.connection.execute(
                                "UPDATE measures SET status='superseded' WHERE measure_uid=?",
                                (row["measure_uid"],))
                            superseded.append(row["measure_uid"])
                            self._event(row["measure_uid"], "measure_withdrawn",
                                        {"plan_id": plan_id, "member_id": member_id,
                                         "spec_key": key})
            self._event(plan_id, "measures_synced",
                        {"created": created, "superseded": superseded,
                         "skipped_unauthorized": skipped})
            response = {"plan_id": plan_id, "created": created,
                        "superseded": superseded, "skipped_unauthorized": skipped}
            self._idem_put(request_key, response)
            return response

    def list_measures(self, plan_id: str, member_id: Optional[str] = None,
                      include_superseded: bool = False) -> list[dict]:
        sql = "SELECT * FROM measures WHERE plan_id=?"
        args: list = [plan_id]
        if member_id:
            sql += " AND member_id=?"
            args.append(member_id)
        if not include_superseded:
            sql += " AND status!='superseded'"
        sql += " ORDER BY due_at, spec_key"
        return [self._measure_dict(r) for r in self.connection.execute(sql, args)]

    def _measure_dict(self, r: sqlite3.Row) -> dict:
        return {
            "measure_uid": r["measure_uid"], "plan_id": r["plan_id"],
            "member_id": r["member_id"], "spec_key": r["spec_key"],
            "revision": r["revision"], "kind": r["kind"], "audience": r["audience"],
            "title": r["title"], "advice": r["advice"], "due_at": r["due_at"],
            "window_start": r["window_start"], "window_end": r["window_end"],
            "risk_level": r["risk_level"], "status": r["status"],
            "created_at": r["created_at"], "completed_at": r["completed_at"],
            "completion_request": r["completion_request"],
        }

    def complete_measure(self, measure_uid, actor_id, request_key=None) -> dict:
        """标记措施完成。重复的完成同步（同一 request_key 或已完成）幂等返回。"""
        with self.transaction():
            cached = self._idem_get(request_key)
            if cached is not None:
                return cached
            row = self.connection.execute("SELECT * FROM measures WHERE measure_uid=?",
                                          (measure_uid,)).fetchone()
            if row is None:
                raise ServiceError("措施不存在")
            plan = self.connection.execute("SELECT owner_id FROM plans WHERE plan_id=?",
                                           (row["plan_id"],)).fetchone()
            if plan["owner_id"] != actor_id:
                raise ServiceError("无权操作此计划")
            if row["status"] == "done":
                # 已完成措施的重复同步：原样返回，不再产生事件或提醒
                response = self._measure_dict(row)
                self._idem_put(request_key, response)
                return response
            if row["status"] == "superseded":
                raise ServiceError("该措施修订已作废，不能完成")
            now = self._now_iso()
            self.connection.execute(
                "UPDATE measures SET status='done', completed_at=?, completion_request=? WHERE measure_uid=?",
                (now, request_key, measure_uid))
            self._event(measure_uid, "measure_completed",
                        {"member_id": row["member_id"], "spec_key": row["spec_key"],
                         "revision": row["revision"], "request_key": request_key})
            response = self._measure_dict(
                self.connection.execute("SELECT * FROM measures WHERE measure_uid=?",
                                        (measure_uid,)).fetchone())
            self._idem_put(request_key, response)
            return response

    # ------------------------------------------------------------------
    # 提醒（可注入时钟驱动）
    # ------------------------------------------------------------------

    def _due_slot(self, measure_uid: str) -> str:
        """按天去重：同一修订一天只投递一次，重复扫描不重复触发。"""
        return measure_uid  # 每修订一个到期槽（due_at 已固定到当天）

    def generate_due_reminders(self, request_key=None) -> dict:
        """扫描当前时间已到期、未完成且尚未投递的措施，登记投递记录。

        可安全重复调用：已投递的 (measure_uid) 不再产生事件；
        被作废/已完成的措施不会出现。
        """
        with self.transaction():
            cached = self._idem_get(request_key)
            if cached is not None:
                return cached
            now = self._now_iso()
            due = []
            rows = self.connection.execute(
                "SELECT m.* FROM measures m JOIN plans p ON p.plan_id=m.plan_id "
                "WHERE m.status='pending' AND p.status='active' AND m.due_at<=?",
                (now,)).fetchall()
            for r in rows:
                slot = self._due_slot(r["measure_uid"])
                exists = self.connection.execute(
                    "SELECT 1 FROM reminder_deliveries WHERE measure_uid=? AND due_slot=?",
                    (r["measure_uid"], slot)).fetchone()
                if exists:
                    continue
                self.connection.execute(
                    "INSERT INTO reminder_deliveries VALUES(?,?,?,?)",
                    (r["measure_uid"], slot, now, request_key or _new_id("auto")))
                self._event(r["measure_uid"], "reminder_delivered",
                            {"member_id": r["member_id"], "spec_key": r["spec_key"],
                             "kind": r["kind"], "audience": r["audience"],
                             "due_at": r["due_at"], "delivered_at": now})
                due.append(r["measure_uid"])
            response = {"generated_at": now, "reminders": due}
            self._idem_put(request_key, response)
            return response

    def pending_reminders(self, plan_id: Optional[str] = None) -> list[dict]:
        """值班视图：就医升级永远排在普通提示之前，其余按到期时间。"""
        sql = ("SELECT m.* FROM measures m JOIN plans p ON p.plan_id=m.plan_id "
               "WHERE m.status='pending' AND p.status='active'")
        args: list = []
        if plan_id:
            sql += " AND m.plan_id=?"
            args.append(plan_id)
        sql += " ORDER BY m.due_at"
        items = [self._measure_dict(r) for r in self.connection.execute(sql, args)]
        urgent_member_ids = {
            r["member_id"] for r in self.connection.execute(
                "SELECT member_id FROM escalations WHERE status='open'")}
        urgent, normal = [], []
        for item in items:
            (urgent if item["member_id"] in urgent_member_ids else normal).append(item)
        return urgent + normal

    def delivered_reminders(self, measure_uid: str) -> list[dict]:
        return [{"measure_uid": r["measure_uid"], "due_slot": r["due_slot"],
                 "delivered_at": r["delivered_at"], "request_key": r["request_key"]}
                for r in self.connection.execute(
                    "SELECT * FROM reminder_deliveries WHERE measure_uid=? ORDER BY delivered_at",
                    (measure_uid,))]

    # ------------------------------------------------------------------
    # 症状报告与升级
    # ------------------------------------------------------------------

    def _was_in_risk_area(self, member_id: str, occurred_utc: datetime) -> bool:
        """症状出现时（按目的地时区换算当地日期）该成员是否身处 moderate+ 目的地。

        仅对处于行程内的成员判定；回程后的症状通过行程最后一日风险继承。
        """
        mrow = self.connection.execute("SELECT * FROM members WHERE member_id=?",
                                       (member_id,)).fetchone()
        if mrow is None:
            return False
        plan = self._load_plan_obj(mrow["plan_id"])
        zone = get_zone(plan.dest_timezone)
        local_day = occurred_utc.astimezone(zone).date()
        profile = self._profile_for(plan.destination)
        if plan.depart_date <= local_day <= plan.return_date:
            return profile.level_on(local_day).rank >= RiskLevel.MODERATE.rank
        if local_day > plan.return_date:
            # 回程后发病：继承行程末日的风险暴露
            return profile.level_on(plan.return_date).rank >= RiskLevel.MODERATE.rank
        return False

    def report_symptoms(self, member_id, symptoms, occurred_at, note="",
                        actor_id=None, request_key=None) -> dict:
        symptoms = tuple(symptoms) if not isinstance(symptoms, str) else tuple(
            s.strip() for s in symptoms.split(",") if s.strip())
        occurred_utc = parse_ts(occurred_at) if not isinstance(occurred_at, datetime) else (
            occurred_at if occurred_at.tzinfo else occurred_at.replace(tzinfo=timezone.utc))
        occurred_utc = occurred_utc.astimezone(timezone.utc)
        with self.transaction():
            cached = self._idem_get(request_key)
            if cached is not None:
                return cached
            mrow = self.connection.execute("SELECT * FROM members WHERE member_id=?",
                                           (member_id,)).fetchone()
            if mrow is None:
                raise ServiceError("成员不存在")
            plan = self.connection.execute("SELECT * FROM plans WHERE plan_id=?",
                                           (mrow["plan_id"],)).fetchone()
            if actor_id is not None and plan["owner_id"] != actor_id:
                raise ServiceError("无权操作此计划")
            # 监测窗口校验：回程后超过一个自然月不再计入本计划监测
            monitor_end = add_months(parse_local_date(plan["return_date"]), 1)
            local_day = occurred_utc.astimezone(get_zone(plan["dest_timezone"])).date()
            if local_day >= monitor_end:
                raise ServiceError("症状发生时间已超出回程后一个月监测窗口")
            from_risk = self._was_in_risk_area(member_id, occurred_utc)
            report_id = _new_id("rpt")
            now = self._now_iso()
            self.connection.execute(
                "INSERT INTO symptoms VALUES(?,?,?,?,?,?,?)",
                (report_id, member_id, to_iso(occurred_utc),
                 json.dumps(symptoms, ensure_ascii=False), note, int(from_risk), now))
            self._event(report_id, "symptoms_reported", {
                "member_id": member_id, "symptoms": list(symptoms),
                "occurred_at": to_iso(occurred_utc), "from_risk_area": from_risk,
            })
            response = {"report_id": report_id, "member_id": member_id,
                        "symptoms": list(symptoms), "note": note,
                        "occurred_at": to_iso(occurred_utc),
                        "from_risk_area": from_risk,
                        "escalation_id": None, "care_pathway": []}
            report = SymptomReport(
                report_id=report_id, member_id=member_id, occurred_at=to_iso(occurred_utc),
                symptoms=symptoms, note=note, from_risk_area=from_risk, created_at=now)
            if requires_urgent_care(report):
                esc_id = self._open_escalation(member_id, report_id)
                response["escalation_id"] = esc_id
                response["care_pathway"] = list(FEVER_CARE_PATHWAY)
            self._idem_put(request_key, response)
            return response

    def _open_escalation(self, member_id: str, report_id: str) -> str:
        esc_id = _new_id("esc")
        now = self._now_iso()
        self.connection.execute(
            "INSERT INTO escalations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (esc_id, member_id, report_id, "urgent_care", "open",
             json.dumps(FEVER_CARE_PATHWAY, ensure_ascii=False), now, None, None, None, None))
        self._event(esc_id, "escalation_opened", {
            "member_id": member_id, "report_id": report_id,
            "level": "urgent_care", "care_pathway": list(FEVER_CARE_PATHWAY),
        })
        return esc_id

    def list_symptoms(self, member_id: str) -> list[dict]:
        out = []
        for r in self.connection.execute(
                "SELECT * FROM symptoms WHERE member_id=? ORDER BY occurred_at", (member_id,)):
            out.append({"report_id": r["report_id"], "member_id": r["member_id"],
                        "symptoms": json.loads(r["symptoms"]), "note": r["note"],
                        "occurred_at": r["occurred_at"],
                        "from_risk_area": bool(r["from_risk_area"]),
                        "created_at": r["created_at"]})
        return out

    # ------------------------------------------------------------------
    # 升级处置（重启后仍可继续）
    # ------------------------------------------------------------------

    def list_escalations(self, status: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM escalations"
        args: list = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY created_at"
        return [self._escalation_dict(r) for r in self.connection.execute(sql, args)]

    def _escalation_dict(self, r: sqlite3.Row) -> dict:
        return {"escalation_id": r["escalation_id"], "member_id": r["member_id"],
                "report_id": r["report_id"], "level": r["level"],
                "status": r["status"], "care_pathway": json.loads(r["care_pathway"]),
                "created_at": r["created_at"], "acknowledged_at": r["acknowledged_at"],
                "resolved_at": r["resolved_at"]}

    def acknowledge_escalation(self, escalation_id, actor_id=None, request_key=None) -> dict:
        with self.transaction():
            cached = self._idem_get(request_key)
            if cached is not None:
                return cached
            row = self.connection.execute("SELECT * FROM escalations WHERE escalation_id=?",
                                          (escalation_id,)).fetchone()
            if row is None:
                raise ServiceError("升级不存在")
            if row["status"] == "open":
                now = self._now_iso()
                self.connection.execute(
                    "UPDATE escalations SET status='acknowledged', acknowledged_at=?, ack_request=? "
                    "WHERE escalation_id=?", (now, request_key, escalation_id))
                self._event(escalation_id, "escalation_acknowledged",
                            {"actor_id": actor_id, "acknowledged_at": now})
            response = self._escalation_dict(
                self.connection.execute("SELECT * FROM escalations WHERE escalation_id=?",
                                        (escalation_id,)).fetchone())
            self._idem_put(request_key, response)
            return response

    def resolve_escalation(self, escalation_id, actor_id=None, request_key=None) -> dict:
        with self.transaction():
            cached = self._idem_get(request_key)
            if cached is not None:
                return cached
            row = self.connection.execute("SELECT * FROM escalations WHERE escalation_id=?",
                                          (escalation_id,)).fetchone()
            if row is None:
                raise ServiceError("升级不存在")
            if row["status"] != "resolved":
                now = self._now_iso()
                self.connection.execute(
                    "UPDATE escalations SET status='resolved', resolved_at=?, resolve_request=? "
                    "WHERE escalation_id=?", (now, request_key, escalation_id))
                self._event(escalation_id, "escalation_resolved",
                            {"actor_id": actor_id, "resolved_at": now})
            response = self._escalation_dict(
                self.connection.execute("SELECT * FROM escalations WHERE escalation_id=?",
                                        (escalation_id,)).fetchone())
            self._idem_put(request_key, response)
            return response

    # ------------------------------------------------------------------
    # 历史解释（权限撤销/计划取消/重启后值班人员可核对）
    # ------------------------------------------------------------------

    def history(self, aggregate_id: Optional[str] = None,
                kinds: Optional[Iterable[str]] = None) -> list[dict]:
        sql = "SELECT * FROM events"
        where, args = [], []
        if aggregate_id:
            where.append("aggregate_id=?")
            args.append(aggregate_id)
        if kinds:
            where.append(f"kind IN ({','.join('?' for _ in kinds)})")
            args.extend(kinds)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY rowid"
        return [{"event_id": r["event_id"], "aggregate_id": r["aggregate_id"],
                 "kind": r["kind"], "body": json.loads(r["body"]),
                 "created_at": r["created_at"]}
                for r in self.connection.execute(sql, args)]

    def duty_handoff(self) -> dict:
        """值班交接视图：待处理升级 + 未完成措施 + 就医优先的待办排序。"""
        open_escalations = self.list_escalations("open")
        acknowledged = self.list_escalations("acknowledged")
        pending = self.pending_reminders()
        return {
            "generated_at": self._now_iso(),
            "open_escalations": open_escalations,
            "in_progress_escalations": acknowledged,
            "pending_reminders": pending,
            "pending_count": len(pending),
            "escalation_count": len(open_escalations),
        }
