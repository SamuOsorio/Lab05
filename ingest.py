#!/usr/bin/env python3
"""
Lab 05 - Ingesta en tiempo real del FCD de SUMO (TAPAS Cologne) a MySQL.

Lee locations.xml mientras SUMO lo va escribiendo (modo "tail -f"),
y por cada <timestep> inserta en lote las posiciones de los vehiculos.

Uso:
    python ingest.py TAPASCologne-0.32.0/locations.xml
    python ingest.py TAPASCologne-0.32.0/locations.xml --reset   # borra la tabla antes
"""
import argparse
import os
import re
import sys
import time
import json
from datetime import datetime, timezone


import pymysql

DB = dict(
    host=os.getenv("DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"),
    password=os.getenv("DB_PASS", "lab05root"),
    database=os.getenv("DB_NAME", "sumo_db"),
    autocommit=False,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    sim_time     DECIMAL(8,2)  NOT NULL,          -- segundo de simulacion
    vehicle_id   VARCHAR(64)   NOT NULL,
    lon          DOUBLE        NOT NULL,          -- x con --fcd-output.geo
    lat          DOUBLE        NOT NULL,          -- y con --fcd-output.geo
    angle        DOUBLE,
    speed        DOUBLE,
    pos          DOUBLE,
    lane         VARCHAR(64),
    slope        DOUBLE,
    vtype        VARCHAR(64),
    ingested_at  TIMESTAMP(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),  -- reloj real
    INDEX idx_sim_time (sim_time),
    INDEX idx_vehicle (vehicle_id),
    INDEX idx_ingested (ingested_at)
) ENGINE=InnoDB;
"""

INSERT = """
INSERT INTO locations
    (sim_time, vehicle_id, lon, lat, angle, speed, pos, lane, slope, vtype)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
TIMESTEP_RE = re.compile(r'<timestep\s+time="([\d.]+)"')


def fnum(v):
    return float(v) if v not in (None, "") else None

def binlog_pos(cur):
    cur.execute("SHOW MASTER STATUS")
    row = cur.fetchone()
    return {"file": row[0], "pos": int(row[1])}


def catastrophe(conn, cur, sim_now, d_from, d_to, evidence_dir):
    """Inyecta el DELETE y guarda la evidencia (filas, hora, posiciones del binlog)."""
    cur.execute("SELECT COUNT(*) FROM locations WHERE sim_time BETWEEN %s AND %s", (d_from, d_to))
    before_rows = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM locations")
    total_before = cur.fetchone()[0]
    pos_before = binlog_pos(cur)
    t = datetime.now(timezone.utc)
    sql = f"DELETE FROM locations WHERE sim_time BETWEEN {d_from} AND {d_to}"
    cur.execute(sql)
    deleted = cur.rowcount
    conn.commit()
    pos_after = binlog_pos(cur)
    ev = {
        "sql": sql,
        "sim_time_at_delete": sim_now,
        "wall_time_utc": t.isoformat(timespec="milliseconds"),
        "rows_in_range_before": before_rows,
        "rows_deleted": deleted,
        "total_rows_before": total_before,
        "binlog_before_delete": pos_before,
        "binlog_after_delete": pos_after,
    }
    os.makedirs(evidence_dir, exist_ok=True)
    with open(os.path.join(evidence_dir, "catastrophe.json"), "w") as f:
        json.dump(ev, f, indent=2)
    print("\n" + "!" * 70)
    print(f"[CATASTROFE] sim t={sim_now:.0f}s  {sql}")
    print(f"[CATASTROFE] filas borradas: {deleted:,}  binlog {pos_before} -> {pos_after}")
    print("!" * 70 + "\n", flush=True)

def follow(path):
    """Generador estilo tail -f: devuelve lineas completas a medida que aparecen."""
    while not os.path.exists(path):
        print(f"[espera] {path} todavia no existe...", flush=True)
        time.sleep(1)
    buf = ""
    idle = 0
    with open(path, "r", encoding="utf-8") as f:
        while True:
            chunk = f.readline()
            if not chunk:
                idle += 1
                time.sleep(0.2)
                continue
            idle = 0
            buf += chunk
            if not buf.endswith("\n"):
                continue  # linea incompleta, SUMO aun la esta escribiendo
            line, buf = buf, ""
            yield line


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xml", help="ruta al locations.xml que escribe SUMO")
    ap.add_argument("--reset", action="store_true", help="DROP de la tabla antes de empezar")
    ap.add_argument("--delete-at", type=float, help="segundo de simulacion en que se inyecta el DELETE")
    ap.add_argument("--delete-from", type=float, default=4000)
    ap.add_argument("--delete-to", type=float, default=6000)
    ap.add_argument("--evidence-dir", default="evidence")
    args = ap.parse_args()

    conn = pymysql.connect(**DB)
    cur = conn.cursor()
    if args.reset:
        cur.execute("DROP TABLE IF EXISTS locations")
    cur.execute(SCHEMA)
    conn.commit()
    print(f"[ok] conectado a {DB['host']}:{DB['port']}/{DB['database']}", flush=True)

    current_time = None
    batch = []
    total = 0
    last_report = 0.0
    deleted_done = False

    for line in follow(args.xml):
        s = line.strip()

        m = TIMESTEP_RE.match(s)
        if m:
            current_time = float(m.group(1))
            continue

        if s.startswith("<vehicle ") and current_time is not None:
            a = dict(ATTR_RE.findall(s))
            batch.append((
                current_time,
                a.get("id"),
                fnum(a.get("x")),
                fnum(a.get("y")),
                fnum(a.get("angle")),
                fnum(a.get("speed")),
                fnum(a.get("pos")),
                a.get("lane"),
                fnum(a.get("slope")),
                a.get("type"),
            ))
            continue

        if s.startswith("</timestep>"):
            if batch:
                cur.executemany(INSERT, batch)
                conn.commit()  # un commit por timestep
                total += len(batch)
                batch.clear()
            if (args.delete_at is not None and not deleted_done
                    and current_time >= args.delete_at):
                catastrophe(conn, cur, current_time, args.delete_from,
                            args.delete_to, args.evidence_dir)
                deleted_done = True
            if current_time - last_report >= 100:
                print(f"[sim t={current_time:>8.0f}s] filas totales: {total:,}", flush=True)
                last_report = current_time
            continue

        if s.startswith("</fcd-export>"):
            print(f"[fin] simulacion terminada. Filas insertadas: {total:,}", flush=True)
            break

    cur.close()
    conn.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[stop] interrumpido por el usuario", file=sys.stderr)
