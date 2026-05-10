"""
Full-page screenshot via Chrome DevTools Protocol (CDP).
Starts a local HTTP server, launches Chrome with remote debugging,
and takes a full-page PNG via WebSocket CDP.
"""
import http.server
import socketserver
import threading
import subprocess
import time
import os
import sys
import json
import base64
import socket
import urllib.request

DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 3000
DEBUG_PORT = 9222
OUT_DIR = os.path.join(DIR, "temporary screenshots")
os.makedirs(OUT_DIR, exist_ok=True)

existing = [f for f in os.listdir(OUT_DIR) if f.startswith('screenshot-') and f.endswith('.png')]
nums = []
for f in existing:
    try: nums.append(int(f.replace('screenshot-','').split('-')[0].split('.')[0]))
    except: pass
next_n = max(nums) + 1 if nums else 1

label = f"-{sys.argv[2]}" if len(sys.argv) > 2 else ""
out_path = os.path.join(OUT_DIR, f"screenshot-{next_n}{label}.png")

# ── Start HTTP server ──────────────────────────────────────
os.chdir(DIR)
class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass

httpd = socketserver.TCPServer(("127.0.0.1", PORT), Handler)
srv_thread = threading.Thread(target=httpd.serve_forever)
srv_thread.daemon = True
srv_thread.start()
print(f"Server: http://127.0.0.1:{PORT}")
time.sleep(0.5)

# ── Launch Chrome headless with CDP ────────────────────────
chrome_paths = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
chrome = next((p for p in chrome_paths if os.path.exists(p)), None)
if not chrome:
    print("Chrome not found"); httpd.shutdown(); sys.exit(1)

url = sys.argv[1] if len(sys.argv) > 1 else f"http://127.0.0.1:{PORT}"

chrome_proc = subprocess.Popen([
    chrome,
    f"--remote-debugging-port={DEBUG_PORT}",
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--proxy-server=direct://",
    "--window-size=1440,900",
    url,
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# Wait for Chrome to be ready
for _ in range(30):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=1)
        break
    except: time.sleep(0.5)
else:
    print("Chrome CDP not ready"); chrome_proc.terminate(); httpd.shutdown(); sys.exit(1)

time.sleep(4)  # let page + external CDN resources fully load

# ── Connect to CDP via WebSocket ───────────────────────────
import struct

def ws_connect(host, port, path):
    """Minimal WebSocket client (no external deps)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((host, port))
    key = base64.b64encode(os.urandom(16)).decode()
    handshake = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    s.sendall(handshake.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += s.recv(4096)
    return s

def ws_send(s, data):
    payload = json.dumps(data).encode()
    n = len(payload)
    mask = os.urandom(4)
    header = b'\x81'
    if n < 126:
        header += bytes([n | 0x80])
    elif n < 65536:
        header += struct.pack(">BH", 254, n)
    else:
        header += struct.pack(">BQ", 255, n)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    s.sendall(header + masked)

def ws_recv(s):
    def read_exact(n):
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk: raise ConnectionError("closed")
            buf += chunk
        return buf
    b0, b1 = read_exact(2)
    masked = (b1 & 0x80) != 0
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", read_exact(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", read_exact(8))[0]
    mask = read_exact(4) if masked else b""
    data = read_exact(n)
    if masked:
        data = bytes(data[i] ^ mask[i % 4] for i in range(n))
    return json.loads(data.decode())

def cdp(s, method, params=None, id=1):
    ws_send(s, {"id": id, "method": method, "params": params or {}})
    while True:
        msg = ws_recv(s)
        if msg.get("id") == id:
            return msg

# Get first page's WS URL
tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json").read())
page_tab = next((t for t in tabs if t.get("type") == "page"), None)
if not page_tab:
    print("No page tab found"); chrome_proc.terminate(); httpd.shutdown(); sys.exit(1)

ws_url = page_tab["webSocketDebuggerUrl"]
# parse host/port/path
import urllib.parse
parsed = urllib.parse.urlparse(ws_url)
sock = ws_connect(parsed.hostname, parsed.port or 80, parsed.path + ("?" + parsed.query if parsed.query else ""))

# Enable Page domain
cdp(sock, "Page.enable", id=1)

# Inject JS: reveal all hidden elements instantly for screenshot
cdp(sock, "Runtime.evaluate", {
    "expression": """
        // Force all .reveal elements visible
        document.querySelectorAll('.reveal').forEach(el => {
            el.style.opacity = '1';
            el.style.transform = 'translateY(0px)';
            el.style.transition = 'none';
        });
        // Also force hero elements visible
        ['#hero-eyebrow','#hero-title','#hero-sub','#hero-ctas','#scroll-indicator'].forEach(id => {
            var el = document.querySelector(id);
            if (el) { el.style.opacity = '1'; el.style.transform = 'none'; }
        });
        // Refresh ScrollTrigger
        if (window.ScrollTrigger) ScrollTrigger.refresh();
        'done';
    """
}, id=2)
time.sleep(0.5)

# Get content size
metrics = cdp(sock, "Page.getLayoutMetrics", id=10)
content = metrics.get("result", {}).get("cssContentSize", metrics.get("result", {}).get("contentSize", {}))
w = int(content.get("width", 1440))
h = int(content.get("height", 900))
print(f"Page dimensions: {w}x{h}")

# Capture screenshot
result = cdp(sock, "Page.captureScreenshot", {
    "format": "png",
    "fromSurface": True,
    "captureBeyondViewport": True,
    "clip": {"x": 0, "y": 0, "width": w, "height": h, "scale": 1}
}, id=11)
img_data = result.get("result", {}).get("data", "")

if img_data:
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(img_data))
    print(f"Screenshot saved: {out_path}")
else:
    print("No image data in CDP response")

sock.close()
chrome_proc.terminate()
httpd.shutdown()
