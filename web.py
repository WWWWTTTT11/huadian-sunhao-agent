from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import json, os
from sources import analyze

ROOT = os.path.dirname(os.path.abspath(__file__))
class Handler(BaseHTTPRequestHandler):
    def send_json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/api/liquidations':
            symbol = parse_qs(u.query).get('symbol',['BTCUSDT'])[0].upper()
            try:
                result = analyze(symbol)
                self.send_json({'symbol': result['symbol'], 'summary': result['summary'], 'hourly': result['hourly'], 'large': result['large']})
            except Exception as e: self.send_json({'error': str(e)}, 500)
            return
        if u.path == '/api/positions':
            symbol = parse_qs(u.query).get('symbol',['BTCUSDT'])[0].upper()
            try:
                result = analyze(symbol)
                self.send_json({'symbol': result['symbol'], 'summary': result['summary'], 'trend': result['trend']})
            except Exception as e: self.send_json({'error': str(e)}, 500)
            return
        if u.path == '/api/analyze':
            symbol = parse_qs(u.query).get('symbol',['BTCUSDT'])[0].upper()
            try: self.send_json(analyze(symbol))
            except Exception as e: self.send_json({'error': str(e)}, 500)
            return
        if u.path == '/api/health': self.send_json({'status':'ok'}); return
        path = os.path.join(ROOT, 'static', 'index.html')
        with open(path, 'rb') as f: raw=f.read()
        self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def log_message(self, *args): pass

def run_server(port=None):
    port = int(port or os.environ.get('PORT') or 8001)
    host = os.environ.get('HOST') or '127.0.0.1'
    print(f'网页地址：http://{host}:{port}')
    ThreadingHTTPServer((host, port), Handler).serve_forever()
