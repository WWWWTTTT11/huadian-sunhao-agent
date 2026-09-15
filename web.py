# -*- coding: utf-8 -*-
"""盘口损耗研判智能体 Web 服务。"""
import json
import os
import time
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

import sources

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "slippage_history.json")


def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _load_history():
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save_history(row):
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    rows = _load_history()
    rows.insert(0, row)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(rows[:30], f, ensure_ascii=False, indent=2)


def analyze(symbol, side, amount):
    depth = sources.fetch_binance_depth(symbol, 100)
    calc_amounts = [1000, 5000, 10000, 30000]
    if amount not in calc_amounts:
        calc_amounts.append(amount)
        calc_amounts.sort()
    calc = sources.calculate_slippage(depth, side, calc_amounts)
    fg = sources.fetch_fear_greed(7)
    now = fg.get("now", {})
    hist = fg.get("history", [])
    prev = hist[-2] if len(hist) >= 2 else None
    delta = (int(now.get("value", 0)) - int(prev.get("value", 0))) if prev else 0
    row = {
        "id": str(int(time.time() * 1000)),
        "symbol": symbol.upper(), "side": side, "amount": amount,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "risk_rating": calc["risk_rating"], "slippage_pct": calc["results"][-1]["slippage_pct"],
        "cost_usdt": calc["results"][-1]["cost_usdt"],
    }
    _save_history(row)
    return {"symbol": symbol.upper(), "requested_amount": amount, "depth": depth,
            "slippage": calc, "fear_greed": {"now": now, "history": hist, "delta": delta},
            "history": _load_history()}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api(self, fn):
        try:
            self._send(200, _json_bytes(fn()))
        except Exception as e:
            self._send(500, _json_bytes({"error": str(e)}))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            fp = os.path.join(STATIC_DIR, "index.html")
            with open(fp, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return
        if path == "/api/history":
            self._api(_load_history)
            return
        if path == "/api/analyze":
            symbol = (q.get("symbol", ["BTCUSDT"])[0] or "BTCUSDT").upper().strip()
            if not symbol.isalnum() or len(symbol) > 20:
                self._send(400, _json_bytes({"error": "交易对格式无效"}))
                return
            side = q.get("side", ["buy"])[0].lower()
            if side not in ("buy", "sell"):
                self._send(400, _json_bytes({"error": "交易方向无效"}))
                return
            try:
                amount = max(100, min(float(q.get("amount", ["10000"])[0]), 1000000))
            except (TypeError, ValueError):
                amount = 10000
            self._api(lambda: analyze(symbol, side, amount))
            return
        self._send(404, b"not found")

    def log_message(self, *args):
        pass


def run(port=None):
    port = port or int(os.environ.get("PORT", "8001"))
    host = os.environ.get("HOST", "0.0.0.0")
    server = HTTPServer((host, port), Handler)
    print("盘口损耗研判智能体已启动: http://{}:{}".format(host, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


def run_dual(port_a=8001, port_b=8002):
    """同时监听两个本地端口，便于不同网络环境访问。"""
    import threading
    host = os.environ.get("HOST", "0.0.0.0")
    servers = [HTTPServer((host, int(port_a)), Handler), HTTPServer((host, int(port_b)), Handler)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    print("盘口损耗研判智能体双端口已启动:")
    print("  国内端口示例: http://127.0.0.1:{}".format(port_a))
    print("  国外端口示例: http://127.0.0.1:{}".format(port_b))
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        for server in servers:
            server.shutdown()
        print("\n已停止。")
