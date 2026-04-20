#!/usr/bin/env python3
"""
Dashboard web local para Monitor de Flujo de Caña.
Sirve un frontend HTML en localhost:8080 y una API JSON con métricas de flujo.

Uso: python3 dashboard.py [--port 8080]
"""

import json
import os
import sys
import argparse
import psycopg2
from psycopg2.extras import Json
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from urllib.parse import urlparse

DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"
DATABASE_URL = os.environ.get('DATABASE_URL')
PORT = int(os.environ.get('PORT', 8080))

THRESHOLD_OK_ABS = 20.0
THRESHOLD_LOW_ABS = 5.0
THRESHOLD_OK_REL = 0.70
THRESHOLD_LOW_REL = 0.30
TREND_BAND = 0.15


def load_history():
    if not DATABASE_URL:
        return []
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            SELECT data FROM readings
            ORDER BY fetch_time ASC
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [json.loads(row[0]) if isinstance(row[0], str) else row[0] for row in rows]
    except Exception as e:
        print(f"Error cargando histórico: {e}", file=sys.stderr)
        return []


def parse_fetch_time(fetch_time_str):
    try:
        dt = datetime.fromisoformat(fetch_time_str)
        # Normalizar: si es naive, asumir UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except:
        return None


def classify_status(flow, avg_flow, history_points):
    if flow is None:
        return "unknown"
    if history_points >= 3 and avg_flow is not None:
        if flow >= avg_flow * THRESHOLD_OK_REL:
            return "ok"
        elif flow >= avg_flow * THRESHOLD_LOW_REL:
            return "low"
        else:
            return "stop"
    else:
        if flow > THRESHOLD_OK_ABS:
            return "ok"
        elif flow > THRESHOLD_LOW_ABS:
            return "low"
        else:
            return "stop" if flow == 0 else "low"


def classify_trend(flow, avg_flow):
    if flow is None or avg_flow is None or avg_flow == 0:
        return "unknown"
    ratio = flow / avg_flow
    if ratio > (1 + TREND_BAND):
        return "up"
    elif ratio < (1 - TREND_BAND):
        return "down"
    else:
        return "stable"


def _calculate_stage_flows(curr_frente, prev_frente, elapsed_h):
    """Calcula flujo en t/h para cada etapa del pipeline."""
    if prev_frente is None or elapsed_h is None or elapsed_h <= 0:
        return None

    stages = {
        'campo': ('tcampo', 'ucampo'),
        'vienen': ('tvienen', 'uvienen'),
        'patio': ('tpatio', 'upatio'),
        'plantel': ('tplantel', 'uplantel'),
        'molino': ('tmoli', 'umoli'),
        'van': ('tvan', 'uvan')
    }

    result = {}
    for stage_name, (t_key, u_key) in stages.items():
        curr_t = curr_frente.get(t_key, 0)
        prev_t = prev_frente.get(t_key, 0)
        delta_t = curr_t - prev_t
        flow_tph = delta_t / elapsed_h if elapsed_h > 0 else 0

        result[stage_name] = {
            'delta_ton': round(delta_t, 2),
            'flow_tph': round(flow_tph, 2),
            'current_t': round(curr_t, 2),
            'current_u': curr_frente.get(u_key, 0)
        }

    return result


def compute_api_data(history):
    if not history:
        return {
            "meta": {
                "last_timestamp": None,
                "last_fetch_time": None,
                "readings_count": 0,
                "server_time": datetime.now(timezone.utc).isoformat()
            },
            "frentes": {},
            "total": {
                "snapshot": {},
                "flow": {"current_tph": 0, "avg_tph": 0},
                "frentes_ok": 0,
                "frentes_low": 0,
                "frentes_stop": 0,
                "frentes_unknown": 0
            }
        }

    current = history[-1]
    previous = history[-2] if len(history) >= 2 else None

    elapsed_h = None
    if previous:
        curr_ts = parse_fetch_time(current['fetch_time'])
        prev_ts = parse_fetch_time(previous['fetch_time'])
        if curr_ts and prev_ts:
            elapsed_sec = (curr_ts - prev_ts).total_seconds()
            if elapsed_sec > 60:
                elapsed_h = elapsed_sec / 3600

    historical_flows = {}
    for codigo in current['frentes'].keys():
        historical_flows[codigo] = []

    for i in range(1, len(history)):
        curr = history[i]
        prev = history[i - 1]
        curr_ts = parse_fetch_time(curr['fetch_time'])
        prev_ts = parse_fetch_time(prev['fetch_time'])
        if not curr_ts or not prev_ts:
            continue
        elapsed_sec = (curr_ts - prev_ts).total_seconds()
        if elapsed_sec < 60:
            continue
        elapsed_h_pair = elapsed_sec / 3600
        for codigo, curr_data in curr['frentes'].items():
            if codigo in prev['frentes']:
                delta = curr_data['tmoli'] - prev['frentes'][codigo]['tmoli']
                if delta >= 0:
                    flow = delta / elapsed_h_pair
                    historical_flows[codigo].append(flow)

    avg_flows = {}
    for codigo, flows in historical_flows.items():
        if flows:
            avg_flows[codigo] = mean(flows)

    frentes_data = {}
    for codigo, curr_frente in sorted(current['frentes'].items(),
                                       key=lambda x: (int(x[0]) if x[0].isdigit() else 9999)):
        flow_tph = None
        if previous and elapsed_h and codigo in previous['frentes']:
            delta = curr_frente['tmoli'] - previous['frentes'][codigo]['tmoli']
            if delta >= 0:
                flow_tph = delta / elapsed_h

        avg_tph = avg_flows.get(codigo)
        status = classify_status(flow_tph, avg_tph, len(historical_flows.get(codigo, [])))
        trend = classify_trend(flow_tph, avg_tph)

        frentes_data[codigo] = {
            "codigo": codigo,
            "nombre": curr_frente['frente'],
            "snapshot": {
                "ucampo": curr_frente['ucampo'],
                "tcampo": curr_frente['tcampo'],
                "uvienen": curr_frente['uvienen'],
                "tvienen": curr_frente['tvienen'],
                "uplantel": curr_frente['uplantel'],
                "tplantel": curr_frente['tplantel'],
                "upatio": curr_frente['upatio'],
                "tpatio": curr_frente['tpatio'],
                "umoli": curr_frente['umoli'],
                "tmoli": curr_frente['tmoli'],
                "uvan": curr_frente['uvan'],
                "tvan": curr_frente['tvan']
            },
            "flow": {
                "current_tph": round(flow_tph, 2) if flow_tph is not None else None,
                "avg_tph": round(avg_tph, 2) if avg_tph is not None else None,
                "delta_ton": round(curr_frente['tmoli'] - previous['frentes'].get(codigo, {}).get('tmoli', curr_frente['tmoli']), 2) if previous else 0,
                "history_points": len(historical_flows.get(codigo, []))
            },
            "stages": _calculate_stage_flows(curr_frente, previous['frentes'].get(codigo) if previous else None, elapsed_h) if previous and elapsed_h is not None and elapsed_h > 0 else None,
            "status": status,
            "trend": trend
        }

    total_current_flow = sum(
        d['flow']['current_tph'] for d in frentes_data.values()
        if d['flow']['current_tph'] is not None
    )
    total_avg_flow = sum(
        d['flow']['avg_tph'] for d in frentes_data.values()
        if d['flow']['avg_tph'] is not None
    )

    status_counts = {"ok": 0, "low": 0, "stop": 0, "unknown": 0}
    for f in frentes_data.values():
        status_counts[f['status']] += 1

    # Calcular flujos globales por etapa
    global_stages = None
    if previous and elapsed_h and elapsed_h > 0:
        global_stages = _calculate_stage_flows(current['total'], previous['total'], elapsed_h)

    return {
        "meta": {
            "last_timestamp": current['timestamp'],
            "last_fetch_time": current['fetch_time'],
            "readings_count": len(history),
            "server_time": datetime.now(timezone.utc).isoformat()
        },
        "frentes": frentes_data,
        "total": {
            "snapshot": {
                "ucampo": current['total']['ucampo'],
                "tcampo": current['total']['tcampo'],
                "uvienen": current['total']['uvienen'],
                "tvienen": current['total']['tvienen'],
                "uplantel": current['total']['uplantel'],
                "tplantel": current['total']['tplantel'],
                "upatio": current['total']['upatio'],
                "tpatio": current['total']['tpatio'],
                "umoli": current['total']['umoli'],
                "tmoli": current['total']['tmoli'],
                "uvan": current['total']['uvan'],
                "tvan": current['total']['tvan']
            },
            "flow": {
                "current_tph": round(total_current_flow, 2),
                "avg_tph": round(total_avg_flow, 2)
            },
            "frentes_ok": status_counts["ok"],
            "frentes_low": status_counts["low"],
            "frentes_stop": status_counts["stop"],
            "frentes_unknown": status_counts["unknown"]
        },
        "global_stages": global_stages
    }


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/':
            self._serve_html()
        elif path == '/api/data':
            self._serve_api()
        else:
            self.send_error(404)

    def _serve_html(self):
        if not DASHBOARD_HTML.exists():
            self.send_error(500, "dashboard.html no encontrado")
            return
        content = DASHBOARD_HTML.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(content))
        self.end_headers()
        self.wfile.write(content)

    def _serve_api(self):
        data = compute_api_data(load_history())
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description="Dashboard de Monitor de Flujo")
    parser.add_argument('--port', type=int, default=PORT, help=f"Puerto HTTP (default: {PORT})")
    args = parser.parse_args()

    print(f"Dashboard iniciado en http://0.0.0.0:{args.port}")
    if DATABASE_URL:
        print("Leyendo de PostgreSQL")
    else:
        print("ADVERTENCIA: DATABASE_URL no configurada")
    print("Ctrl+C para detener.\n")

    server = HTTPServer(('0.0.0.0', args.port), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\nDashboard detenido.")
        sys.exit(0)


if __name__ == '__main__':
    main()
