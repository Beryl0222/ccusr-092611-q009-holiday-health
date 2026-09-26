"""旅行健康编排服务测试。

覆盖：时区锚定与自然月监测、措施派生与儿童/成人分类、幂等同步、
风险变化只重算受影响窗口、授权撤销/计划取消/重启后的可解释性与续办、
超时升级，以及疫区返回者症状就医路径优先。
"""
import json
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from holiday_health import domain as D
from holiday_health.travel import Clock, TravelHealthError, TravelHealthService

HOME_TZ = "Asia/Shanghai"        # UTC+8，无夏令时
DEST_TZ = "America/Los_Angeles"  # 有夏令时，用于验证跨时区锚定


class FixedClock(Clock):
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs):
        self.value = self.value + timedelta(**kwargs)
        return self.value


def dt(y, m, d, hh=0, mm=0, ss=0, tz="UTC"):
    from zoneinfo import ZoneInfo
    zone = timezone.utc if tz == "UTC" else ZoneInfo(tz)
    return datetime(y, m, d, hh, mm, ss, tzinfo=zone)


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.clock = FixedClock(dt(2026, 1, 1, 0, 0))
        self.svc = TravelHealthService(clock=self.clock)
        self.make_plan()

    def tearDown(self):
        self.svc.close()

    def make_plan(self, plan="p1", depart="2026-03-01",
                  returning="2026-03-10", home_tz=HOME_TZ, dest_tz=DEST_TZ):
        return self.svc.create_plan(
            plan, "op1", "某疫区城市", dest_tz, home_tz,
            depart, returning, f"req-create-{plan}")

    def activate(self, plan="p1"):
        return self.svc.activate_plan(plan, "op1", f"req-activate-{plan}")

    member_seq = 0

    def add_member(self, mid, category, plan="p1", consent="granted"):
        ServiceTestBase.member_seq += 1
        return self.svc.upsert_member(
            plan, mid, category, mid, consent, "op1",
            f"req-member-{plan}-{mid}-n{ServiceTestBase.member_seq}")

    win_seq = 0

    def add_window(self, wid="w1", level=D.RISK_HIGH,
                   diseases=("dengue",), start="2026-03-01",
                   end="2026-03-10", plan="p1"):
        # 每次更新对应一次新的客户端提交，使用新请求键；完全重复的
        # 提交由专门用例验证幂等
        ServiceTestBase.win_seq += 1
        return self.svc.upsert_risk_window(
            plan, wid, "城区", start, end, level, list(diseases),
            "op1",
            f"req-window-{plan}-{wid}-n{ServiceTestBase.win_seq}")

    def kinds(self, plan="p1", member=None, **filters):
        return {(m["kind"], m["scheduled_date"], m["disease"])
                for m in self.svc.list_measures(plan, member, **filters)}


class TimeAndDerivationTests(ServiceTestBase):
    def test_calendar_month_and_monitoring_window(self):
        # 自然月而非固定 30 天，月末钳位
        self.assertEqual(D.add_calendar_month(
            __import__("datetime").date(2026, 1, 31), 1),
            __import__("datetime").date(2026, 2, 28))
        self.assertEqual(D.add_calendar_month(
            __import__("datetime").date(2026, 3, 31), 1),
            __import__("datetime").date(2026, 4, 30))
        # 回程 3/10（上海）→ 监测截止 4/10 23:59:59.999 上海
        start, end, end_day = D.monitoring_window(
            __import__("datetime").date(2026, 3, 10), HOME_TZ)
        self.assertEqual(end_day.isoformat(), "2026-04-10")
        # 4/10 当天晚上（上海）仍在监测期；对应 UTC 是 4/10 下午
        self.assertTrue(D.is_within_monitoring(
            dt(2026, 4, 10, 15, 59), __import__("datetime").date(2026, 3, 10),
            HOME_TZ))
        # 上海 4/11 00:30 = UTC 4/10 16:30，已超出：不能按 UTC 日期误判
        self.assertFalse(D.is_within_monitoring(
            dt(2026, 4, 10, 16, 30), __import__("datetime").date(2026, 3, 10),
            HOME_TZ))

    def test_routine_and_window_measures_with_timezone_due(self):
        self.activate()
        self.add_member("adult1", D.MEMBER_ADULT)
        self.add_member("child1", D.MEMBER_CHILD)
        self.add_window()
        measures = self.svc.list_measures("p1")
        by_slot = {(m["member_id"], m["kind"], m["scheduled_date"],
                    m["disease"]): m for m in measures}

        # 洗手：行程内每天，锚定目的地时区 08:00
        hw = by_slot[("adult1", D.MEASURE_HANDWASHING, "2026-03-05", None)]
        self.assertEqual(hw["tz_name"], DEST_TZ)
        self.assertEqual(
            datetime.fromisoformat(hw["due_at"]),
            D.local_datetime(__import__("datetime").date(2026, 3, 5),
                             DEST_TZ, 8))
        # 行程外没有洗手措施
        self.assertNotIn(
            ("adult1", D.MEASURE_HANDWASHING, "2026-02-28", None), by_slot)

        # 防蚊：只在窗口与行程相交日，傍晚 17:00 目的地时间
        mos = by_slot[("adult1", D.MEASURE_MOSQUITO, "2026-03-05", None)]
        self.assertEqual(mos["window_id"], "w1")
        self.assertEqual(
            datetime.fromisoformat(mos["due_at"]),
            D.local_datetime(__import__("datetime").date(2026, 3, 5),
                             DEST_TZ, 17))

        # 高风险黄热病 → 出发前 14 天居家地 09:00 的疫苗措施
        self.add_window("wyf", D.RISK_HIGH, ["yellow_fever"])
        measures = self.svc.list_measures("p1", "adult1")
        vac = next(m for m in measures if m["kind"] == D.MEASURE_VACCINE)
        self.assertEqual(vac["scheduled_date"], "2026-03-01")
        self.assertEqual(vac["disease"], "yellow_fever")
        self.assertEqual(vac["tz_name"], HOME_TZ)
        self.assertEqual(
            datetime.fromisoformat(vac["due_at"]),
            D.local_datetime(__import__("datetime").date(2026, 2, 15),
                             HOME_TZ, 9))

        # elevated + 登革热有防蚊无疫苗；low 什么都不派生
        self.add_window("wlow", D.RISK_LOW, ["dengue"],
                        start="2026-03-08", end="2026-03-09")
        low_mos = [m for m in self.svc.list_measures("p1")
                   if m["window_id"] == "wlow"]
        self.assertEqual(low_mos, [])

    def test_child_and_adult_advice_recorded_separately(self):
        self.activate()
        self.add_member("child1", D.MEMBER_CHILD)
        self.add_member("adult1", D.MEMBER_ADULT)
        self.add_window()

        child_pause = self.svc.list_measures(
            "p1", "child1", advice_class=D.ADVICE_CHILD_GROUP)
        adult_advice = self.svc.list_measures(
            "p1", "adult1", advice_class=D.ADVICE_ADULT)
        general = self.svc.list_measures(
            "p1", advice_class=D.ADVICE_GENERAL)

        # 分类互不混染
        self.assertTrue(child_pause)
        self.assertTrue(adult_advice)
        self.assertTrue(all(m["member_id"] != "adult1" for m in child_pause))
        self.assertTrue(all(m["advice_class"] != D.ADVICE_CHILD_GROUP
                            for m in adult_advice))
        self.assertTrue(all(m["kind"] in (
            D.MEASURE_HANDWASHING, D.MEASURE_MOSQUITO, D.MEASURE_VACCINE)
            for m in general))

        # 儿童暂缓集体活动：回程当日起 14 个自然日
        pause_days = sorted(m["scheduled_date"] for m in child_pause)
        self.assertEqual(pause_days[0], "2026-03-10")
        self.assertEqual(pause_days[-1], "2026-03-23")
        self.assertEqual(len(pause_days), 14)
        self.assertEqual(datetime.fromisoformat(child_pause[0]["due_at"]),
                         D.local_datetime(
                             __import__("datetime").date(2026, 3, 10),
                             HOME_TZ, 7, 30))

        # 成人建议：每日自评持续到回程后一个自然月
        check_days = sorted(m["scheduled_date"] for m in adult_advice)
        self.assertEqual(check_days[0], "2026-03-10")
        self.assertEqual(check_days[-1], "2026-04-10")

        # 未暴露于风险窗口的成员没有返程监测建议
        self.make_plan("p2")
        self.activate("p2")
        self.add_member("adult2", D.MEMBER_ADULT, plan="p2")
        self.assertEqual(
            self.svc.list_measures("p2", "adult2",
                                   advice_class=D.ADVICE_ADULT), [])

    def test_window_partial_overlap_only_creates_overlap_days(self):
        self.activate()
        self.add_member("adult1", D.MEMBER_ADULT)
        # 窗口只覆盖行程尾部两天
        self.add_window("w1", D.RISK_ELEVATED, ["dengue"],
                        start="2026-03-09", end="2026-03-20")
        days = sorted(m["scheduled_date"] for m in self.svc.list_measures(
            "p1", "adult1") if m["kind"] == D.MEASURE_MOSQUITO)
        self.assertEqual(days, ["2026-03-09", "2026-03-10"])


class ReminderAndIdempotencyTests(ServiceTestBase):
    def test_due_scan_send_once_and_completion_blocks_retrigger(self):
        self.activate()
        self.add_member("adult1", D.MEMBER_ADULT)
        self.add_window("w1", D.RISK_HIGH, ["dengue", "yellow_fever"])
        # 时间在出发前：无到期提醒
        self.clock.value = dt(2026, 2, 1)
        self.assertEqual(self.svc.due_reminders(), [])

        # 疫苗到期（2/15 09:00 上海 = 2/15 01:00 UTC）
        self.clock.value = dt(2026, 2, 15, 1, 30)
        first = self.svc.due_reminders(mark_sent=True)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["status"], D.REMINDER_SENT)
        # 再次同步/扫描不会重复发送
        self.svc.sync_measures("p1", reason="repeat")
        again = self.svc.due_reminders(mark_sent=True)
        self.assertEqual(again, [])

        vac = next(m for m in self.svc.list_measures("p1", "adult1")
                   if m["kind"] == D.MEASURE_VACCINE)
        self.svc.complete_measure(
            "p1", "adult1", D.MEASURE_VACCINE,
            vac["scheduled_date"], "req-complete-yf",
            disease=vac["disease"])
        # 完成后重复同步：不复活、不新增提醒
        stats = self.svc.sync_measures("p1", reason="after-complete")
        self.assertGreaterEqual(stats["skipped_completed"], 1)
        self.assertEqual(stats["created"], 0)
        self.assertEqual(self.svc.due_reminders(), [])
        # 重复提交完成请求幂等
        again_done = self.svc.complete_measure(
            "p1", "adult1", D.MEASURE_VACCINE,
            vac["scheduled_date"], "req-complete-yf",
            disease=vac["disease"])
        self.assertEqual(again_done["status"], D.STATUS_COMPLETED)
        self.assertEqual(again_done["measure_uid"], vac["measure_uid"])

    def test_duplicate_write_request_key_returns_first_result(self):
        a = self.svc.create_plan(
            "px", "op1", "X", DEST_TZ, HOME_TZ,
            "2026-05-01", "2026-05-05", "dup-key")
        b = self.svc.create_plan(
            "px", "op1", "X", DEST_TZ, HOME_TZ,
            "2026-05-01", "2026-05-05", "dup-key")
        self.assertEqual(a, b)

    def test_overdue_escalation_idempotent_and_completion_closes_reminder(self):
        self.activate()
        self.add_member("adult1", D.MEMBER_ADULT)
        self.add_window("w1", D.RISK_HIGH, ["yellow_fever"])
        self.clock.value = dt(2026, 2, 15, 1, 30)
        self.svc.due_reminders(mark_sent=True)

        # 未过宽限期：无升级
        before = self.svc.pending_escalations()
        self.assertEqual(before["created"], [])
        self.assertEqual(before["open"], [])

        # 超过 24 小时仍未完成 → 升级
        self.clock.value = dt(2026, 2, 16, 2, 0)
        scan1 = self.svc.pending_escalations()
        self.assertEqual(len(scan1["created"]), 1)
        esc_id = scan1["created"][0]["escalation_id"]
        # 再扫不重复登记
        scan2 = self.svc.pending_escalations()
        self.assertEqual(scan2["created"], [])
        self.assertEqual(len(scan2["open"]), 1)

        # 完成措施：提醒办结，但升级仍待值班人员显式处理
        vac = next(m for m in self.svc.list_measures("p1", "adult1")
                   if m["kind"] == D.MEASURE_VACCINE)
        self.svc.complete_measure(
            "p1", "adult1", D.MEASURE_VACCINE, vac["scheduled_date"],
            "req-done", disease=vac["disease"])
        history = self.svc.reminder_history("p1")
        self.assertTrue(all(r["status"] == D.REMINDER_DONE
                            for r in history if r["measure_uid"] ==
                            vac["measure_uid"]))
        still_open = self.svc.list_escalations(status=D.ESCALATION_OPEN)
        self.assertEqual([e["escalation_id"] for e in still_open], [esc_id])
        resolved = self.svc.resolve_escalation(
            esc_id, "op1", "req-resolve", resolution="已电话确认补种")
        self.assertEqual(resolved["status"], D.ESCALATION_RESOLVED)


class RiskRecalculationTests(ServiceTestBase):
    def test_risk_change_only_recomputes_affected_window(self):
        self.activate()
        self.add_member("adult1", D.MEMBER_ADULT)
        self.add_window("w1", D.RISK_HIGH, ["dengue"])
        self.add_window("w2", D.RISK_ELEVATED, ["malaria"],
                        start="2026-03-05", end="2026-03-08")
        before = {m["measure_uid"]: m for m in self.svc.list_measures("p1")}

        w1_only_days = {"2026-03-01", "2026-03-02", "2026-03-03",
                        "2026-03-04", "2026-03-09", "2026-03-10"}
        w2_days = {"2026-03-05", "2026-03-06",
                   "2026-03-07", "2026-03-08"}

        def mos(day_set):
            return {uid: m for uid, m in before.items()
                    if m["kind"] == D.MEASURE_MOSQUITO
                    and m["scheduled_date"] in day_set}

        w1_only_before = mos(w1_only_days)
        w2_before = mos(w2_days)
        self.assertEqual(len(w1_only_before), 6)
        self.assertEqual(len(w2_before), 4)

        # w1 降级为 low：w1 独有日措施结算；w2 覆盖日措施保留待办
        self.add_window("w1", D.RISK_LOW, ["dengue"])
        after = {m["measure_uid"]: m for m in self.svc.list_measures("p1")}
        for uid in w1_only_before:
            self.assertEqual(after[uid]["status"], D.STATUS_SUPERSEDED)
        for uid, m in w2_before.items():
            self.assertIn(uid, after)
            self.assertEqual(after[uid]["status"], D.STATUS_PENDING)
            self.assertEqual(after[uid]["window_id"], "w2")
        # 常规措施（洗手、成人自评）uid 完全不动
        routine_before = {uid: m for uid, m in before.items()
                          if m["window_id"] is None}
        self.assertTrue(routine_before)
        for uid, m in routine_before.items():
            self.assertEqual(after[uid]["status"], D.STATUS_PENDING)

        # 已结算措施仍可被值班人员解释，且全部源自 w1
        superseded = self.svc.list_measures(
            "p1", status=D.STATUS_SUPERSEDED)
        self.assertEqual(len(superseded), 6)
        self.assertTrue(all(m["window_id"] == "w1" for m in superseded))

        # 风险恢复：w1 独有日措施复活（同一槽位），w2 覆盖日仍只有一条
        self.add_window("w1", D.RISK_HIGH, ["dengue"])
        revived = [m for m in self.svc.list_measures("p1")
                   if m["kind"] == D.MEASURE_MOSQUITO
                   and m["scheduled_date"] in w1_only_days]
        self.assertEqual(len(revived), 6)
        self.assertTrue(all(m["status"] == D.STATUS_PENDING for m in revived))
        w2_days_after = [m for m in self.svc.list_measures("p1")
                         if m["kind"] == D.MEASURE_MOSQUITO
                         and m["scheduled_date"] in w2_days]
        self.assertEqual(len(w2_days_after), 4)
    def test_remove_window_reassigns_overlap_measure(self):
        self.activate()
        self.add_member("adult1", D.MEMBER_ADULT)
        self.add_window("w1", D.RISK_ELEVATED, ["dengue"],
                        start="2026-03-03", end="2026-03-07")
        self.add_window("w2", D.RISK_ELEVATED, ["dengue"],
                        start="2026-03-05", end="2026-03-09")
        # 重叠日 3/5–3/7 的防蚊措施物理上只有一条
        overlap = [m for m in self.svc.list_measures("p1")
                   if m["kind"] == D.MEASURE_MOSQUITO
                   and m["scheduled_date"] in
                   ("2026-03-05", "2026-03-06", "2026-03-07")]
        self.assertEqual(len(overlap), 3)

        self.svc.remove_risk_window("p1", "w1", "op1", "req-rm-w1")
        remaining = {m["scheduled_date"]: m for m in
                     self.svc.list_measures("p1")
                     if m["kind"] == D.MEASURE_MOSQUITO
                     and m["status"] == D.STATUS_PENDING}
        # w1 独有的 3/3–3/4 被结算；重叠日措施改挂 w2 且仍待办
        self.assertNotIn("2026-03-03", remaining)
        self.assertNotIn("2026-03-04", remaining)
        for day in ("2026-03-05", "2026-03-06", "2026-03-07"):
            self.assertEqual(remaining[day]["window_id"], "w2")
            self.assertEqual(remaining[day]["status"], D.STATUS_PENDING)
        # 完成重叠措施只需一次，任何同步都不再触发
        self.svc.complete_measure(
            "p1", "adult1", D.MEASURE_MOSQUITO, "2026-03-05",
            "req-mos-done")
        self.svc.sync_measures("p1", reason="post-complete")
        self.assertEqual(
            [m for m in self.svc.list_measures("p1")
             if m["kind"] == D.MEASURE_MOSQUITO
             and m["scheduled_date"] == "2026-03-05"
             and m["status"] == D.STATUS_PENDING], [])


class AuthorizationCancellationRestartTests(ServiceTestBase):
    def test_revoke_consent_holds_work_then_regrant_resumes(self):
        self.activate()
        self.add_member("adult1", D.MEMBER_ADULT)
        self.add_window("w1", D.RISK_HIGH, ["yellow_fever"])
        self.clock.value = dt(2026, 2, 15, 1, 30)
        sent = self.svc.due_reminders(mark_sent=True)
        self.assertEqual(len(sent), 1)
        # 还有更多等待中的未来提醒
        waiting = self.svc.reminder_history("p1")

        self.svc.upsert_member(
            "p1", "adult1", D.MEMBER_ADULT, "adult1", "revoked", "op1",
            "req-revoke")
        held = self.svc.list_measures("p1", "adult1", status=D.STATUS_HELD)
        self.assertTrue(held)
        # 等待中的提醒撤回；已发提醒作为历史保留
        statuses = {r["status"] for r in self.svc.reminder_history("p1")}
        self.assertNotIn(D.REMINDER_WAITING, statuses)
        self.assertIn(D.REMINDER_SENT, statuses)
        # 撤销期间不再产生任何提醒
        self.clock.value = dt(2026, 3, 5, 10)
        self.assertEqual(self.svc.due_reminders(), [])
        # 撤销期间不能登记完成或症状
        with self.assertRaises(TravelHealthError):
            self.svc.complete_measure(
                "p1", "adult1", D.MEASURE_HANDWASHING, "2026-03-05",
                "req-blocked")
        with self.assertRaises(TravelHealthError):
            self.svc.report_symptoms(
                "p1", "adult1", ["fever"], "req-sym-blocked")

        # 重新授权：冻结措施恢复待办并继续提醒
        self.svc.upsert_member(
            "p1", "adult1", D.MEMBER_ADULT, "adult1", "granted", "op1",
            "req-regrant")
        pending = self.svc.list_measures("p1", "adult1",
                                         status=D.STATUS_PENDING)
        self.assertTrue(pending)
        due = self.svc.due_reminders()
        self.assertTrue(due)

    def test_cancel_plan_retains_history_and_open_escalations(self):
        self.activate()
        self.add_member("adult1", D.MEMBER_ADULT)
        self.add_window("w1", D.RISK_HIGH, ["yellow_fever"])
        self.clock.value = dt(2026, 2, 15, 1, 30)
        self.svc.due_reminders(mark_sent=True)
        self.clock.value = dt(2026, 2, 16, 2, 0)
        esc = self.svc.pending_escalations()["created"][0]

        self.svc.cancel_plan("p1", "op1", "req-cancel", reason="行程取消")
        # 历史提醒仍在，开放升级仍可解释、可办结
        history = self.svc.reminder_history("p1")
        self.assertTrue(any(r["status"] == D.REMINDER_SENT for r in history))
        self.assertEqual(
            self.svc.get_escalation(esc["escalation_id"])["status"],
            D.ESCALATION_OPEN)
        self.svc.resolve_escalation(
            esc["escalation_id"], "op1", "req-resolve-after-cancel",
            resolution="取消后电话确认无需处理")
        # 取消后不再产生新提醒
        self.clock.value = dt(2026, 3, 20, 0, 0)
        self.assertEqual(self.svc.due_reminders(), [])
        # 事件流可以解释整个过程
        kinds = [e["kind"] for e in self.svc.plan_events("p1")]
        self.assertIn("plan_cancelled", kinds)
        self.assertIn("escalation_opened", kinds)

    def test_restart_rebuilds_reminders_and_escalations(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbpath = str(Path(tmp) / "hh.db")
            svc = TravelHealthService(database=dbpath, clock=self.clock)
            self.svc.close()
            self.svc = svc
            # 文件库是全新状态，需要在此完整建计划/成员/窗口
            svc.create_plan(
                "p1", "op1", "某疫区城市", DEST_TZ, HOME_TZ,
                "2026-03-01", "2026-03-10", "req-create-p1")
            self.activate()
            self.add_member("adult1", D.MEMBER_ADULT)
            self.add_window("w1", D.RISK_HIGH, ["yellow_fever"])
            self.clock.value = dt(2026, 2, 15, 1, 30)
            svc.due_reminders(mark_sent=True)
            self.clock.value = dt(2026, 2, 16, 2, 0)
            svc.pending_escalations()
            svc.close()

            # 服务重启：内存调度状态全空，全部从磁盘重建
            restarted = TravelHealthService(database=dbpath, clock=self.clock)
            self.assertEqual(
                len(restarted.due_reminders()), 0)  # 已发送的不重发
            open_esc = restarted.list_escalations(status=D.ESCALATION_OPEN)
            self.assertEqual(len(open_esc), 1)
            # 历史提醒与事件仍可解释
            self.assertTrue(restarted.reminder_history("p1"))
            self.assertTrue(restarted.plan_events("p1"))

            # 时间继续推进，新到期措施自动补挂提醒
            # 2026-03-02 01:30 UTC = 洛杉矶 3/1 17:30：当日洗手（08:00）
            # 与防蚊（17:00）均已到期
            self.clock.value = dt(2026, 3, 2, 1, 30)
            due = restarted.due_reminders(mark_sent=True)
            self.assertTrue(due)
            # 重启后重复扫描升级不产生重复
            again = restarted.pending_escalations()
            self.assertEqual(again["created"], [])
            restarted.close()
            self.svc = TravelHealthService(clock=self.clock)


class SymptomTriageTests(ServiceTestBase):
    def _guidance_codes(self, resp):
        return [g["code"] for g in resp["guidance"]]

    def test_exposed_returner_symptoms_put_care_path_first(self):
        self.activate()
        self.add_member("adult1", D.MEMBER_ADULT)
        self.add_window()
        # 回程后第 5 天，上海本地时间报告发热（登革热暴露 → 紧急）
        resp = self.svc.report_symptoms(
            "p1", "adult1", ["fever"], "req-sym-1",
            reported_at=dt(2026, 3, 15, 12, 0, tz=HOME_TZ))
        self.assertTrue(resp["care_first"])
        self.assertTrue(resp["exposed"])
        self.assertTrue(resp["in_monitoring"])
        self.assertIsNotNone(resp["escalation_id"])
        codes = self._guidance_codes(resp)
        tip_pos = [i for i, c in enumerate(codes) if c.startswith("tip_")]
        care_pos = [i for i, c in enumerate(codes)
                    if c in ("emergency_now", "fever_clinic",
                             "declare_travel_history", "quarantine_hotline",
                             "avoid_public_transit", "isolate_from_household")]
        self.assertTrue(care_pos)
        self.assertTrue(all(c < min(tip_pos) for c in care_pos))
        self.assertEqual(codes[0], "emergency_now")
        # 同一请求键重复提交不产生第二条报告/升级
        dup = self.svc.report_symptoms(
            "p1", "adult1", ["fever"], "req-sym-1",
            reported_at=dt(2026, 3, 15, 12, 0, tz=HOME_TZ))
        self.assertEqual(dup["report_id"], resp["report_id"])
        self.assertEqual(
            len(self.svc.list_escalations(status=D.ESCALATION_OPEN)), 1)

    def test_symptoms_during_trip_also_care_first(self):
        self.activate()
        self.add_member("adult1", D.MEMBER_ADULT)
        self.add_window()
        resp = self.svc.report_symptoms(
            "p1", "adult1", ["diarrhea"], "req-sym-trip",
            reported_at=dt(2026, 3, 5, 20, 0, tz=HOME_TZ))
        self.assertTrue(resp["care_first"])
        self.assertFalse(resp["in_monitoring"])  # 尚在行程中
        codes = self._guidance_codes(resp)
        self.assertLess(codes.index("fever_clinic"),
                        next(i for i, c in enumerate(codes)
                             if c.startswith("tip_")))

    def test_after_monitoring_month_ordinary_tips_only(self):
        self.activate()
        self.add_member("adult1", D.MEMBER_ADULT)
        self.add_window()
        # 上海 4/11 已超出一个月监测窗口
        resp = self.svc.report_symptoms(
            "p1", "adult1", ["fever"], "req-sym-late",
            reported_at=dt(2026, 4, 11, 9, 0, tz=HOME_TZ))
        self.assertFalse(resp["care_first"])
        self.assertIsNone(resp["escalation_id"])
        codes = self._guidance_codes(resp)
        # 普通就医建议仍在提示之前，但没有发热门诊/检疫路径
        self.assertNotIn("fever_clinic", codes)
        self.assertNotIn("quarantine_hotline", codes)
        self.assertEqual(codes[0], "seek_care")

    def test_unexposed_member_no_escalation_red_flag_emergency(self):
        self.make_plan("pSafe")
        self.activate("pSafe")
        self.add_member("adult9", D.MEMBER_ADULT, plan="pSafe")
        resp = self.svc.report_symptoms(
            "pSafe", "adult9", ["shortness_of_breath"], "req-sym-red",
            reported_at=dt(2026, 3, 12, 8, 0, tz=HOME_TZ))
        self.assertFalse(resp["care_first"])
        self.assertIsNone(resp["escalation_id"])
        self.assertEqual(self._guidance_codes(resp)[0], "emergency_now")

    def test_report_before_departure_not_care_window(self):
        self.activate()
        self.add_member("adult1", D.MEMBER_ADULT)
        self.add_window()
        resp = self.svc.report_symptoms(
            "p1", "adult1", ["fever"], "req-sym-pre",
            reported_at=dt(2026, 2, 20, 8, 0, tz=HOME_TZ))
        self.assertFalse(resp["care_first"])
        self.assertIsNone(resp["escalation_id"])


class PermissionVersionTests(ServiceTestBase):
    def test_operator_and_version_checks(self):
        with self.assertRaises(TravelHealthError):
            self.svc.activate_plan("p1", "intruder", "req-hack")
        self.activate()
        with self.assertRaises(TravelHealthError):
            self.svc.cancel_plan("p1", "op1", "req-stale",
                                 expected_version=99)
        with self.assertRaises(TravelHealthError):
            self.svc.upsert_risk_window(
                "p1", "wx", "x", "2026-03-01", "2026-03-02",
                D.RISK_LOW, [], "intruder", "req-hack2")

    def test_bad_dates_and_timezone_rejected(self):
        with self.assertRaises(TravelHealthError):
            self.svc.create_plan(
                "bad", "op1", "X", DEST_TZ, HOME_TZ,
                "2026-05-10", "2026-05-01", "req-bad-dates")
        with self.assertRaises(ValueError):
            self.svc.create_plan(
                "bad2", "op1", "X", "Mars/Olympus", HOME_TZ,
                "2026-05-01", "2026-05-10", "req-bad-tz")


class ApiSmokeTest(unittest.TestCase):
    def test_http_endpoints(self):
        from holiday_health.api import Handler
        Handler.travel_store = TravelHealthService(
            clock=FixedClock(dt(2026, 2, 15, 2, 0)),
            check_same_thread=False)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def call(method, path, body=None):
                data = json.dumps(body or {}).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}", data=data,
                    method=method,
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req) as resp:
                    return json.loads(resp.read())

            call("POST", "/plans", {
                "plan_id": "p1", "operator_id": "op1",
                "destination": "疫区城市", "destination_tz": DEST_TZ,
                "home_tz": HOME_TZ, "depart_date": "2026-03-01",
                "return_date": "2026-03-10", "request_key": "k1"})
            call("POST", "/plans/p1/activate",
                 {"operator_id": "op1", "request_key": "k2"})
            call("PUT", "/plans/p1/members/adult1", {
                "operator_id": "op1", "category": "adult",
                "name": "成人甲", "consent_status": "granted",
                "request_key": "k3"})
            call("PUT", "/plans/p1/risk-windows/w1", {
                "operator_id": "op1", "region": "城区",
                "start_date": "2026-03-01", "end_date": "2026-03-10",
                "risk_level": "high",
                "diseases": ["dengue", "yellow_fever"],
                "request_key": "k4"})
            due = call("POST", "/reminders/due?send=1", {})
            self.assertTrue(due)
            again = call("POST", "/reminders/due?send=1", {})
            self.assertEqual(again, [])
            sym = call("POST", "/plans/p1/members/adult1/symptoms", {
                "symptoms": ["fever"], "request_key": "k5",
                "reported_at": dt(2026, 3, 15, 12, tz=HOME_TZ).isoformat()})
            self.assertTrue(sym["care_first"])
            self.assertLess(
                [g["code"] for g in sym["guidance"]].index("fever_clinic"),
                next(i for i, g in enumerate(sym["guidance"])
                     if g["code"].startswith("tip_")))
            esc = call("GET", "/escalations?status=open")
            self.assertEqual(len(esc), 1)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            Handler.travel_store.close()


if __name__ == "__main__":
    unittest.main()
