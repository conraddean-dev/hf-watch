#!/usr/bin/env python3
"""
HF·WATCH — start.py
Serves hf-watch.html + proxies DX cluster data.
Usage:  python start.py
Open:   http://localhost:8080/hf-watch.html
"""

import os, sys, ssl, json, urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

os.chdir(os.path.dirname(os.path.abspath(__file__)))

PORT = 8080

ssl_ctx = ssl.create_default_context()

def safe_print(msg):
    """Print with ASCII fallback so Windows console never crashes."""
    try:
        print(msg)
    except Exception:
        print(msg.encode('ascii', errors='replace').decode('ascii'))

def fetch_url(url, timeout=12):
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 HF-WATCH/2.0',
        'Accept': 'application/json, text/plain, */*',
    })
    ctx = ssl_ctx if url.startswith('https') else None
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
        return r.status, dict(r.headers), r.read()

def parse_spots(data: bytes) -> list:
    try:
        obj = json.loads(data)
    except Exception as e:
        safe_print(f'  DX    JSON parse failed: {e}')
        return []

    # s  = {spot_id: [dx_call, freq, de_call, comment, time, ...]}
    # ci = {callsign: [prefix, country, continent, flag, cq, itu, lat, lon, ...]}
    spots_raw = obj.get('s', {})
    ci        = obj.get('ci', {})

    if not isinstance(spots_raw, dict) or not spots_raw:
        safe_print(f'  DX    s is empty or wrong type: {type(spots_raw).__name__}')
        return []

    safe_print(f'  DX    parsing {len(spots_raw)} raw spots, {len(ci)} callsigns in ci')

    spots = []
    for spot_id, arr in spots_raw.items():
        try:
            if not isinstance(arr, list) or len(arr) < 3:
                continue
            dx      = str(arr[0]).strip()
            freq    = str(arr[1])
            de      = str(arr[2]).strip()
            comment = str(arr[3]).strip() if len(arr) > 3 else ''
            time_   = str(arr[4]).strip() if len(arr) > 4 else ''

            if not de or not dx:
                continue

            spot = {
                'de': de, 'dx': dx, 'freq': freq,
                'band': '', 'comment': comment,
                'mode': '', 'time': time_,
            }

            # Attach lat/lon from ci so HTML can skip DXCC prefix lookup
            de_info = ci.get(de, [])
            dx_info = ci.get(dx, [])
            if len(de_info) >= 8:
                try:
                    spot['de_lat'] = float(de_info[6])
                    spot['de_lon'] = float(de_info[7])
                except (ValueError, TypeError):
                    pass
            if len(dx_info) >= 8:
                try:
                    spot['dx_lat'] = float(dx_info[6])
                    spot['dx_lon'] = float(dx_info[7])
                except (ValueError, TypeError):
                    pass

            spots.append(spot)
        except Exception:
            continue

    safe_print(f'  DX    built {len(spots)} spots')
    return spots

class Handler(SimpleHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors(); self.end_headers()

    def do_GET(self):
        if self.path.startswith('/spots'):
            self._serve_dx()
        elif self.path.startswith('/owm/'):
            self._proxy_owm()
        else:
            super().do_GET()

    def _proxy_owm(self):
        # /owm/{layer}/{z}/{x}/{y}.png?appid={key}
        # Proxies OWM tile requests to bypass browser CORS restrictions
        try:
            req = urllib.request.Request(
                f'https://tile.openweathermap.org/map{self.path[4:]}',
                headers={'User-Agent': 'HF-WATCH/2.0'}
            )
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as r:
                data = r.read()
                ct = r.headers.get('Content-Type', 'image/png')
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', ct)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'public, max-age=600')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            safe_print(f'  OWM tile error: {e}')
            self.send_response(502)
            self.end_headers()

    def _serve_dx(self):
        result = {'error': 'No data'}

        try:
            url = 'http://www.dxwatch.com/dxsd1/s.php?c=75'
            status, headers, data = fetch_url(url)
            ct = headers.get('Content-Type', 'unknown')
            safe_print(f'  DX    dxwatch HTTP {status}  {ct}  {len(data)} bytes')
            spots = parse_spots(data)
            if spots:
                safe_print(f'  DX checkmark  {len(spots)} spots from dxwatch')
                result = spots
            else:
                result = {'error': f'dxwatch: 0 spots parsed from {len(data)}b'}
        except Exception as e:
            safe_print(f'  DX    dxwatch error: {type(e).__name__}: {e}')
            result = {'error': f'dxwatch: {type(e).__name__}: {e}'}

        # Serialize — strip NaN coords and retry if needed
        try:
            body = json.dumps(result, ensure_ascii=True, allow_nan=False).encode('utf-8')
        except (ValueError, TypeError):
            if isinstance(result, list):
                for spot in result:
                    for k in ('de_lat', 'de_lon', 'dx_lat', 'dx_lon'):
                        spot.pop(k, None)
            body = json.dumps(result, ensure_ascii=True).encode('utf-8')

        # Always send response
        try:
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except Exception as e:
            safe_print(f'  DX    response write error: {e}')

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin',  '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')

    def log_message(self, fmt, *args):
        path = str(args[0]) if args else ''
        code = str(args[1]) if len(args) > 1 else ''
        skip = ('.css','.js','.png','.ico','.woff','.map','leaflet','favicon')
        if not any(s in path for s in skip):
            safe_print(f'  {code}  {path}')

if __name__ == '__main__':
    safe_print(f'''
  +----------------------------------------------+
  |           HF.WATCH  --  start.py             |
  +----------------------------------------------+
  |  Serving from:                               |
  |  {os.getcwd():<44s}|
  |                                              |
  |  Open -> http://localhost:{PORT}/hf-watch.html  |
  |  Press  Ctrl+C  to stop                      |
  +----------------------------------------------+
''')
    try:
        ThreadingHTTPServer(('', PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        safe_print('\n  Stopped.')
        sys.exit(0)
