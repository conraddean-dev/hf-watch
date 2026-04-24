#!/usr/bin/env python3
"""
HF·WATCH — start.py
Serves hf-watch.html + proxies DX cluster data.
Usage:  python start.py
Open:   http://localhost:8080/hf-watch.html
"""

import os, sys, ssl, json, urllib.request, urllib.parse
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
        safe_print(f'  DX ✗  JSON parse failed: {e}')
        return []

    spots_raw = obj.get('s', {})
    ci        = obj.get('ci', {})

    if not isinstance(spots_raw, dict) or not spots_raw:
        return []

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

    def do_POST(self):
        if self.path == '/qrz-log':
            self._proxy_qrz_log()
        else:
            self.send_response(405)
            self.end_headers()

    def _proxy_qrz_log(self):
        """
        Proxy POST requests to the QRZ Logbook API.
        Client sends JSON {key, options} → we POST to QRZ → return parsed response.
        """
        try:
            length = int(self.headers.get('Content-Length', 0))
            body   = json.loads(self.rfile.read(length))
            key    = body.get('key', '').strip()
            opts   = body.get('options', 'MAX:250,AFTERLOGID:0')

            if not key:
                self._json_response({'error': 'No API key provided'})
                return

            post_data = urllib.parse.urlencode({
                'KEY':    key,
                'ACTION': 'FETCH',
                'OPTION': opts,
            }).encode('utf-8')

            req = urllib.request.Request(
                'https://logbook.qrz.com/api',
                data=post_data,
                method='POST',
                headers={
                    'User-Agent':   f'HF-WATCH/{PORT} (hf-watch)',
                    'Content-Type': 'application/x-www-form-urlencoded',
                }
            )
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=20) as r:
                raw = r.read().decode('utf-8')

            # QRZ returns URL-encoded name=value pairs
            parsed = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))
            result = parsed.get('RESULT', '').upper()

            if result == 'OK':
                self._json_response({
                    'result': 'OK',
                    'adif':   parsed.get('ADIF', ''),
                    'count':  parsed.get('COUNT', '0'),
                    'logids': parsed.get('LOGIDS', ''),
                })
            else:
                msg = parsed.get('REASON') or parsed.get('ERROR') or raw[:200]
                safe_print(f'  QRZ ✗  {msg}')
                self._json_response({'error': msg, 'result': result})

        except Exception as e:
            safe_print(f'  QRZ !! {type(e).__name__}: {e}')
            self._json_response({'error': f'{type(e).__name__}: {e}'})

    def _json_response(self, obj):
        body = json.dumps(obj).encode('utf-8')
        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

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
            url = 'http://www.dxwatch.com/dxsd1/s.php?c=100'
            status, headers, data = fetch_url(url)
            spots = parse_spots(data)
            if spots:
                safe_print(f'  DX ✓  {len(spots)} spots')
                result = spots
            else:
                result = {'error': f'0 spots parsed from {len(data)}b'}
        except Exception as e:
            safe_print(f'  DX ✗  {type(e).__name__}: {e}')
            result = {'error': f'{type(e).__name__}: {e}'}

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
