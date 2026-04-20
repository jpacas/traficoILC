# Monitor de Flujo de Caña — Ingenio La Cabaña

Programa que monitorea en tiempo real el flujo de caña por frente desde el cuadro de "Control Tráfico" del Ingenio La Cabaña.

## ¿Qué hace?

- Interroga cada 5 minutos (o cuando haya nuevos datos) la tabla de tráfico en `https://intranet.grupolacabana.net/consultasilc/Home/Trafico`
- Guarda las lecturas en `history.json`
- Calcula el **flujo en toneladas/hora** comparando las últimas dos lecturas
- Muestra en la terminal una tabla con:
  - **TMoli(t)** — Toneladas entregadas al molino
  - **TCampo(t)** — Toneladas en campo
  - **TVienen(t)** — Toneladas en tránsito
  - **ΔTon** — Cambio en toneladas respecto a la lectura anterior
  - **Flujo(t/h)** — Ritmo de flujo en toneladas por hora

## Instalación

```bash
# Clonar o descargar este directorio
cd traficoILC

# Instalar dependencias
pip install -r requirements.txt
```

## Uso

```bash
python3 monitor.py
```

El programa entrará en un loop que:
1. Descarga la tabla cada 5 minutos
2. Detecta si hay datos nuevos (comparando el timestamp)
3. Si hay datos nuevos: los guarda, calcula flujo y muestra tabla
4. Si no hay datos nuevos: espera 1 minuto y reintenta

## Salida de ejemplo

```
======================================================================================================================
CONTROL TRÁFICO — FLUJO DE CAÑA POR FRENTE
Última actualización: 20/04/2026 12:27 pm
======================================================================================================================
+----------+-------------------------+------------+-------------+--------------+--------+--------+--------------+
| Código   | Frente                  |   TMoli(t) |   TCampo(t) |   TVienen(t) |   UVan |   ΔTon |   Flujo(t/h) |
+==========+=========================+============+=============+==============+========+========+==============+
| 1        | Ingenio Norte           |     163.11 |         120 |           25 |      1 |     50 |          600 |
| 2        | Ingenio Sur             |     185.12 |         334 |           68 |      2 |     50 |          600 |
| 39       | Mecanizado Norte 1      |     227.12 |         115 |           76 |      3 |     50 |          600 |
| ...      | ...                     |        ... |         ... |           ... |    ... |    ... |           ... |
| TOTAL    |                         |    1667.88 |        2430 |          700 |     16 |    200 |         2400 |
======================================================================================================================
```

## Archivos

| Archivo | Descripción |
|---------|-------------|
| `monitor.py` | Script principal |
| `requirements.txt` | Dependencias Python |
| `history.json` | Historial de lecturas (generado automáticamente) |

## Historial

El archivo `history.json` guarda las últimas 100 lecturas. Cada lectura contiene:
- `timestamp` — Hora de actualización de la página ("Última actualización de los datos: ...")
- `fetch_time` — Hora exacta en que se descargó la página
- `frentes` — Diccionario con datos de cada frente (por código)
- `total` — Totales de la tabla

Ejemplo:
```json
{
  "timestamp": "20/04/2026 12:27 pm",
  "fetch_time": "2026-04-20T12:27:30.123456",
  "frentes": {
    "1": {
      "frente": "Ingenio Norte",
      "umoli": 4,
      "tmoli": 113.11,
      "ucampo": 4,
      "tcampo": 120.0,
      ...
    }
  },
  "total": { ... }
}
```

## Cálculo de flujo

El flujo por frente se calcula como:

```
ΔTon = TMoli_actual - TMoli_anterior
Flujo(t/h) = ΔTon / (tiempo_transcurrido_en_horas)
```

Ejemplo: Si en 5 minutos (0.083 horas) el frente "Ingenio Norte" pasó de 100t a 150t:
- ΔTon = +50t
- Flujo = 50t / 0.083h ≈ 600 t/h

## Notas

- El programa requiere acceso a la intranet del Ingenio (red corporativa)
- La página se actualiza cada 5 minutos, así que lecturas más frecuentes no mostrarán cambios
- El timestamp en la tabla es fijo (cuando se generó el reporte) — usa `fetch_time` para tiempos precisos
- Si hay un error de conexión, el programa reintenta automáticamente cada 60 segundos
