"""假期健康提醒编排的持久化边界和状态服务。"""
import json,sqlite3
from contextlib import contextmanager
from .domain import Record,utc_now
class ServiceError(Exception): pass
class DomainStore:
 def __init__(self,database=":memory:",clock=utc_now):
  self.connection=sqlite3.connect(database);self.connection.row_factory=sqlite3.Row;self.clock=clock;self.connection.executescript("""PRAGMA foreign_keys=ON;CREATE TABLE IF NOT EXISTS records(record_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,state TEXT NOT NULL,version INTEGER NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL);CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,record_id TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(record_id) REFERENCES records(record_id));CREATE TABLE IF NOT EXISTS idempotency(request_key TEXT PRIMARY KEY,result TEXT NOT NULL);""");self.connection.commit()
 @contextmanager
 def transaction(self):
  try:self.connection.execute("BEGIN IMMEDIATE");yield;self.connection.commit()
  except Exception:self.connection.rollback();raise
 def create(self,record_id,owner_id,payload=None):
  with self.transaction():
   self.connection.execute("INSERT INTO records VALUES(?,?,?,?,?,?)",(record_id,owner_id,"draft",1,json.dumps(payload or {}),self.clock()));self.connection.execute("INSERT INTO events VALUES(?,?,?,?,?)",(record_id+":created",record_id,"created","{}",self.clock()))
  return self.get(record_id)
 def get(self,record_id):
  row=self.connection.execute("SELECT * FROM records WHERE record_id=?",(record_id,)).fetchone()
  if row is None:raise ServiceError("记录不存在")
  return Record(row["record_id"],row["owner_id"],row["state"],row["version"],row["updated_at"])
 def transition(self,record_id,owner_id,target,request_key,expected_version=None):
  with self.transaction():
   old=self.connection.execute("SELECT * FROM records WHERE record_id=?",(record_id,)).fetchone()
   if old is None:raise ServiceError("记录不存在")
   if old["owner_id"]!=owner_id:raise ServiceError("无权操作")
   cached=self.connection.execute("SELECT result FROM idempotency WHERE request_key=?",(request_key,)).fetchone()
   if cached:return json.loads(cached["result"])
   if expected_version is not None and old["version"]!=expected_version:raise ServiceError("版本冲突")
   allowed={"draft":{"pending"},"pending":{"approved","cancelled"},"approved":{"closed"},"cancelled":set(),"closed":set()}
   if target not in allowed.get(old["state"],set()):raise ServiceError("状态迁移不允许")
   version=old["version"]+1;now=self.clock();self.connection.execute("UPDATE records SET state=?,version=?,updated_at=? WHERE record_id=?",(target,version,now,record_id));body=json.dumps({"from":old["state"],"to":target,"version":version});self.connection.execute("INSERT INTO events VALUES(?,?,?,?,?)",(request_key+":event",record_id,"transition",body,now));result={"record_id":record_id,"state":target,"version":version};self.connection.execute("INSERT INTO idempotency VALUES(?,?)",(request_key,json.dumps(result)));return result
 def close(self):self.connection.close()
