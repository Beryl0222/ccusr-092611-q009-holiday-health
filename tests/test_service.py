"""端到端业务规则测试：幂等、增量风险重算、年龄分流、时区、重启、升级。"""
import os
import tempfile
import unittest
from datetime import date, timedelta

from holiday_health.domain import (
    FixedClock, RiskLevel, add_months, build_measure_specs, is_child_age,
    local_midnight, parse_local_date,
)
from holiday_health.service import ConflictError, ServiceError, TravelHealthService


def make_service(start="2026-06-01T00:00:00+00:00"):
    return TravelHealthService(clock=FixedClock(start))


def seed_family(svc, destination="Bangkok", tz="Asia/Bangkok",
                dep="2026-07-01", ret="2026-07-10", risk=None):
    plan = svc.create_plan("owner1", destination, tz, dep, ret, request_key="plan-1")
    pid = plan["plan_id"]
    adult = svc.add_member(pid, "owner1", "Dad", "1980-05-01", "father",
                           request_key="m-adult")["member_id"]
    child = svc.add_member(pid, "owner1", "Kid", "2015-06-15", "child",
                           request_key="m-child")["member_id"]
    svc.grant_consent(adult, "owner1", request_key="c-adult")
    svc.grant_consent(child, "owner1", request_key="c-child")
    if risk:
        svc.upsert_risk_window(destination, dep, ret, risk, request_key="risk-1")
    return pid, adult, child


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()

    def tearDown(self):
        self.svc.close()

    def test_repeated_sync_does_not_regenerate(self):
        pid, adult, _child = seed_family(self.svc, risk="low")
        first = self.svc.sync_measures(pid, "owner1", request_key="sync-1")
        # 同一 request_key 重放：返回缓存结果
        replay = self.svc.sync_measures(pid, "owner1", request_key="sync-1")
        self.assertEqual(first, replay)
        # 新 request_key 的重复同步：风险未变，不产生任何新措施或事件
        measures_before = self.svc.list_measures(pid)
        events_before = len(self.svc.history(kinds=["measure_generated"]))
        again = self.svc.sync_measures(pid, "owner1", request_key="sync-2")
        self.assertEqual(again["created"], [])
        self.assertEqual(again["superseded"], [])
        self.assertEqual(len(self.svc.list_measures(pid)), len(measures_before))
        self.assertEqual(len(self.svc.history(kinds=["measure_generated"])), events_before)

    def test_completed_measure_never_retriggers(self):
        pid, adult, _child = seed_family(self.svc, risk="high")
        self.svc.sync_measures(pid, "owner1", request_key="sync-1")
        vaccine = next(m for m in self.svc.list_measures(pid, adult)
                       if m["spec_key"] == "vaccine_pre")
        # 到期后完成
        self.svc.clock.advance(timedelta(days=20))
        done1 = self.svc.complete_measure(vaccine["measure_uid"], "owner1",
                                          request_key="done-1")
        self.assertEqual(done1["status"], "done")
        # 完成请求重放：幂等
        done2 = self.svc.complete_measure(vaccine["measure_uid"], "owner1",
                                          request_key="done-1")
        self.assertEqual(done1, done2)
        # 风险再次变化后同步：已完成的疫苗不产生新修订、不复活
        self.svc.upsert_risk_window("Bangkok", "2026-07-01", "2026-07-10",
                                    "moderate", request_key="risk-2")
        self.svc.sync_measures(pid, "owner1", request_key="sync-2")
        vaccines = [m for m in self.svc.list_measures(pid, adult, include_superseded=True)
                    if m["spec_key"] == "vaccine_pre"]
        self.assertEqual(len(vaccines), 1)
        self.assertEqual(vaccines[0]["status"], "done")
        self.assertEqual(vaccines[0]["completed_at"], done1["completed_at"])

    def test_reminder_delivered_once(self):
        pid, adult, _child = seed_family(self.svc, risk="low")
        self.svc.sync_measures(pid, "owner1", request_key="sync-1")
        self.svc.clock.advance(timedelta(days=30))
        first = self.svc.generate_due_reminders(request_key="rem-1")
        self.assertGreater(len(first["reminders"]), 0)
        replay = self.svc.generate_due_reminders(request_key="rem-1")
        self.assertEqual(first, replay)
        again = self.svc.generate_due_reminders(request_key="rem-2")
        self.assertEqual(again["reminders"], [])  # 重复扫描不重复投递

    def test_risk_upsert_same_level_no_version_bump(self):
        self.svc.upsert_risk_window("Bangkok", "2026-07-01", "2026-07-10",
                                    "high", request_key="r1")
        again = self.svc.upsert_risk_window("Bangkok", "2026-07-01", "2026-07-10",
                                            "high", request_key="r2")
        self.assertEqual(again["risk_version"], 1)


class IncrementalRiskTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()

    def tearDown(self):
        self.svc.close()

    def test_only_affected_windows_recomputed(self):
        pid, adult, child = seed_family(self.svc, risk="low")
        self.svc.sync_measures(pid, "owner1", request_key="sync-1")
        # low 风险下不应有疫苗/防蚊/儿童暂缓
        self.assertFalse(any(m["spec_key"] == "vaccine_pre"
                             for m in self.svc.list_measures(pid)))
        # 风险升至 high：仅受影响的疫苗/防蚊/儿童暂缓生成，洗手与监测不动
        self.svc.upsert_risk_window("Bangkok", "2026-07-01", "2026-07-10",
                                    "high", request_key="risk-hi")
        result = self.svc.sync_measures(pid, "owner1", request_key="sync-2")
        created = self.svc.list_measures(pid, include_superseded=True)
        new_keys = {m["spec_key"] for m in created
                    if m["measure_uid"] in result["created"]}
        self.assertIn("vaccine_pre", new_keys)
        self.assertIn("mosquito_travel", new_keys)
        self.assertIn("child_group_hold", new_keys)
        self.assertNotIn("wash_pre", new_keys)
        self.assertNotIn("monitor_early", new_keys)
        self.assertNotIn("monitor_late", new_keys)

        # 只调整行程中段 7/5-7/8 的风险：
        # - 监测窗口从回程日 7/10 开始，不被该窗口覆盖 -> 不修订；
        before_late = next(m for m in self.svc.list_measures(pid)
                           if m["spec_key"] == "monitor_late")
        self.svc.upsert_risk_window("Bangkok", "2026-07-05", "2026-07-08",
                                    "moderate", request_key="risk-mid")
        result2 = self.svc.sync_measures(pid, "owner1", request_key="sync-3")
        after_late = next(m for m in self.svc.list_measures(pid)
                          if m["spec_key"] == "monitor_late")
        self.assertEqual(before_late["measure_uid"], after_late["measure_uid"])
        # - 行程内防蚊窗口覆盖该段 -> 仅修订受影响窗口
        mosquito_audlts = [m for m in self.svc.list_measures(pid, adult)
                           if m["spec_key"] == "mosquito_travel"]
        self.assertEqual(len(mosquito_audlts), 1)
        self.assertEqual(mosquito_audlts[0]["revision"], 2)
        self.assertIn(mosquito_audlts[0]["measure_uid"], result2["created"])
        # 旧修订标记 superseded
        all_mosq = self.svc.list_measures(pid, adult, include_superseded=True)
        statuses = {m["revision"]: m["status"]
                    for m in all_mosq if m["spec_key"] == "mosquito_travel"}
        self.assertEqual(statuses, {1: "superseded", 2: "pending"})

    def test_risk_downgrade_withdraws_gated_measures(self):
        pid, adult, _child = seed_family(self.svc, risk="high")
        self.svc.sync_measures(pid, "owner1", request_key="sync-1")
        self.assertTrue(any(m["spec_key"] == "vaccine_pre"
                            for m in self.svc.list_measures(pid)))
        self.svc.upsert_risk_window("Bangkok", "2026-07-01", "2026-07-10",
                                    "low", request_key="risk-low")
        result = self.svc.sync_measures(pid, "owner1", request_key="sync-2")
        self.assertGreater(len(result["superseded"]), 0)
        self.assertFalse(any(m["spec_key"] == "vaccine_pre"
                             for m in self.svc.list_measures(pid)))


class AudienceSeparationTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()

    def tearDown(self):
        self.svc.close()

    def test_child_hold_and_adult_advisory_are_separate_records(self):
        pid, adult, child = seed_family(self.svc, risk="moderate")
        self.svc.sync_measures(pid, "owner1", request_key="sync-1")
        child_measures = self.svc.list_measures(pid, child)
        adult_measures = self.svc.list_measures(pid, adult)
        holds = [m for m in child_measures if m["kind"] == "child_group_hold"]
        advisories = [m for m in adult_measures if m["kind"] == "adult_advisory"]
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds[0]["audience"], "child")
        self.assertEqual(len(advisories), 1)
        self.assertEqual(advisories[0]["audience"], "adult")
        # 成人没有暂缓集体活动条目，儿童没有成人建议条目
        self.assertFalse(any(m["kind"] == "child_group_hold" for m in adult_measures))
        self.assertFalse(any(m["kind"] == "adult_advisory" for m in child_measures))
        # 完成成人建议不影响儿童暂缓
        self.svc.complete_measure(advisories[0]["measure_uid"], "owner1",
                                  request_key="adv-done")
        hold = next(m for m in self.svc.list_measures(pid, child)
                    if m["kind"] == "child_group_hold")
        self.assertEqual(hold["status"], "pending")

    def test_age_classification_at_departure(self):
        # 出发当日恰好满 18 岁 -> 成人
        self.assertFalse(is_child_age(date(2008, 7, 1), date(2026, 7, 1)))
        # 出发前一天满 18，按出发日算仍是... 出发日已过生，成人
        self.assertTrue(is_child_age(date(2008, 7, 2), date(2026, 7, 1)))


class TimezoneAndMonitoringWindowTests(unittest.TestCase):
    def test_local_midnight_anchoring(self):
        from datetime import timezone
        # 东京 7/1 00:00 锚定当地日历日，换算 UTC 为 6/30 15:00，不错位
        dt = local_midnight(date(2026, 7, 1), "Asia/Tokyo")
        self.assertEqual(dt.utcoffset(), timedelta(hours=9))
        self.assertEqual(dt.astimezone(timezone.utc).isoformat(),
                         "2026-06-30T15:00:00+00:00")

    def test_add_months_calendar_clamping(self):
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(add_months(date(2026, 7, 10), 1), date(2026, 8, 10))

    def test_monitoring_spans_one_calendar_month_without_gap_or_overlap(self):
        svc = make_service()
        pid, adult, _child = seed_family(svc, dep="2026-01-31", ret="2026-02-20",
                                         risk="low")
        svc.sync_measures(pid, "owner1", request_key="sync-1")
        measures = svc.list_measures(pid, adult)
        early = next(m for m in measures if m["spec_key"] == "monitor_early")
        late = next(m for m in measures if m["spec_key"] == "monitor_late")
        # 早段：回程起 14 天；晚段次日开始，直到满一个自然月前一天
        self.assertEqual(early["window_start"], "2026-02-20")
        self.assertEqual(early["window_end"], "2026-03-05")
        self.assertEqual(late["window_start"], "2026-03-06")
        self.assertEqual(late["window_end"], "2026-03-19")  # 满一个月日 3/20 不含
        svc.close()

    def test_due_at_uses_destination_timezone(self):
        svc = make_service()
        pid, adult, _child = seed_family(svc, tz="Asia/Tokyo", risk="low")
        svc.sync_measures(pid, "owner1", request_key="sync-1")
        wash = next(m for m in svc.list_measures(pid, adult)
                    if m["spec_key"] == "wash_pre")
        # 到期当地日 D-3 = 6/28，东京零点 = UTC 6/27 15:00
        self.assertEqual(wash["due_at"], "2026-06-27T15:00:00+00:00")
        svc.close()

    def test_symptom_outside_monitoring_window_rejected(self):
        svc = make_service()
        pid, adult, _child = seed_family(svc, risk="low")
        svc.sync_measures(pid, "owner1", request_key="sync-1")
        # 回程 7/10，满一个月为 8/10；8/10 当天的症状超出窗口
        with self.assertRaises(ServiceError):
            svc.report_symptoms(adult, ["headache"], "2026-08-10T09:00:00+07:00",
                                actor_id="owner1", request_key="sym-late")
        # 8/9 仍在窗口内
        ok = svc.report_symptoms(adult, ["headache"], "2026-08-09T23:00:00+07:00",
                                 actor_id="owner1", request_key="sym-ok")
        self.assertIsNone(ok["escalation_id"])
        svc.close()


class SymptomEscalationTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        self.pid, self.adult, self.child = seed_family(self.svc, risk="high")
        self.svc.sync_measures(self.pid, "owner1", request_key="sync-1")

    def tearDown(self):
        self.svc.close()

    def test_risk_area_fever_opens_care_pathway(self):
        rep = self.svc.report_symptoms(
            self.child, ["fever", "cough"], "2026-07-12T20:00:00+07:00",
            note="回程后发烧", actor_id="owner1", request_key="sym-fever")
        self.assertTrue(rep["from_risk_area"])
        self.assertIsNotNone(rep["escalation_id"])
        self.assertGreaterEqual(len(rep["care_pathway"]), 3)
        # 升级记录可查且为 open
        escs = self.svc.list_escalations("open")
        self.assertEqual(len(escs), 1)
        self.assertEqual(escs[0]["escalation_id"], rep["escalation_id"])
        self.assertEqual(escs[0]["level"], "urgent_care")

    def test_care_pathway_ranks_before_normal_tips(self):
        # 回程后儿童发烧
        self.svc.report_symptoms(
            self.child, ["fever"], "2026-07-12T20:00:00+07:00",
            actor_id="owner1", request_key="sym-fever")
        pending = self.svc.pending_reminders()
        self.assertGreater(len(pending), 0)
        # 该儿童（有 open 升级）的所有待办排在最前，先于其他成员普通提示
        first_owner = pending[0]["member_id"]
        self.assertEqual(first_owner, self.child)
        adult_index = next(i for i, m in enumerate(pending)
                           if m["member_id"] == self.adult)
        child_indexes = [i for i, m in enumerate(pending)
                         if m["member_id"] == self.child]
        self.assertLess(max(child_indexes), adult_index)

    def test_non_risk_symptoms_do_not_escalate(self):
        svc = make_service()
        pid, adult, _ = seed_family(svc, destination="Osaka", risk="low")
        svc.sync_measures(pid, "owner1", request_key="sync-1")
        rep = svc.report_symptoms(adult, ["fever"], "2026-07-12T20:00:00+09:00",
                                  actor_id="owner1", request_key="sym-x")
        self.assertFalse(rep["from_risk_area"])
        self.assertIsNone(rep["escalation_id"])
        self.assertEqual(svc.list_escalations(), [])
        svc.close()

    def test_risk_exposure_inherited_after_return(self):
        # 行程末日 high，回程两周后发烧仍视为疫区暴露
        rep = self.svc.report_symptoms(
            self.adult, ["diarrhea"], "2026-07-24T08:00:00+07:00",
            actor_id="owner1", request_key="sym-late-fever")
        self.assertTrue(rep["from_risk_area"])
        self.assertIsNotNone(rep["escalation_id"])

    def test_escalation_lifecycle(self):
        rep = self.svc.report_symptoms(
            self.child, ["fever"], "2026-07-12T20:00:00+07:00",
            actor_id="owner1", request_key="sym-1")
        esc_id = rep["escalation_id"]
        ack = self.svc.acknowledge_escalation(esc_id, actor_id="duty-1",
                                              request_key="ack-1")
        self.assertEqual(ack["status"], "acknowledged")
        # 重复确认幂等
        self.assertEqual(
            self.svc.acknowledge_escalation(esc_id, actor_id="duty-1",
                                            request_key="ack-1"), ack)
        resolved = self.svc.resolve_escalation(esc_id, actor_id="duty-1",
                                               request_key="res-1")
        self.assertEqual(resolved["status"], "resolved")


class PermissionAndCancellationTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        self.pid, self.adult, self.child = seed_family(self.svc, risk="high")
        self.svc.sync_measures(self.pid, "owner1", request_key="sync-1")

    def tearDown(self):
        self.svc.close()

    def test_non_owner_rejected(self):
        with self.assertRaises(ServiceError):
            self.svc.sync_measures(self.pid, "intruder", request_key="x1")
        with self.assertRaises(ServiceError):
            self.svc.cancel_plan(self.pid, "intruder", request_key="x2")

    def test_revoked_consent_skips_member_and_voids_future(self):
        self.svc.clock.advance(timedelta(days=5))  # 仍在出发前
        self.svc.revoke_consent(self.child, "owner1", request_key="rev-1")
        # 未到期的待办被作废
        live = self.svc.list_measures(self.pid, self.child)
        self.assertEqual(live, [])
        # 再次同步跳过该成员，不生成新措施
        self.svc.upsert_risk_window("Bangkok", "2026-07-01", "2026-07-10",
                                    "moderate", request_key="risk-2")
        result = self.svc.sync_measures(self.pid, "owner1", request_key="sync-2")
        self.assertIn(self.child, result["skipped_unauthorized"])
        self.assertEqual(self.svc.list_measures(self.pid, self.child), [])
        # 撤销事件与作废历史仍可解释
        events = self.svc.history(self.child)
        kinds = {e["kind"] for e in events}
        self.assertIn("consent_revoked", kinds)

    def test_regrant_restarts_measures(self):
        self.svc.revoke_consent(self.child, "owner1", request_key="rev-1")
        self.svc.grant_consent(self.child, "owner1", request_key="regrant-1")
        self.svc.sync_measures(self.pid, "owner1", request_key="sync-2")
        self.assertTrue(any(m["kind"] == "child_group_hold"
                            for m in self.svc.list_measures(self.pid, self.child)))

    def test_cancel_plan_voids_future_keeps_history(self):
        self.svc.cancel_plan(self.pid, "owner1", request_key="cancel-1")
        plan = self.svc.get_plan(self.pid)
        self.assertEqual(plan["status"], "cancelled")
        # 出发前所有待办都在未来 -> 全部作废
        self.assertEqual(self.svc.list_measures(self.pid), [])
        # 取消后同步不再产生措施
        result = self.svc.sync_measures(self.pid, "owner1", request_key="sync-x")
        self.assertEqual(result["created"], [])
        # 历史事件保留，可解释取消
        kinds = {e["kind"] for e in self.svc.history(self.pid)}
        self.assertIn("plan_cancelled", kinds)
        self.assertIn("measures_synced", kinds)

    def test_optimistic_version_conflict(self):
        with self.assertRaises(ConflictError):
            self.svc.cancel_plan(self.pid, "owner1", request_key="c1",
                                 expected_version=99)


class RestartRecoveryTests(unittest.TestCase):
    def test_state_and_pending_work_survive_restart(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            svc = TravelHealthService(database=path,
                                      clock=FixedClock("2026-06-01T00:00:00+00:00"))
            pid, adult, child = seed_family(svc, risk="high")
            svc.sync_measures(pid, "owner1", request_key="sync-1")
            svc.clock.advance(timedelta(days=30))
            svc.generate_due_reminders(request_key="rem-1")
            # 回程后发烧 -> open 升级
            svc.report_symptoms(child, ["fever"], "2026-07-12T20:00:00+07:00",
                                actor_id="owner1", request_key="sym-1")
            pending_uids = {m["measure_uid"] for m in svc.pending_reminders()}
            svc.close()

            # “重启”：新实例、新时钟（系统时间不影响待办判定的持久状态）
            svc2 = TravelHealthService(database=path,
                                       clock=FixedClock("2026-07-13T00:00:00+00:00"))
            handoff = svc2.duty_handoff()
            self.assertEqual(handoff["escalation_count"], 1)
            self.assertEqual(handoff["open_escalations"][0]["status"], "open")
            self.assertTrue(pending_uids.issubset(
                {m["measure_uid"] for m in handoff["pending_reminders"]}))
            # 继续执行未完成事项
            target = handoff["pending_reminders"][-1]["measure_uid"]
            done = svc2.complete_measure(target, "owner1", request_key="done-after-restart")
            self.assertEqual(done["status"], "done")
            # 值班人员继续处置升级
            esc = handoff["open_escalations"][0]["escalation_id"]
            svc2.acknowledge_escalation(esc, actor_id="duty-2", request_key="ack-2")
            self.assertEqual(svc2.list_escalations("open"), [])
            # 历史提醒可解释：投递事件仍在
            delivered = svc2.history(kinds=["reminder_delivered"])
            self.assertGreater(len(delivered), 0)
            svc2.close()
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
