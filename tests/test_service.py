import unittest
from holiday_health.service import DomainStore,ServiceError
class StoreTests(unittest.TestCase):
 def setUp(self):self.store=DomainStore()
 def tearDown(self):self.store.close()
 def test_idempotent_version(self):
  self.store.create("r1","u1",{"topic":"假期健康提醒编排"});a=self.store.transition("r1","u1","pending","req-1",1);b=self.store.transition("r1","u1","pending","req-1",1);self.assertEqual(a,b)
  with self.assertRaises(ServiceError):self.store.transition("r1","u1","approved","req-2",1)
 def test_permission_state(self):
  self.store.create("r2","u1")
  with self.assertRaises(ServiceError):self.store.transition("r2","u2","pending","req-3")
  with self.assertRaises(ServiceError):self.store.transition("r2","u1","closed","req-4")
if __name__=="__main__":unittest.main()
