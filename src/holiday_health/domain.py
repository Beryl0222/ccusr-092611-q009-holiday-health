"""假期健康提醒编排中的基础对象。"""
from dataclasses import dataclass
from datetime import datetime,timezone
@dataclass(frozen=True)
class Record:
 record_id:str
 owner_id:str
 state:str
 version:int
 updated_at:str
def utc_now(): return datetime.now(timezone.utc).isoformat()
