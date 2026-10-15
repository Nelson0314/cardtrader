"""CardTrader 本機伺服器：提供 index.html 並讀寫 data.json。

資料保護：
  - 只接受本機連線（綁定 127.0.0.1、檢查 Host 標頭），不開放 CORS。
  - 每次寫入前，把現有 data.json 複製到 backups/，保留最近 MAX_BACKUPS 份。
  - 先寫入暫存檔再以 os.replace 取代，寫入中途當機也不會留下半個檔案。
  - 以 X-Base-Version 比對 lastUpdated，舊分頁的資料不會覆蓋較新的資料（回應 409）。
  - 拒絕不完整的內容（非 JSON、缺少陣列、或把有資料的帳本清成空的）。
"""
import http.server
import json
import os
import shutil
import socket
import socketserver
import threading
import time
import webbrowser
from datetime import datetime

PORT = 3000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, 'data.json')
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')
MAX_BACKUPS = 50
IDLE_TIMEOUT = 180   # 秒；背景分頁的 ping 會被瀏覽器節流到約每分鐘一次
ALLOWED_HOSTS = {f'localhost:{PORT}', f'127.0.0.1:{PORT}'}
SERVED_FILES = {'/': 'index.html', '/index.html': 'index.html'}

last_ping_time = time.time()
write_lock = threading.Lock()


def read_data():
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def backup_current():
    if not os.path.exists(DATA_FILE):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    shutil.copy2(DATA_FILE, os.path.join(BACKUP_DIR, f'data_{stamp}.json'))
    # 自動備份只保留最近 MAX_BACKUPS 份；手動備份（檔名含其他標記）不刪
    auto = sorted(f for f in os.listdir(BACKUP_DIR)
                  if f.startswith('data_') and f.count('_') == 3 and f.endswith('.json'))
    for old in auto[:-MAX_BACKUPS]:
        os.remove(os.path.join(BACKUP_DIR, old))


def atomic_write(text):
    tmp = DATA_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DATA_FILE)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 由 .vbs 以隱藏視窗啟動，沒有主控台可輸出

    def send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def host_ok(self):
        if self.headers.get('Host') in ALLOWED_HOSTS:
            return True
        self.send_json(403, {'error': 'forbidden host'})
        return False

    def do_GET(self):
        if not self.host_ok():
            return
        path = self.path.split('?')[0]
        if path == '/api/data':
            if not os.path.exists(DATA_FILE):
                return self.send_json(200, {'products': [], 'transactions': []})
            with open(DATA_FILE, 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
            return
        if path in SERVED_FILES:
            with open(os.path.join(BASE_DIR, SERVED_FILES[path]), 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_json(404, {'error': 'not found'})

    def do_POST(self):
        global last_ping_time
        if not self.host_ok():
            return
        if self.path == '/api/ping':
            last_ping_time = time.time()
            return self.send_json(200, {'ok': True})
        if self.path != '/api/data':
            return self.send_json(404, {'error': 'not found'})

        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        try:
            text = raw.decode('utf-8')
            data = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self.send_json(400, {'error': 'invalid JSON'})
        if not isinstance(data.get('products'), list) or not isinstance(data.get('transactions'), list):
            return self.send_json(400, {'error': 'products / transactions must be arrays'})

        with write_lock:
            if os.path.exists(DATA_FILE):
                current = read_data()
                base = self.headers.get('X-Base-Version', '')
                if current.get('lastUpdated') and base != current['lastUpdated']:
                    return self.send_json(409, {'error': 'data changed since it was loaded'})
                if (current.get('products') or current.get('transactions')) \
                        and not data['products'] and not data['transactions']:
                    return self.send_json(400, {'error': 'refusing to replace existing data with an empty ledger'})
            backup_current()
            atomic_write(text)
        self.send_json(200, {'success': True})


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = False   # Windows 上設為 True 會允許兩個程序同時綁定同一個埠
    daemon_threads = True


def connection_monitor():
    while True:
        time.sleep(5)
        if time.time() - last_ping_time > IDLE_TIMEOUT:
            os._exit(0)


def port_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', PORT)) == 0


if __name__ == '__main__':
    url = f'http://localhost:{PORT}'
    if port_in_use():
        # 系統已在執行：只開啟瀏覽器，不啟動第二個伺服器
        webbrowser.open(url)
    else:
        threading.Thread(target=connection_monitor, daemon=True).start()
        with Server(('127.0.0.1', PORT), Handler) as httpd:
            webbrowser.open(url)
            httpd.serve_forever()
