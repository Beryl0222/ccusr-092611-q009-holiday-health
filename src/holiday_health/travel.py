"""旅行健康编排服务。

保存旅行计划、成员授权、目的地风险窗口、预防措施与症状报告，并以
可注入的当前时间驱动提醒与升级。设计要点：

* 所有写入在单个 SQLite 事务内完成（由 ``_idempotent`` 或公开的
  ``sync_measures`` / ``due_reminders`` / ``pending_escalations`` 持事务，
  ``*_locked`` 内部实现不再自行开事务）；
* 提醒/升级是按当前时间扫描持久化状态的结果，服务重启后依据磁盘数据
  重建，值班人员始终能解释历史提醒与待处理升级并继续办结；
* 已完成措施通过 completions 去重，重复同步不会再次触发；
* 风险窗口变化时只重算与该窗口关联的措施期望（按 window_id 收敛）；
* 儿童“暂缓集体活动”与成人建议分别派生、分别记录（advice_class）；
* 跨时区日期锚定 IANA 时区，返程后一个自然月的监测按居家地日历计算；
* 疫区暴露者在行程中或返程监测期内报告症状时，就医路径排在普通提示
  之前，并立即生成开放升级。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from . import domain as D

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS plans(
  plan_id TEXT PRIMARY KEY,
  operator_id TEXT NOT NULL,
  destination TEXT NOT NULL,
  destination_tz TEXT NOT NULL,
  home_tz TEXT NOT NULL,
  depart_date TEXT NOT NULL,
  return_date TEXT NOT NULL,
  status TEXT NOT NULL,
  version INTEGER NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_events(
  event_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(plan_id) REFERENCES plans(plan_id)
);
CREATE TABLE IF NOT EXISTS members(
  plan_id TEXT NOT NULL,
  member_id TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  consent_status TEXT NOT NULL,
  version INTEGER NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(plan_id, member_id)
);
CREATE TABLE IF NOT EXISTS risk_windows(
  plan_id TEXT NOT NULL,
  window_id TEXT NOT NULL,
  region TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  risk_level TEXT NOT NULL,
  diseases TEXT NOT NULL,
  version INTEGER NOT NULL,
  active INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(plan_id, window_id)
);
CREATE TABLE IF NOT EXISTS measures(
  measure_uid TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  member_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  advice_class TEXT NOT NULL,
  scheduled_date TEXT NOT NULL,
  tz_name TEXT NOT NULL,
  due_at TEXT NOT NULL,
  window_id TEXT,
  disease TEXT,
  status TEXT NOT NULL,
  generation INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
-- 同一成员同一天的同一措施（疫苗按疾病区分）全局唯一，即使多个
-- 风险窗口重叠支撑它；window_id 仅记录其中一个支撑窗口。
CREATE UNIQUE INDEX IF NOT EXISTS measures_slot_unique
  ON measures(plan_id, member_id, kind, scheduled_date,
              COALESCE(disease,''));
CREATE TABLE IF NOT EXISTS completions(
  plan_id TEXT NOT NULL,
  member_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  scheduled_date TEXT NOT NULL,
  disease TEXT,
  request_key TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  note TEXT,
  PRIMARY KEY(plan_id, member_id, kind, scheduled_date, disease)
);
-- 疫苗按疾病区分完成槽位（disease 可空，用 COALESCE 收敛）
CREATE UNIQUE INDEX IF NOT EXISTS completions_slot_unique
  ON completions(plan_id, member_id, kind, scheduled_date,
                 COALESCE(disease,''));
CREATE TABLE IF NOT EXISTS reminders(
  reminder_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  member_id TEXT NOT NULL,
  measure_uid TEXT NOT NULL,
  scheduled_date TEXT NOT NULL,
  due_at TEXT NOT NULL,
  status TEXT NOT NULL,
  sent_at TEXT,
  generation INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
-- 每条措施同时至多存在一条未结束（waiting/sent）的提醒
CREATE UNIQUE INDEX IF NOT EXISTS reminders_one_open
  ON reminders(measure_uid) WHERE status IN ('waiting','sent');
CREATE TABLE IF NOT EXISTS symptom_reports(
  report_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  member_id TEXT NOT NULL,
  reported_at TEXT NOT NULL,
  local_date TEXT NOT NULL,
  symptoms TEXT NOT NULL,
  exposed INTEGER NOT NULL,
  in_monitoring INTEGER NOT NULL,
  care_first INTEGER NOT NULL,
  escalation_id TEXT,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS escalations(
  escalation_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL,
  member_id TEXT,
  reason TEXT NOT NULL,
  status TEXT NOT NULL,
  source_ref TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  resolution TEXT,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency(
  request_key TEXT PRIMARY KEY,
  result TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def _parse_day(value: str) -> date:
    return date.fromisoformat(value)


def _parse_dt(value: str) -> datetime:
    return D.as_aware(datetime.fromisoformat(value))


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class TravelHealthError(Exception):
    """业务规则错误。"""


class Clock:
    """可注入时钟；默认使用时区感知 UTC。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class TravelHealthService:
    def __init__(self, database: str = ":memory:", clock=None,
                 check_same_thread: bool = True):
        self.clock = clock or Clock()
        self.connection = sqlite3.connect(
            database, check_same_thread=check_same_thread)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------

    def now(self) -> datetime:
        return D.as_aware(self.clock.now())

    def now_iso(self) -> str:
        return self.now().isoformat()

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _idempotent(self, request_key, action):
        """请求键幂等：整个动作与幂等登记在同一事务内，重复提交返回
        首次结果，写动作只执行一次。"""
        if not request_key:
            raise TravelHealthError("缺少 request_key")
        with self.transaction():
            cached = self.connection.execute(
                "SELECT result FROM idempotency WHERE request_key=?",
                (request_key,)).fetchone()
            if cached:
                return json.loads(cached["result"])
            result = action()
            self.connection.execute(
                "INSERT INTO idempotency VALUES(?,?,?)",
                (request_key, _dumps(result), self.now_iso()))
            return result

    def _event(self, plan_id, kind, body: dict):
        self.connection.execute(
            "INSERT INTO plan_events VALUES(?,?,?,?,?)",
            (_new_id("evt"), plan_id, kind, _dumps(body), self.now_iso()))

    def _get_plan_row(self, plan_id) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise TravelHealthError("旅行计划不存在")
        return row

    @staticmethod
    def _coerce_date(value) -> date:
        if isinstance(value, datetime):
            raise TravelHealthError("日期必须是 date，而非 datetime")
        if isinstance(value, date):
            return value
        return date.fromisoformat(value)

    @staticmethod
    def _assert_operator(row, operator_id):
        if row["operator_id"] != operator_id:
            raise TravelHealthError("无权操作该计划")

    @staticmethod
    def _check_version(row, expected_version):
        if expected_version is not None and row["version"] != expected_version:
            raise TravelHealthError("版本冲突")

    # ------------------------------------------------------------------
    # 计划
    # ------------------------------------------------------------------

    def create_plan(self, plan_id, operator_id, destination,
                    destination_tz, home_tz, depart_date, return_date,
                    request_key, payload=None):
        depart = self._coerce_date(depart_date)
        returning = self._coerce_date(return_date)
        if returning < depart:
            raise TravelHealthError("回程日期不得早于出发日期")
        D.get_zone(destination_tz)
        D.get_zone(home_tz)

        def action():
            now = self.now_iso()
            exists = self.connection.execute(
                "SELECT 1 FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            if exists:
                raise TravelHealthError("旅行计划已存在")
            self.connection.execute(
                "INSERT INTO plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, operator_id, destination, destination_tz,
                 home_tz, depart.isoformat(), returning.isoformat(),
                 D.PLAN_DRAFT, 1, _dumps(payload or {}), now, now))
            self._event(plan_id, "plan_created", {
                "operator_id": operator_id, "destination": destination,
                "depart_date": depart.isoformat(),
                "return_date": returning.isoformat()})
            return self.plan_summary(plan_id)

        return self._idempotent(request_key, action)

    def activate_plan(self, plan_id, operator_id, request_key,
                      expected_version=None):
        def action():
            row = self._get_plan_row(plan_id)
            self._assert_operator(row, operator_id)
            self._check_version(row, expected_version)
            if row["status"] != D.PLAN_DRAFT:
                raise TravelHealthError("仅 draft 计划可激活")
            self._update_plan_status(row, D.PLAN_ACTIVE)
            return self.plan_summary(plan_id)

        return self._idempotent(request_key, action)

    def cancel_plan(self, plan_id, operator_id, request_key,
                    expected_version=None, reason=None):
        """取消计划：停止产生新提醒；历史提醒与开放升级保留，
        值班人员仍可解释并继续办结尚未完成的事项。"""
        def action():
            row = self._get_plan_row(plan_id)
            self._assert_operator(row, operator_id)
            self._check_version(row, expected_version)
            if row["status"] == D.PLAN_CANCELLED:
                return self.plan_summary(plan_id)
            self._update_plan_status(row, D.PLAN_CANCELLED)
            # 尚未发出的提醒作废；已发出提醒作为历史保留
            self.connection.execute(
                "UPDATE reminders SET status=? WHERE plan_id=? AND status=?",
                (D.REMINDER_CANCELLED, plan_id, D.REMINDER_WAITING))
            # 待办措施标记取消（区别于风险重算的 superseded）
            self.connection.execute(
                "UPDATE measures SET status=? WHERE plan_id=? AND status=?",
                (D.STATUS_CANCELLED, plan_id, D.STATUS_PENDING))
            # open 升级不自动关闭：人员跟进可能仍在进行，需显式 resolve
            self._event(plan_id, "plan_cancelled", {
                "reason": reason, "open_escalations_retained": True})
            return self.plan_summary(plan_id)

        return self._idempotent(request_key, action)

    def _update_plan_status(self, row, status):
        version = row["version"] + 1
        self.connection.execute(
            "UPDATE plans SET status=?, version=?, updated_at=? "
            "WHERE plan_id=?",
            (status, version, self.now_iso(), row["plan_id"]))
        self._event(row["plan_id"], "status_transition", {
            "from": row["status"], "to": status, "version": version})

    def plan_summary(self, plan_id):
        row = self._get_plan_row(plan_id)
        return {
            "plan_id": row["plan_id"], "operator_id": row["operator_id"],
            "destination": row["destination"],
            "destination_tz": row["destination_tz"],
            "home_tz": row["home_tz"],
            "depart_date": row["depart_date"],
            "return_date": row["return_date"],
            "status": row["status"], "version": row["version"],
            "updated_at": row["updated_at"],
        }

    def plan_events(self, plan_id):
        self._get_plan_row(plan_id)
        rows = self.connection.execute(
            "SELECT * FROM plan_events WHERE plan_id=? ORDER BY rowid",
            (plan_id,)).fetchall()
        return [{
            "event_id": r["event_id"], "kind": r["kind"],
            "body": json.loads(r["body"]), "created_at": r["created_at"],
        } for r in rows]

    # ------------------------------------------------------------------
    # 成员与授权
    # ------------------------------------------------------------------

    def upsert_member(self, plan_id, member_id, category, name,
                      consent_status, operator_id, request_key,
                      payload=None, expected_version=None):
        if category not in D.MEMBER_CATEGORIES:
            raise TravelHealthError("成员类别必须是 child 或 adult")
        if consent_status not in ("granted", "revoked"):
            raise TravelHealthError("授权状态必须是 granted 或 revoked")

        def action():
            plan = self._get_plan_row(plan_id)
            self._assert_operator(plan, operator_id)
            self._check_version(plan, expected_version)
            now = self.now_iso()
            existing = self.connection.execute(
                "SELECT * FROM members WHERE plan_id=? AND member_id=?",
                (plan_id, member_id)).fetchone()
            if existing is None:
                self.connection.execute(
                    "INSERT INTO members VALUES(?,?,?,?,?,?,?,?,?)",
                    (plan_id, member_id, category, name, consent_status,
                     1, _dumps(payload or {}), now, now))
                self._event(plan_id, "member_added",
                            {"member_id": member_id, "category": category})
            else:
                version = existing["version"] + 1
                self.connection.execute(
                    "UPDATE members SET consent_status=?, category=?, "
                    "name=?, payload=?, version=?, updated_at=? "
                    "WHERE plan_id=? AND member_id=?",
                    (consent_status, category, name,
                     _dumps(payload or {}), version, now,
                     plan_id, member_id))
                self._event(plan_id, "member_consent_changed", {
                    "member_id": member_id,
                    "from": existing["consent_status"],
                    "to": consent_status, "version": version})
            if consent_status == "revoked":
                # 冻结待办措施、撤回尚未发出的提醒；已发提醒与开放升级保留
                self.connection.execute(
                    "UPDATE measures SET status=? WHERE plan_id=? "
                    "AND member_id=? AND status=?",
                    (D.STATUS_HELD, plan_id, member_id, D.STATUS_PENDING))
                self.connection.execute(
                    "UPDATE reminders SET status=? WHERE plan_id=? "
                    "AND member_id=? AND status=?",
                    (D.REMINDER_CANCELLED, plan_id, member_id,
                     D.REMINDER_WAITING))
            # 授权变化只影响该成员；同步会结算/按授权状态决定是否新建
            self._sync_measures_locked(
                plan_id, reason="member_upsert:" + member_id,
                only_member=member_id)
            return self.get_member(plan_id, member_id)

        return self._idempotent(request_key, action)

    def get_member(self, plan_id, member_id):
        row = self.connection.execute(
            "SELECT * FROM members WHERE plan_id=? AND member_id=?",
            (plan_id, member_id)).fetchone()
        if row is None:
            raise TravelHealthError("成员不存在或未加入计划")
        return self._member_dict(row)

    def list_members(self, plan_id):
        self._get_plan_row(plan_id)
        rows = self.connection.execute(
            "SELECT * FROM members WHERE plan_id=? ORDER BY member_id",
            (plan_id,)).fetchall()
        return [self._member_dict(r) for r in rows]

    @staticmethod
    def _member_dict(row):
        return {
            "plan_id": row["plan_id"], "member_id": row["member_id"],
            "category": row["category"], "name": row["name"],
            "consent_status": row["consent_status"],
            "version": row["version"], "updated_at": row["updated_at"],
        }

    def _member_rows(self, plan_id):
        return self.connection.execute(
            "SELECT * FROM members WHERE plan_id=? ORDER BY member_id",
            (plan_id,)).fetchall()

    # ------------------------------------------------------------------
    # 风险窗口
    # ------------------------------------------------------------------

    def upsert_risk_window(self, plan_id, window_id, region, start_date,
                           end_date, risk_level, diseases, operator_id,
                           request_key, expected_version=None):
        start = self._coerce_date(start_date)
        end = self._coerce_date(end_date)
        if end < start:
            raise TravelHealthError("风险窗口结束日期不得早于开始日期")
        if risk_level not in D.RISK_LEVELS:
            raise TravelHealthError("未知风险级别")
        diseases = sorted(set(diseases or []))

        def action():
            plan = self._get_plan_row(plan_id)
            self._assert_operator(plan, operator_id)
            self._check_version(plan, expected_version)
            now = self.now_iso()
            existing = self.connection.execute(
                "SELECT * FROM risk_windows WHERE plan_id=? AND window_id=?",
                (plan_id, window_id)).fetchone()
            if existing is None:
                self.connection.execute(
                    "INSERT INTO risk_windows VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (plan_id, window_id, region, start.isoformat(),
                     end.isoformat(), risk_level, _dumps(diseases), 1,
                     1, now, now))
            else:
                version = existing["version"] + 1
                self.connection.execute(
                    "UPDATE risk_windows SET region=?, start_date=?, "
                    "end_date=?, risk_level=?, diseases=?, version=?, "
                    "active=1, updated_at=? WHERE plan_id=? AND window_id=?",
                    (region, start.isoformat(), end.isoformat(),
                     risk_level, _dumps(diseases), version, now,
                     plan_id, window_id))
            self._event(plan_id, "risk_window_upserted", {
                "window_id": window_id, "risk_level": risk_level,
                "diseases": diseases, "start_date": start.isoformat(),
                "end_date": end.isoformat()})
            # 目的地风险变化：重算该窗口关联措施，并联动常规作用域
            # （返程监测建议依赖暴露窗口的聚合判断）；其他窗口不受影响
            self._sync_measures_locked(
                plan_id, only_window=window_id,
                reason="risk_window_upsert:" + window_id)
            return self.get_risk_window(plan_id, window_id)

        return self._idempotent(request_key, action)

    def remove_risk_window(self, plan_id, window_id, operator_id, request_key):
        def action():
            plan = self._get_plan_row(plan_id)
            self._assert_operator(plan, operator_id)
            existing = self.connection.execute(
                "SELECT * FROM risk_windows WHERE plan_id=? AND window_id=?",
                (plan_id, window_id)).fetchone()
            if existing is None:
                raise TravelHealthError("风险窗口不存在")
            self.connection.execute(
                "UPDATE risk_windows SET active=0, updated_at=? "
                "WHERE plan_id=? AND window_id=?",
                (self.now_iso(), plan_id, window_id))
            self._event(plan_id, "risk_window_removed",
                        {"window_id": window_id})
            self._sync_measures_locked(
                plan_id, only_window=window_id,
                reason="risk_window_remove:" + window_id)
            return {"window_id": window_id, "active": False}

        return self._idempotent(request_key, action)

    def get_risk_window(self, plan_id, window_id):
        row = self.connection.execute(
            "SELECT * FROM risk_windows WHERE plan_id=? AND window_id=?",
            (plan_id, window_id)).fetchone()
        if row is None:
            raise TravelHealthError("风险窗口不存在")
        return self._window_dict(row)

    def list_risk_windows(self, plan_id, active_only=True):
        self._get_plan_row(plan_id)
        sql = "SELECT * FROM risk_windows WHERE plan_id=?"
        if active_only:
            sql += " AND active=1"
        sql += " ORDER BY start_date, window_id"
        return [self._window_dict(r) for r in
                self.connection.execute(sql, (plan_id,)).fetchall()]

    @staticmethod
    def _window_dict(row):
        return {
            "plan_id": row["plan_id"], "window_id": row["window_id"],
            "region": row["region"], "start_date": row["start_date"],
            "end_date": row["end_date"], "risk_level": row["risk_level"],
            "diseases": json.loads(row["diseases"]),
            "version": row["version"], "active": bool(row["active"]),
        }

    def _active_windows(self, plan_id):
        rows = self.connection.execute(
            "SELECT * FROM risk_windows WHERE plan_id=? AND active=1",
            (plan_id,)).fetchall()

        class Window:
            pass

        windows = []
        for r in rows:
            w = Window()
            w.window_id = r["window_id"]
            w.risk_level = r["risk_level"]
            w.diseases = json.loads(r["diseases"])
            w.destination_tz = None
            w.start_date = _parse_day(r["start_date"])
            w.end_date = _parse_day(r["end_date"])
            windows.append(w)
        return windows

    # ------------------------------------------------------------------
    # 措施同步：核心编排逻辑（调用方持事务）
    # ------------------------------------------------------------------

    def sync_measures(self, plan_id, only_window=None, reason="manual"):
        """把“领域期望”与存储措施对齐（公开入口，自持事务）。"""
        with self.transaction():
            return self._sync_measures_locked(
                plan_id, only_window=only_window, reason=reason)

    def _sync_measures_locked(self, plan_id, only_window=None, reason="manual",
                              only_member=None):
        """以“作用域”为单位把领域期望与存储措施对齐：

        * 常规作用域（None）：洗手 + 返程监测期分类建议（window_id 为空），
          是否派生监测建议取决于“是否存在任何暴露窗口”的聚合判断；
        * 窗口作用域（window_id）：仅该窗口支撑的防蚊/疫苗措施。

        ``only_window`` 给定时只对齐该窗口并联动常规作用域，其他窗口的
        措施完全不触碰；不给定时对齐全部作用域（新成员加入/手工同步）。
        已完成措施（completion）永不复活、永不重复提醒。
        """
        plan = self._get_plan_row(plan_id)
        depart = _parse_day(plan["depart_date"])
        returning = _parse_day(plan["return_date"])
        home_tz = plan["home_tz"]
        active_windows = self._active_windows(plan_id)
        for w in active_windows:
            w.destination_tz = plan["destination_tz"]
        members = self._member_rows(plan_id)
        if only_member is not None:
            members = [m for m in members if m["member_id"] == only_member]

        if only_window is None:
            scopes = [None] + [w.window_id for w in active_windows]
        else:
            # 指定窗口变化：窗口作用域 + 联动常规作用域（暴露聚合可能变）
            scopes = [only_window, None]

        totals = {"created": 0, "superseded": 0, "skipped_completed": 0}
        for member in members:
            for scope in scopes:
                self._reconcile_scope(
                    plan, member, active_windows, depart, returning,
                    home_tz, scope, totals)

        self._event(plan_id, "measures_synced", {
            "reason": reason, "only_window": only_window,
            "only_member": only_member, **totals})
        return totals

    def _reconcile_scope(self, plan, member, active_windows, depart,
                         returning, home_tz, scope, totals):
        now = self.now_iso()
        granted = member["consent_status"] == "granted"

        def exposing(w):
            return (w.risk_level != D.RISK_LOW
                    and D.window_covers_trip(w.start_date, w.end_date,
                                             depart, returning))

        if scope is None:
            expected = list(D.expected_routine_measures(
                depart, returning, plan["destination_tz"], home_tz,
                exposed=any(exposing(w) for w in active_windows),
                category=member["category"]))
            scope_predicate = "window_id IS NULL"
            scope_params = []
            other_keys = None
        else:
            target = next((w for w in active_windows
                           if w.window_id == scope), None)
            if target is None:
                expected = []
            else:
                expected = [
                    (replace(e, tz_name=home_tz)
                     if e.kind == D.MEASURE_VACCINE else e)
                    for e in D.expected_window_measures(
                        target, depart, returning)]
            scope_predicate = "window_id=?"
            scope_params = [scope]
            # 重叠窗口可能支撑同一措施：记录其他窗口仍需要的键以便改挂
            other_keys = {}
            for w in active_windows:
                if w.window_id == scope:
                    continue
                for exp in D.expected_window_measures(w, depart, returning):
                    other_keys.setdefault(exp.dedup_key, w.window_id)

        expected_keys = {e.dedup_key for e in expected}

        # 1) 结算本作用域内不再被期望、且尚未完成的措施
        rows = self.connection.execute(
            f"SELECT * FROM measures WHERE plan_id=? AND member_id=? "
            f"AND status=? AND {scope_predicate}",
            [plan["plan_id"], member["member_id"], D.STATUS_PENDING,
             *scope_params]).fetchall()
        for m in rows:
            key = self._measure_dedup_from_row(m)
            if key in expected_keys:
                continue
            if other_keys is not None and key in other_keys:
                # 重叠窗口仍需要同一措施：改挂到另一窗口，不取消不重发
                self.connection.execute(
                    "UPDATE measures SET window_id=?, "
                    "generation=generation+1, updated_at=? WHERE measure_uid=?",
                    (other_keys[key], now, m["measure_uid"]))
                continue
            self.connection.execute(
                "UPDATE measures SET status=?, generation=generation+1,"
                " updated_at=? WHERE measure_uid=?",
                (D.STATUS_SUPERSEDED, now, m["measure_uid"]))
            self.connection.execute(
                "UPDATE reminders SET status=? WHERE measure_uid=? "
                "AND status=?",
                (D.REMINDER_CANCELLED, m["measure_uid"],
                 D.REMINDER_WAITING))
            totals["superseded"] += 1

        # 授权撤销或计划取消：只结算，不产生新待办
        if not granted or plan["status"] == D.PLAN_CANCELLED:
            return

        # 2) 为缺失的期望建措施（已完成的不复活、不重复触发）
        for exp in expected:
            if self._has_completion(plan["plan_id"], member["member_id"], exp):
                totals["skipped_completed"] += 1
                continue
            if self._insert_measure_if_absent(plan, member, exp, now):
                totals["created"] += 1


    @staticmethod
    def _measure_dedup_from_row(row) -> str:
        if row["kind"] == D.MEASURE_VACCINE:
            return f"{row['kind']}|{row['disease']}|{row['scheduled_date']}"
        return f"{row['kind']}|{row['scheduled_date']}"

    def _has_completion(self, plan_id, member_id, exp: D.ExpectedMeasure) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM completions WHERE plan_id=? AND member_id=? "
            "AND kind=? AND scheduled_date=? AND COALESCE(disease,'')=?",
            (plan_id, member_id, exp.kind, exp.scheduled.isoformat(),
             exp.disease or "")).fetchone()
        return row is not None

    def _insert_measure_if_absent(self, plan_row, member_row,
                                  exp: D.ExpectedMeasure, now):
        due_dt = self._compute_due(plan_row, exp)
        try:
            self.connection.execute(
                "INSERT INTO measures(measure_uid,plan_id,member_id,kind,"
                "advice_class,scheduled_date,tz_name,due_at,window_id,"
                "disease,status,generation,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_new_id("m"), plan_row["plan_id"], member_row["member_id"],
                 exp.kind, D.MEASURE_META[exp.kind]["advice_class"],
                 exp.scheduled.isoformat(), exp.tz_name,
                 due_dt.isoformat(), exp.window_id, exp.disease,
                 D.STATUS_PENDING, 1, now, now))
            return True
        except sqlite3.IntegrityError:
            # 槽位已存在：若旧措施被结算/冻结过而期望恢复，将其复活
            # （有 completion 时不会走到这里）；否则跳过
            existing = self.connection.execute(
                "SELECT * FROM measures WHERE plan_id=? AND member_id=? "
                "AND kind=? AND scheduled_date=? "
                "AND COALESCE(disease,'')=?",
                (plan_row["plan_id"], member_row["member_id"], exp.kind,
                 exp.scheduled.isoformat(),
                 exp.disease or "")).fetchone()
            if existing and existing["status"] in (
                    D.STATUS_SUPERSEDED, D.STATUS_HELD, D.STATUS_CANCELLED):
                self.connection.execute(
                    "UPDATE measures SET status=?, generation=generation+1, "
                    "updated_at=? WHERE measure_uid=?",
                    (D.STATUS_PENDING, now, existing["measure_uid"]))
                return True
            return False

    def _compute_due(self, plan_row, exp: D.ExpectedMeasure) -> datetime:
        """每条措施的到期绝对时刻，全部锚定措施当地时区。"""
        if exp.kind == D.MEASURE_VACCINE:
            # 出发前 14 天的居家地上午 9 点
            anchor = _parse_day(plan_row["depart_date"]) - timedelta(
                days=D.VACCINE_LEAD_DAYS)
            return D.as_aware(D.local_datetime(anchor, plan_row["home_tz"], 9))
        if exp.kind == D.MEASURE_CHILD_GROUP_PAUSE:
            # 当日早晨居家地 7:30 提醒家长不要送托/返校
            return D.as_aware(D.local_datetime(
                exp.scheduled, plan_row["home_tz"], 7, 30))
        if exp.kind == D.MEASURE_ADULT_SELF_CHECK:
            return D.as_aware(D.local_datetime(
                exp.scheduled, plan_row["home_tz"], 8))
        if exp.kind == D.MEASURE_HANDWASHING:
            return D.as_aware(D.local_datetime(exp.scheduled, exp.tz_name, 8))
        if exp.kind == D.MEASURE_MOSQUITO:
            # 当日傍晚前提醒（目的地 17 点）
            return D.as_aware(D.local_datetime(exp.scheduled, exp.tz_name, 17))
        return D.as_aware(D.local_datetime(exp.scheduled, exp.tz_name, 9))

    def list_measures(self, plan_id, member_id=None, status=None,
                      advice_class=None):
        self._get_plan_row(plan_id)
        sql = "SELECT * FROM measures WHERE plan_id=?"
        params: list = [plan_id]
        if member_id:
            sql += " AND member_id=?"
            params.append(member_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        if advice_class:
            sql += " AND advice_class=?"
            params.append(advice_class)
        sql += " ORDER BY scheduled_date, kind"
        return [self._measure_dict(r) for r in
                self.connection.execute(sql, params).fetchall()]

    @staticmethod
    def _measure_dict(row):
        return {
            "measure_uid": row["measure_uid"], "plan_id": row["plan_id"],
            "member_id": row["member_id"], "kind": row["kind"],
            "advice_class": row["advice_class"],
            "scheduled_date": row["scheduled_date"],
            "tz_name": row["tz_name"], "due_at": row["due_at"],
            "window_id": row["window_id"], "disease": row["disease"],
            "status": row["status"], "generation": row["generation"],
        }

    # ------------------------------------------------------------------
    # 完成措施（重复同步不得再次触发）
    # ------------------------------------------------------------------

    def complete_measure(self, plan_id, member_id, kind, scheduled_date,
                         request_key, disease=None, note=None):
        scheduled = self._coerce_date(scheduled_date)

        def action():
            member = self.connection.execute(
                "SELECT * FROM members WHERE plan_id=? AND member_id=?",
                (plan_id, member_id)).fetchone()
            if member is None:
                raise TravelHealthError("成员不存在")
            if member["consent_status"] != "granted":
                raise TravelHealthError("授权已撤销，不能登记完成；"
                                        "历史记录仍保留")
            measure = self.connection.execute(
                "SELECT * FROM measures WHERE plan_id=? AND member_id=? "
                "AND kind=? AND scheduled_date=? "
                "AND COALESCE(disease,'')=?",
                (plan_id, member_id, kind, scheduled.isoformat(),
                 disease or "")).fetchone()
            if measure is None:
                raise TravelHealthError("措施不存在")
            now = self.now_iso()
            if measure["status"] != D.STATUS_COMPLETED:
                self.connection.execute(
                    "UPDATE measures SET status=?, generation=generation+1,"
                    " updated_at=? WHERE measure_uid=?",
                    (D.STATUS_COMPLETED, now, measure["measure_uid"]))
            try:
                self.connection.execute(
                    "INSERT INTO completions(plan_id,member_id,kind,"
                    "scheduled_date,disease,request_key,completed_at,note) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (plan_id, member_id, kind, scheduled.isoformat(),
                     disease, request_key, now, note))
            except sqlite3.IntegrityError:
                # 槽位已完成：幂等返回，绝不再次触发提醒/升级
                pass
            self.connection.execute(
                "UPDATE reminders SET status=? WHERE measure_uid=? "
                "AND status IN (?,?)",
                (D.REMINDER_DONE, measure["measure_uid"],
                 D.REMINDER_WAITING, D.REMINDER_SENT))
            self._event(plan_id, "measure_completed", {
                "member_id": member_id, "kind": kind,
                "scheduled_date": scheduled.isoformat(),
                "disease": disease})
            return self._measure_dict(
                self.connection.execute(
                    "SELECT * FROM measures WHERE measure_uid=?",
                    (measure["measure_uid"],)).fetchone())

        return self._idempotent(request_key, action)

    # ------------------------------------------------------------------
    # 提醒扫描（纯状态派生；重启后可重建）
    # ------------------------------------------------------------------

    def _ensure_waiting_reminders(self):
        """为 active 计划中所有已到期 pending 措施补挂 waiting 提醒。
        重启后内存中没有任何调度状态，全部依据持久化措施重建。"""
        now = self.now()
        rows = self.connection.execute(
            "SELECT m.* FROM measures m JOIN plans p ON m.plan_id=p.plan_id "
            "WHERE m.status=? AND p.status=?",
            (D.STATUS_PENDING, D.PLAN_ACTIVE)).fetchall()
        for m in rows:
            if _parse_dt(m["due_at"]) > now:
                continue
            exists = self.connection.execute(
                "SELECT 1 FROM reminders WHERE measure_uid=? "
                "AND status IN (?,?)",
                (m["measure_uid"], D.REMINDER_WAITING,
                 D.REMINDER_SENT)).fetchone()
            if exists:
                continue
            self.connection.execute(
                "INSERT INTO reminders VALUES(?,?,?,?,?,?,?,?,?,?)",
                (_new_id("rem"), m["plan_id"], m["member_id"],
                 m["measure_uid"], m["scheduled_date"], m["due_at"],
                 D.REMINDER_WAITING, None, m["generation"],
                 self.now_iso()))

    def due_reminders(self, mark_sent=False):
        """返回到期待发提醒。``mark_sent=True`` 时在同一事务内标记
        sent；重复调用不会重复发送同一条。"""
        with self.transaction():
            self._ensure_waiting_reminders()
            rows = self.connection.execute(
                "SELECT * FROM reminders WHERE status=? ORDER BY due_at",
                (D.REMINDER_WAITING,)).fetchall()
            result = [self._reminder_dict(r) for r in rows]
            if mark_sent:
                now = self.now_iso()
                for r in rows:
                    self.connection.execute(
                        "UPDATE reminders SET status=?, sent_at=? "
                        "WHERE reminder_id=?",
                        (D.REMINDER_SENT, now, r["reminder_id"]))
                for item in result:
                    item["status"] = D.REMINDER_SENT
                    item["sent_at"] = now
            return result

    def reminder_history(self, plan_id, member_id=None):
        self._get_plan_row(plan_id)
        sql = "SELECT * FROM reminders WHERE plan_id=?"
        params: list = [plan_id]
        if member_id:
            sql += " AND member_id=?"
            params.append(member_id)
        sql += " ORDER BY due_at, reminder_id"
        return [self._reminder_dict(r) for r in
                self.connection.execute(sql, params).fetchall()]

    @staticmethod
    def _reminder_dict(row):
        return {
            "reminder_id": row["reminder_id"], "plan_id": row["plan_id"],
            "member_id": row["member_id"],
            "measure_uid": row["measure_uid"],
            "scheduled_date": row["scheduled_date"],
            "due_at": row["due_at"], "status": row["status"],
            "sent_at": row["sent_at"], "generation": row["generation"],
            "created_at": row["created_at"],
        }

    # ------------------------------------------------------------------
    # 升级（超时未完成 / 症状触发；取消或重启都不丢失）
    # ------------------------------------------------------------------

    def pending_escalations(self, grace: timedelta = D.ESCALATION_GRACE):
        """扫描 sent 且超过宽限期仍未完成的提醒并登记 open 升级。

        幂等：同一提醒同时只有一条 open 升级；重启后重新扫描即可恢复
        全部待处理升级，且不会重复登记。
        """
        with self.transaction():
            self._ensure_waiting_reminders()
            now = self.now()
            # 只为仍在执行中的措施产生新升级；计划取消/措施结算后
            # 已有的 open 升级仍保留，由值班人员显式办结
            rows = self.connection.execute(
                "SELECT r.* FROM reminders r "
                "JOIN measures m ON r.measure_uid=m.measure_uid "
                "JOIN plans p ON r.plan_id=p.plan_id "
                "WHERE r.status=? AND m.status=? AND p.status=?",
                (D.REMINDER_SENT, D.STATUS_PENDING,
                 D.PLAN_ACTIVE)).fetchall()
            created = []
            for r in rows:
                if _parse_dt(r["due_at"]) + grace > now:
                    continue
                open_existing = self.connection.execute(
                    "SELECT 1 FROM escalations WHERE source_ref=? AND status=?",
                    (r["reminder_id"], D.ESCALATION_OPEN)).fetchone()
                if open_existing:
                    continue
                created.append(self._open_escalation_locked(
                    r["plan_id"], r["member_id"], "measure_overdue",
                    source_ref=r["reminder_id"], payload={
                        "measure_uid": r["measure_uid"],
                        "scheduled_date": r["scheduled_date"],
                        "due_at": r["due_at"],
                        "grace_hours": grace.total_seconds() / 3600}))
            return {"created": created,
                    "open": self.list_escalations(status=D.ESCALATION_OPEN)}

    def _open_escalation_locked(self, plan_id, member_id, reason,
                                source_ref=None, payload=None):
        esc_id = _new_id("esc")
        now = self.now_iso()
        self.connection.execute(
            "INSERT INTO escalations VALUES(?,?,?,?,?,?,?,?,?,?)",
            (esc_id, plan_id, member_id, reason, D.ESCALATION_OPEN,
             source_ref, now, None, None, _dumps(payload or {})))
        self._event(plan_id, "escalation_opened", {
            "escalation_id": esc_id, "member_id": member_id,
            "reason": reason, "source_ref": source_ref})
        return {
            "escalation_id": esc_id, "plan_id": plan_id,
            "member_id": member_id, "reason": reason,
            "status": D.ESCALATION_OPEN, "source_ref": source_ref,
            "created_at": now, "payload": payload or {},
        }

    def resolve_escalation(self, escalation_id, operator_id, request_key,
                           resolution=None):
        def action():
            row = self.connection.execute(
                "SELECT * FROM escalations WHERE escalation_id=?",
                (escalation_id,)).fetchone()
            if row is None:
                raise TravelHealthError("升级不存在")
            plan = self._get_plan_row(row["plan_id"])
            self._assert_operator(plan, operator_id)
            if row["status"] == D.ESCALATION_OPEN:
                self.connection.execute(
                    "UPDATE escalations SET status=?, resolved_at=?, "
                    "resolution=? WHERE escalation_id=?",
                    (D.ESCALATION_RESOLVED, self.now_iso(),
                     resolution, escalation_id))
                self._event(row["plan_id"], "escalation_resolved",
                            {"escalation_id": escalation_id,
                             "resolution": resolution})
            return self.get_escalation(escalation_id)

        return self._idempotent(request_key, action)

    def get_escalation(self, escalation_id):
        row = self.connection.execute(
            "SELECT * FROM escalations WHERE escalation_id=?",
            (escalation_id,)).fetchone()
        if row is None:
            raise TravelHealthError("升级不存在")
        return self._escalation_dict(row)

    def list_escalations(self, plan_id=None, status=None):
        sql = "SELECT * FROM escalations WHERE 1=1"
        params: list = []
        if plan_id:
            sql += " AND plan_id=?"
            params.append(plan_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at"
        return [self._escalation_dict(r) for r in
                self.connection.execute(sql, params).fetchall()]

    @staticmethod
    def _escalation_dict(row):
        return {
            "escalation_id": row["escalation_id"],
            "plan_id": row["plan_id"], "member_id": row["member_id"],
            "reason": row["reason"], "status": row["status"],
            "source_ref": row["source_ref"], "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
            "resolution": row["resolution"],
            "payload": json.loads(row["payload"]),
        }

    # ------------------------------------------------------------------
    # 症状报告：就医路径优先
    # ------------------------------------------------------------------

    def report_symptoms(self, plan_id, member_id, symptoms, request_key,
                        reported_at=None, payload=None):
        symptoms = sorted(set(symptoms or []))
        if not symptoms:
            raise TravelHealthError("症状列表不能为空")

        def action():
            plan = self._get_plan_row(plan_id)
            member = self.connection.execute(
                "SELECT * FROM members WHERE plan_id=? AND member_id=?",
                (plan_id, member_id)).fetchone()
            if member is None:
                raise TravelHealthError("成员不存在")
            if member["consent_status"] != "granted":
                raise TravelHealthError("授权已撤销，不能提交症状报告；"
                                        "请先恢复授权或走应急渠道")

            if reported_at is None:
                instant = self.now()
            else:
                instant = reported_at
                if isinstance(instant, str):
                    instant = datetime.fromisoformat(instant)
                instant = D.as_aware(instant)

            return_date_ = _parse_day(plan["return_date"])
            depart = _parse_day(plan["depart_date"])
            home_tz = plan["home_tz"]

            in_mon = D.is_within_monitoring(instant, return_date_, home_tz)
            # 就医路径窗口：出发当日（居家地 0 点）起，至回程后满一个月
            care_start = D.as_aware(D.local_datetime(depart, home_tz))
            _, care_end, _ = D.monitoring_window(return_date_, home_tz)
            in_care_window = care_start <= instant <= care_end
            local_day = D.local_date(instant, home_tz).isoformat()

            windows = self._active_windows(plan_id)

            def covers(w):
                return D.window_covers_trip(
                    w.start_date, w.end_date, depart, return_date_)

            exposed = any(w.risk_level != D.RISK_LOW and covers(w)
                          for w in windows)
            mosquito_exposed = any(
                bool(set(w.diseases) & D.MOSQUITO_DISEASES) and covers(w)
                for w in windows)

            guidance = D.triage_guidance(
                symptoms, exposed_risk_area=exposed,
                in_monitoring=in_care_window,
                mosquito_exposed=mosquito_exposed)
            # care-first：疫区暴露者在行程中或返程监测期内报告任何症状
            care_first = bool(exposed and in_care_window)

            report_id = _new_id("sym")
            escalation_id = None
            if care_first:
                esc = self._open_escalation_locked(
                    plan_id, member_id, "symptom_from_risk_area",
                    source_ref=report_id, payload={
                        "symptoms": symptoms,
                        "reported_at": instant.isoformat(),
                        "local_date": local_day,
                        "guidance": [g.as_dict() for g in guidance]})
                escalation_id = esc["escalation_id"]

            self.connection.execute(
                "INSERT INTO symptom_reports VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (report_id, plan_id, member_id, instant.isoformat(),
                 local_day, _dumps(symptoms), int(exposed),
                 int(in_mon), int(care_first), escalation_id,
                 _dumps(payload or {})))
            self._event(plan_id, "symptoms_reported", {
                "member_id": member_id, "symptoms": symptoms,
                "exposed": exposed, "in_monitoring": in_mon,
                "care_first": care_first,
                "escalation_id": escalation_id})
            return {
                "report_id": report_id, "plan_id": plan_id,
                "member_id": member_id,
                "reported_at": instant.isoformat(),
                "local_date": local_day, "symptoms": symptoms,
                "exposed": exposed, "in_monitoring": in_mon,
                "care_first": care_first,
                "escalation_id": escalation_id,
                "guidance": [g.as_dict() for g in guidance],
            }

        return self._idempotent(request_key, action)

    def list_symptom_reports(self, plan_id, member_id=None):
        self._get_plan_row(plan_id)
        sql = "SELECT * FROM symptom_reports WHERE plan_id=?"
        params: list = [plan_id]
        if member_id:
            sql += " AND member_id=?"
            params.append(member_id)
        sql += " ORDER BY reported_at"
        out = []
        for r in self.connection.execute(sql, params).fetchall():
            out.append({
                "report_id": r["report_id"], "plan_id": r["plan_id"],
                "member_id": r["member_id"],
                "reported_at": r["reported_at"],
                "local_date": r["local_date"],
                "symptoms": json.loads(r["symptoms"]),
                "exposed": bool(r["exposed"]),
                "in_monitoring": bool(r["in_monitoring"]),
                "care_first": bool(r["care_first"]),
                "escalation_id": r["escalation_id"],
                "payload": json.loads(r["payload"]),
            })
        return out
