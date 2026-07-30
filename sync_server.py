"""本机同步中继服务（物流看板「刷新」键的后端）

监听 127.0.0.1:8765，供看板网页（github.io）跨域调用，即时从腾讯文档拉取最新数据并推送到 GitHub Pages。
- GET /health  -> {ok:true, syncing:bool}
- GET /sync    -> 执行一次同步（调用 sync.run_sync()），返回 {ok,lastUpdated,rowCount,message}
- OPTIONS      -> 预检（含私有网络访问 PNA 头，供 https 页面调 localhost）

安全：
- 仅绑定回环地址 127.0.0.1，外部不可访问。
- 自带 CORS + Access-Control-Allow-Private-Network，允许 github.io(https) 页面调用 localhost。
- 互斥锁防止并发同步（同步中再点会返回 409）。

运行（建议登录自启，见下方说明）：
  python sync_server.py
依赖：与 sync.py 相同（playwright, cryptography, requests），且需本机已登录 qclaw/Edge 浏览器。
"""
import os
import sys
import json
import threading
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sync  # noqa: E402

HOST = "127.0.0.1"
PORT = 8765

_sync_lock = threading.Lock()
_syncing = False


def _log(msg):
    try:
        with open(os.path.join(HERE, "sync_server.log"), "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        # 允许 https 页面访问 localhost（Chrome 私有网络访问 PNA 预检要求）
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        global _syncing
        path = self.path.split("?")[0]
        if path in ("/health", "/"):
            self._json(200, {"ok": True, "service": "logistics-sync-relay",
                             "syncing": _syncing, "port": PORT})
            return
        if path == "/sync":
            if not _sync_lock.acquire(blocking=False):
                _log("SYNC rejected: already running")
                self._json(409, {"ok": False, "message": "同步正在进行中，请稍候重试"})
                return
            _syncing = True
            _log("SYNC start")
            try:
                result = sync.run_sync()
            except Exception as e:  # noqa: BLE001
                result = {"ok": False, "message": "服务异常: %s" % repr(e)}
            finally:
                _syncing = False
                _sync_lock.release()
            _log("SYNC done: %s" % json.dumps(result, ensure_ascii=False))
            self._json(200 if result.get("ok") else 500, result)
            return
        self._json(404, {"ok": False, "message": "not found"})

    def log_message(self, *a):
        pass  # 静默，避免污染控制台


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("[sync_server] listening on http://%s:%d  (Ctrl+C 退出)" % (HOST, PORT))
    _log("server started on %s:%d" % (HOST, PORT))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
    _log("server stopped")


if __name__ == "__main__":
    main()
