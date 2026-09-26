"""假期健康提醒编排的轻量 HTTP 边界。"""
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from .service import DomainStore,ServiceError
class Handler(BaseHTTPRequestHandler):
 store=DomainStore()
 def _reply(self,code,body):
  data=json.dumps(body).encode();self.send_response(code);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
 def do_GET(self):
  try:self._reply(200,self.store.get(self.path.rsplit("/",1)[-1]).__dict__)
  except ServiceError as exc:self._reply(404,{"error":str(exc)})
 def log_message(self,*_):return
def serve(host="127.0.0.1",port=8080):ThreadingHTTPServer((host,port),Handler).serve_forever()
