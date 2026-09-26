"""HTTP 边界测试：路由、幂等头、重启后同库继续可用。"""
import json
import os
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

from holiday_health.api import Handler
from holiday_health.domain import FixedClock


def _request(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        from holiday_health.service import TravelHealthService
        Handler.store = TravelHealthService(
            database=cls.db_path, clock=FixedClock("2026-06-01T00:00:00+00:00"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        Handler.store.close()
        os.unlink(cls.db_path)

    def post(self, path, body):
        return _request(self.base + path, "POST", body)

    def get(self, path):
        return _request(self.base + path)

    def test_01_full_journey_and_idempotency(self):
        code, plan = self.post("/plans", {
            "owner_id": "owner1", "destination": "Phuket",
            "dest_timezone": "Asia/Bangkok",
            "depart_date": "2026-07-01", "return_date": "2026-07-10",
            "request_key": "p1"})
        self.assertEqual(code, 201)
        pid = plan["plan_id"]
        # 重复提交幂等
        code, plan2 = self.post("/plans", {
            "owner_id": "owner1", "destination": "Phuket",
            "dest_timezone": "Asia/Bangkok",
            "depart_date": "2026-07-01", "return_date": "2026-07-10",
            "request_key": "p1"})
        self.assertEqual(plan2["plan_id"], pid)

        code, adult = self.post(f"/plans/{pid}/members", {
            "owner_id": "owner1", "name": "Mom", "birth_date": "1982-03-03",
            "relationship": "mother", "request_key": "m1"})
        self.assertEqual(code, 201)
        code, child = self.post(f"/plans/{pid}/members", {
            "owner_id": "owner1", "name": "Kid", "birth_date": "2017-09-01",
            "relationship": "child", "request_key": "m2"})
        self.assertEqual(code, 201)

        self.assertEqual(self.post(f"/members/{adult['member_id']}/consent",
                                   {"granted_by": "owner1", "request_key": "g1"})[0], 200)
        self.assertEqual(self.post(f"/members/{child['member_id']}/consent",
                                   {"granted_by": "owner1", "request_key": "g2"})[0], 200)

        code, risk = self.post("/risk/Phuket", {
            "start_date": "2026-07-01", "end_date": "2026-07-10",
            "level": "high", "request_key": "r1"})
        self.assertEqual(code, 200)

        code, sync = self.post(f"/plans/{pid}/sync",
                               {"actor_id": "owner1", "request_key": "s1"})
        self.assertEqual(code, 200)
        created = len(sync["created"])
        # 重复同步不新增
        code, sync2 = self.post(f"/plans/{pid}/sync",
                                {"actor_id": "owner1", "request_key": "s1"})
        self.assertEqual(sync2["created"], sync["created"])
        code, sync3 = self.post(f"/plans/{pid}/sync",
                                {"actor_id": "owner1", "request_key": "s2"})
        self.assertEqual(sync3["created"], [])
        self.assertGreater(created, 0)

        # 儿童/成人分流
        code, child_ms = self.get(f"/plans/{pid}/measures?member_id={child['member_id']}")
        self.assertTrue(any(m["kind"] == "child_group_hold" for m in child_ms))
        code, adult_ms = self.get(f"/plans/{pid}/measures?member_id={adult['member_id']}")
        self.assertTrue(any(m["kind"] == "adult_advisory" for m in adult_ms))
        self.assertFalse(any(m["kind"] == "child_group_hold" for m in adult_ms))

        # 无权限操作被拒
        code, err = self.post(f"/plans/{pid}/sync", {"actor_id": "stranger"})
        self.assertEqual(code, 400)

    def test_02_escalation_and_handoff(self):
        # 找到上一个测试创建的计划与儿童
        code, plans_events = self.get("/events?kinds=plan_created")
        dest_members = {}
        pid = None
        for e in plans_events:
            if e["body"]["destination"] == "Phuket":
                pid = e["aggregate_id"]
        self.assertIsNotNone(pid)
        code, members = self.get(f"/plans/{pid}/members")
        child = next(m for m in members if m["is_child"])

        code, rep = self.post(f"/members/{child['member_id']}/symptoms", {
            "symptoms": ["fever"], "occurred_at": "2026-07-12T20:00:00+07:00",
            "note": "发烧", "actor_id": "owner1", "request_key": "sym1"})
        self.assertEqual(code, 201)
        self.assertTrue(rep["from_risk_area"])
        self.assertIsNotNone(rep["escalation_id"])
        self.assertGreaterEqual(len(rep["care_pathway"]), 3)

        code, handoff = self.get("/handoff")
        self.assertEqual(code, 200)
        self.assertGreaterEqual(handoff["escalation_count"], 1)
        # 就医相关成员的待办排在最前
        self.assertEqual(handoff["pending_reminders"][0]["member_id"],
                         child["member_id"])

        esc = rep["escalation_id"]
        code, ack = self.post(f"/escalations/{esc}/ack",
                              {"actor_id": "duty", "request_key": "a1"})
        self.assertEqual(ack["status"], "acknowledged")

    def test_03_bad_requests(self):
        code, _ = self.post("/plans", {
            "owner_id": "o", "destination": "X",
            "dest_timezone": "Mars/Olympus",
            "depart_date": "2026-07-01", "return_date": "2026-07-10"})
        self.assertEqual(code, 400)
        code, _ = self.get("/plans/nonexistent")
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
