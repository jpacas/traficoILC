#!/usr/bin/env python3
"""
Monitor de Flujo de Caña por Frente
Interroga cada hora la tabla de tráfico del Ingenio La Cabaña,
calcula el flujo (delta de toneladas) por frente y muestra el resultado.
"""

import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
import time
from tabulate import tabulate
import os
import psycopg2
from psycopg2.extras import Json
from pathlib import Path

URL = "https://intranet.grupolacabana.net/consultasilc/Home/Trafico"
DATABASE_URL = os.environ.get('DATABASE_URL')
POLL_INTERVAL = 300  # 5 minutos
RETRY_INTERVAL = 60  # Si no hay datos nuevos, reintentar en 1 minuto


def fetch_table() -> dict:
    """Descarga la página y parsea la tabla."""
    try:
        resp = requests.get(URL, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        # Buscar el timestamp "Última actualización"
        page_text = soup.get_text()
        timestamp_str = None
        for line in page_text.split('\n'):
            if 'Última actualización de los datos:' in line:
                # Extrae formato: "Última actualización de los datos: 20/04/2026 12:22 pm"
                try:
                    ts_part = line.split('Última actualización de los datos:')[1].strip()
                    timestamp_str = ts_part
                    break
                except:
                    pass

        # Parsear la tabla
        table = soup.find('table')
        if not table:
            return None

        rows = table.find_all('tr')
        frentes = {}

        for row in rows[1:]:  # Skip header
            cols = row.find_all('td')
            if len(cols) < 14:  # Esperamos 14 columnas (codigo, frente, 12 datos)
                continue

            try:
                codigo = cols[0].get_text().strip()
                frente = cols[1].get_text().strip()

                if frente == 'Total' or not codigo or codigo == 'Codigo':
                    continue

                umoli = int(cols[2].get_text().strip() or 0)
                tmoli = float(cols[3].get_text().strip() or 0)
                upatio = int(cols[4].get_text().strip() or 0)
                tpatio = float(cols[5].get_text().strip() or 0)
                uplantel = int(cols[6].get_text().strip() or 0)
                tplantel = float(cols[7].get_text().strip() or 0)
                uvienen = int(cols[8].get_text().strip() or 0)
                tvienen = float(cols[9].get_text().strip() or 0)
                ucampo = int(cols[10].get_text().strip() or 0)
                tcampo = float(cols[11].get_text().strip() or 0)
                uvan = int(cols[12].get_text().strip() or 0)
                tvan = float(cols[13].get_text().strip() or 0)

                frentes[codigo] = {
                    'frente': frente,
                    'umoli': umoli,
                    'tmoli': tmoli,
                    'upatio': upatio,
                    'tpatio': tpatio,
                    'uplantel': uplantel,
                    'tplantel': tplantel,
                    'uvienen': uvienen,
                    'tvienen': tvienen,
                    'ucampo': ucampo,
                    'tcampo': tcampo,
                    'uvan': uvan,
                    'tvan': tvan,
                }
            except (IndexError, ValueError):
                continue

        # Buscar total
        total_row = None
        for row in rows:
            cols = row.find_all('td')
            if cols and 'Total' in cols[1].get_text():
                total_row = cols
                break

        total_data = {}
        if total_row and len(total_row) >= 14:
            try:
                total_data = {
                    'umoli': int(total_row[2].get_text().strip() or 0),
                    'tmoli': float(total_row[3].get_text().strip() or 0),
                    'upatio': int(total_row[4].get_text().strip() or 0),
                    'tpatio': float(total_row[5].get_text().strip() or 0),
                    'uplantel': int(total_row[6].get_text().strip() or 0),
                    'tplantel': float(total_row[7].get_text().strip() or 0),
                    'uvienen': int(total_row[8].get_text().strip() or 0),
                    'tvienen': float(total_row[9].get_text().strip() or 0),
                    'ucampo': int(total_row[10].get_text().strip() or 0),
                    'tcampo': float(total_row[11].get_text().strip() or 0),
                    'uvan': int(total_row[12].get_text().strip() or 0),
                    'tvan': float(total_row[13].get_text().strip() or 0),
                }
            except:
                pass

        return {
            'timestamp': timestamp_str,
            'fetch_time': datetime.now(timezone.utc).isoformat(),
            'frentes': frentes,
            'total': total_data
        }

    except Exception as e:
        print(f"Error descargando tabla: {e}")
        return None


def init_db():
    """Crea tabla si no existe."""
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL no configurada")
        return False

    try:
        print("Conectando a PostgreSQL...")
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        print("Creando tabla 'readings'...")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id SERIAL PRIMARY KEY,
                fetch_time TIMESTAMPTZ NOT NULL UNIQUE,
                data JSONB NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        conn.commit()
        print("✓ Tabla 'readings' creada exitosamente")

        cur.close()
        conn.close()
        return True
    except Exception as e:
        print(f"✗ Error inicializando DB: {e}")
        import traceback
        traceback.print_exc()
        return False


def load_history() -> list:
    """Carga últimas 100 lecturas de PostgreSQL."""
    if not DATABASE_URL:
        return []
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            SELECT data FROM readings
            ORDER BY fetch_time DESC
            LIMIT 100
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [json.loads(row[0]) if isinstance(row[0], str) else row[0] for row in reversed(rows)]
    except Exception as e:
        print(f"Error cargando histórico: {e}")
        return []


def save_reading(reading):
    """Inserta lectura en PostgreSQL; elimina si > 100 filas."""
    if not DATABASE_URL:
        return
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO readings (fetch_time, data)
            VALUES (%s, %s)
            ON CONFLICT (fetch_time) DO NOTHING
        """, (reading['fetch_time'], Json(reading)))

        cur.execute("""
            DELETE FROM readings WHERE id NOT IN (
                SELECT id FROM readings ORDER BY fetch_time DESC LIMIT 576
            )
        """)

        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Error guardando lectura: {e}")


def calculate_flow(current, previous) -> dict:
    """Para cada frente, calcula delta de TMoli y flujo en ton/hora."""
    flow = {}

    try:
        current_ts = datetime.fromisoformat(current['fetch_time'])
        prev_ts = datetime.fromisoformat(previous['fetch_time'])
        elapsed_seconds = (current_ts - prev_ts).total_seconds()
        elapsed_hours = elapsed_seconds / 3600 if elapsed_seconds > 0 else 1
    except:
        elapsed_hours = 1

    for codigo, curr_data in current['frentes'].items():
        if codigo not in previous['frentes']:
            flow[codigo] = {
                'delta_ton': 0,
                'flow_ton_per_hour': 0
            }
            continue

        prev_data = previous['frentes'][codigo]
        delta_ton = curr_data['tmoli'] - prev_data['tmoli']
        flow_ton_per_hour = delta_ton / elapsed_hours if elapsed_hours > 0 else 0

        flow[codigo] = {
            'delta_ton': round(delta_ton, 2),
            'flow_ton_per_hour': round(flow_ton_per_hour, 2)
        }

    return flow


def display_report(current, flow_rates=None):
    """Imprime tabla formateada en terminal."""
    print("\n" + "=" * 110)
    print("CONTROL TRÁFICO — FLUJO DE CAÑA POR FRENTE")
    print(f"Última actualización: {current['timestamp']}")
    print("=" * 110)

    table_data = []

    for codigo, data in sorted(current['frentes'].items(), key=lambda x: int(x[0]) if x[0].isdigit() else 999):
        row = [
            codigo,
            data['frente'][:30],  # Limitar longitud
            f"{data['tmoli']:.2f}",
            f"{data['tcampo']:.2f}",
            f"{data['tvienen']:.2f}",
            f"{data['uvan']}",
        ]

        if flow_rates and codigo in flow_rates:
            flow = flow_rates[codigo]
            row.append(f"{flow['delta_ton']:+.2f}")
            row.append(f"{flow['flow_ton_per_hour']:+.1f}")
        else:
            row.append("—")
            row.append("—")

        table_data.append(row)

    # Fila de total
    if current['total']:
        total = current['total']
        row = [
            "TOTAL",
            "",
            f"{total['tmoli']:.2f}",
            f"{total['tcampo']:.2f}",
            f"{total['tvienen']:.2f}",
            f"{total['uvan']}",
        ]

        if flow_rates:
            total_delta = sum(f['delta_ton'] for f in flow_rates.values())
            total_flow = sum(f['flow_ton_per_hour'] for f in flow_rates.values())
            row.append(f"{total_delta:+.2f}")
            row.append(f"{total_flow:+.1f}")
        else:
            row.append("—")
            row.append("—")

        table_data.append(row)

    headers = ["Código", "Frente", "TMoli(t)", "TCampo(t)", "TVienen(t)", "UVan", "ΔTon", "Flujo(t/h)"]
    print(tabulate(table_data, headers=headers, tablefmt="grid", numalign="right"))
    print("=" * 110)


def data_changed(current, previous):
    """Detecta si los datos han cambiado (comparando totales)."""
    if previous is None:
        return True
    curr_total = current.get('total', {})
    prev_total = previous.get('total', {})
    return curr_total != prev_total


def main():
    """Loop principal."""
    print("Monitor de Flujo de Caña")
    if DATABASE_URL:
        print("Modo: PostgreSQL")
        print("Inicializando base de datos...")
        # Reintentar init_db() hasta 3 veces
        for attempt in range(1, 4):
            if init_db():
                break
            if attempt < 3:
                print(f"Reintentando en 5s... (intento {attempt}/3)")
                time.sleep(5)
    else:
        print("ADVERTENCIA: DATABASE_URL no configurada. Modo offline.")
    print(f"URL: {URL}")
    print(f"Intervalo de sondeo: {POLL_INTERVAL}s\n")

    history = load_history()
    last_timestamp = history[-1]['timestamp'] if history else None

    while True:
        reading = fetch_table()

        if not reading:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Error descargando datos. Reintentando en {RETRY_INTERVAL}s...")
            time.sleep(RETRY_INTERVAL)
            continue

        # Siempre guardar: fetch_time es único por llamada, así el cálculo 1h
        # siempre encuentra su par histórico aunque la fuente no haya cambiado.
        source_changed = reading['timestamp'] != last_timestamp
        last_timestamp = reading['timestamp']
        save_reading(reading)
        history = load_history()

        display_report(reading)
        print(f"  [fuente {'actualizada' if source_changed else 'sin cambios'}]")

        if len(history) >= 2:
            calculate_flow(history[-1], history[-2])
        else:
            print("\n  (Primera lectura — no hay delta disponible aún)")

        print(f"\nPróxima lectura en {POLL_INTERVAL}s...")
        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()
