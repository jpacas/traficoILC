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

# Cada día a las 7 AM (hora Colombia, UTC-5) se reinicia el contador tmoli
# para el inicio de una nueva jornada zafra.
ZAFRA_RESET_HOUR = 7
ZAFRA_UTC_OFFSET_H = -5

# Para calcular flujos horarios se busca la lectura más cercana a 1 hora antes
# (funciona con historial de lecturas cada 5 min y con sondeo horario).
TARGET_DELTA_SEC = 3600   # 1 hora
TOLERANCE_DELTA_SEC = 900  # ±15 minutos


def _find_reading_before(timestamps, idx):
    """Devuelve el índice de la lectura más cercana a TARGET_DELTA_SEC antes de timestamps[idx].
    Retorna None si no hay ninguna dentro de TOLERANCE_DELTA_SEC."""
    curr_ts = timestamps[idx]
    if curr_ts is None:
        return None
    best_j = None
    best_diff = float('inf')
    for j in range(idx - 1, -1, -1):
        ts = timestamps[j]
        if ts is None:
            continue
        delta = (curr_ts - ts).total_seconds()
        if delta < 0:
            continue
        diff = abs(delta - TARGET_DELTA_SEC)
        if diff < best_diff:
            best_diff = diff
            best_j = j
        if delta > TARGET_DELTA_SEC + TOLERANCE_DELTA_SEC:
            break
    if best_j is None or best_diff > TOLERANCE_DELTA_SEC:
        return None
    return best_j


def crosses_zafra_boundary(prev_ts, curr_ts):
    """Devuelve True si el par de lecturas cruza el reset de las 7 AM (jornada zafra)."""
    def zafra_day(ts):
        local = ts + timedelta(hours=ZAFRA_UTC_OFFSET_H)
        if local.hour < ZAFRA_RESET_HOUR:
            return local.date() - timedelta(days=1)
        return local.date()
    return zafra_day(prev_ts) != zafra_day(curr_ts)


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


def _calculate_stage_flows(curr_frente):
    """Devuelve los snapshots actuales de cada stage (para display dentro de cajas).
    Los flujos se calculan aparte con histórico."""
    result = {}

    stages = {
        'campo': ('tcampo', 'ucampo'),
        'vienen': ('tvienen', 'uvienen'),
        'patio': ('tpatio', 'upatio'),
        'plantel': ('tplantel', 'uplantel'),
        'molino': ('tmoli', 'umoli'),
        'van': ('tvan', 'uvan')
    }

    for stage_name, (t_key, u_key) in stages.items():
        result[stage_name] = {
            'current_t': round(curr_frente.get(t_key, 0), 2),
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

    # Pre-calcular timestamps para búsqueda eficiente de pares horarios
    timestamps = [parse_fetch_time(r['fetch_time']) for r in history]

    # Flujo puntual: buscar lectura ~1 hora antes de la más reciente
    elapsed_h = None
    previous = None
    curr_idx = len(history) - 1
    prev_idx = _find_reading_before(timestamps, curr_idx)
    if prev_idx is not None:
        previous = history[prev_idx]
        curr_ts_cur = timestamps[curr_idx]
        prev_ts_cur = timestamps[prev_idx]
        if not crosses_zafra_boundary(prev_ts_cur, curr_ts_cur):
            elapsed_h = (curr_ts_cur - prev_ts_cur).total_seconds() / 3600

    # Calcular flujos históricos: para cada lectura, buscar par ~1 hora antes
    historical_flows = {}
    historical_stage_flows = {}
    for codigo in current['frentes'].keys():
        historical_flows[codigo] = []
        historical_stage_flows[codigo] = {
            'plantel': [],   # patio→plantel (balance de masa)
            'patio':   [],   # vienen→patio  (balance de masa)
            'vienen':  [],   # campo→vienen  (balance de masa)
        }

    for i in range(1, len(history)):
        j = _find_reading_before(timestamps, i)
        if j is None:
            continue
        curr_ts = timestamps[i]
        prev_ts = timestamps[j]
        if crosses_zafra_boundary(prev_ts, curr_ts):
            continue
        elapsed_h_pair = (curr_ts - prev_ts).total_seconds() / 3600

        curr = history[i]
        prev = history[j]
        for codigo, curr_data in curr['frentes'].items():
            if codigo in prev['frentes']:
                prev_data = prev['frentes'][codigo]
                delta_moli    = curr_data['tmoli']    - prev_data['tmoli']
                delta_plantel = curr_data['tplantel'] - prev_data['tplantel']
                delta_patio   = curr_data['tpatio']   - prev_data['tpatio']
                delta_vienen  = curr_data['tvienen']  - prev_data['tvienen']

                if delta_moli >= 0:
                    flow_moli = delta_moli / elapsed_h_pair
                    historical_flows[codigo].append(flow_moli)

                    # Balance de masa hacia atrás: flow_in = flow_out + Δinventario
                    flow_plantel = max(0.0, flow_moli    + delta_plantel / elapsed_h_pair)
                    flow_patio   = max(0.0, flow_plantel + delta_patio   / elapsed_h_pair)
                    flow_vienen  = max(0.0, flow_patio   + delta_vienen  / elapsed_h_pair)

                    historical_stage_flows[codigo]['plantel'].append(flow_plantel)
                    historical_stage_flows[codigo]['patio'].append(flow_patio)
                    historical_stage_flows[codigo]['vienen'].append(flow_vienen)

    # Promediar: al menos 5 flujos si disponibles, sino usar todos disponibles
    avg_flows = {}
    avg_stage_flows = {}
    for codigo in current['frentes'].keys():
        flows = historical_flows[codigo]
        # Si hay 5+, promediar los últimos 5. Si hay menos, promediar todos.
        avg_flows[codigo] = mean(flows[-5:]) if len(flows) >= 5 else (mean(flows) if flows else None)

        avg_stage_flows[codigo] = {}
        for stage in ['plantel', 'patio', 'vienen']:
            flows_stage = historical_stage_flows[codigo][stage]
            avg_stage_flows[codigo][stage] = mean(flows_stage[-5:]) if len(flows_stage) >= 5 else (mean(flows_stage) if flows_stage else None)

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
            "stages": {stage: {
                **_calculate_stage_flows(curr_frente)[stage],
                'flow_tph': (
                    round(avg_flows.get(codigo), 2) if stage == 'molino' and avg_flows.get(codigo) is not None else
                    round(avg_stage_flows[codigo].get(stage), 2) if stage in ['plantel', 'patio', 'vienen'] and avg_stage_flows[codigo].get(stage) is not None else
                    None
                )
            } for stage in _calculate_stage_flows(curr_frente)},
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

    # Calcular flujos globales por etapa: suma de flujos de cada frente
    # Como check: sum(frente_flows) == total_flow
    global_stages = _calculate_stage_flows(current['total'])
    for stage in ['plantel', 'patio', 'vienen']:
        stage_flow_sum = sum(
            avg_stage_flows[codigo][stage]
            for codigo in avg_stage_flows.keys()
            if avg_stage_flows[codigo].get(stage) is not None
        )
        if stage_flow_sum > 0:
            global_stages[stage]['flow_tph'] = round(stage_flow_sum, 2)
    if total_avg_flow > 0:
        global_stages['molino']['flow_tph'] = round(total_avg_flow, 2)

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
