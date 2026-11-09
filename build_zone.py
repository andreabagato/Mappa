#!/usr/bin/env python3
"""Prepara i dati della Campania per la mappa delle isobate, zona per zona.

Scarica:
  - la griglia batimetrica EMODnet (ERDDAP, dataset bathymetry_dtm_2024)
  - la linea di costa e gli scogli/secche/relitti da OpenStreetMap (Overpass)
e scrive in site/data/<zona>/ (e un elenco delle zone in site/data/zones.json):
  - meta.json   (dimensioni e coordinate della griglia, fonti)
  - grid.i16    (quote in decimetri, interi a 16 bit little-endian,
                 righe dal nord al sud, colonne da ovest a est, -32768 = dato mancante)
  - coast.json  (tratti di costa e punti)
  - info.json   (riepilogo con pesi e conteggi)

Usa solo la libreria standard di Python: non serve installare nulla.
"""
import array
import csv
import io
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ZONES = [
    {"id": "napoli", "name": "Campania – Golfo di Napoli, Procida, Ischia e Capri",
     "lat_min": 40.50, "lon_min": 13.82, "lat_max": 40.90, "lon_max": 14.52},
    {"id": "domizio", "name": "Campania – Litorale Domizio e Flegreo",
     "lat_min": 40.70, "lon_min": 13.70, "lat_max": 41.30, "lon_max": 14.30},
    {"id": "salerno", "name": "Campania – Golfo di Salerno e Cilento nord",
     "lat_min": 40.10, "lon_min": 14.45, "lat_max": 40.70, "lon_max": 15.20},
    {"id": "cilento-sud", "name": "Campania – Cilento sud e Golfo di Policastro",
     "lat_min": 39.95, "lon_min": 15.00, "lat_max": 40.30, "lon_max": 15.75},
]
DATA_DIR = os.path.join("site", "data")
SPACING = 1.0 / 960.0          # risoluzione della griglia EMODnet (~0,00104 gradi)
MAX_CELLS_PER_REQUEST = 120000  # per non chiedere troppo in una volta sola al server
NODATA = -32768
UA = "mappa-isobate-prototipo/0.1 (progetto personale; GitHub Actions)"
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


def http_get(url, data=None, timeout=300, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            wait = 15 * (i + 1)
            print(f"  tentativo {i + 1} fallito: {e}. Riprovo tra {wait} s", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"download fallito dopo {tries} tentativi: {last}")


# ----------------------------------------------------------------------------
# Griglia EMODnet
# ----------------------------------------------------------------------------
def parse_rows(text):
    """Legge un CSV di ERDDAP (latitude, longitude, elevation).
    Restituisce ({chiave_latitudine: array('h') in decimetri}, [longitudini])."""
    if text.lstrip().startswith("<") or "error" in text[:300].lower():
        raise RuntimeError("EMODnet ha risposto con un errore: " + text[:300].replace("\n", " "))
    reader = csv.reader(io.StringIO(text))
    head = [h.strip().lower() for h in next(reader)]
    i_lat = next((i for i, h in enumerate(head) if h.startswith("lat")), -1)
    i_lon = next((i for i, h in enumerate(head) if h.startswith("lon")), -1)
    i_z = next((i for i, h in enumerate(head) if i not in (i_lat, i_lon)), -1)
    if min(i_lat, i_lon, i_z) < 0:
        raise RuntimeError("il CSV non ha le colonne latitude, longitude, elevation")
    rows = {}
    lons = []
    cur_key = None
    cur = None
    for row in reader:
        try:
            la = float(row[i_lat])
            lo = float(row[i_lon])
        except (ValueError, IndexError):
            continue  # riga delle unita' di misura o riga vuota
        try:
            z = float(row[i_z])
        except (ValueError, IndexError):
            z = float("nan")
        key = int(round(la * 1e6))
        if key != cur_key:
            cur_key = key
            cur = array.array("h")
            rows[key] = cur
        cur.append(NODATA if math.isnan(z) else max(-32767, min(32767, int(round(z * 10)))))
        if len(rows) == 1:
            lons.append(lo)
    if not rows:
        raise RuntimeError("nessun dato numerico nel file")
    return rows, lons


def assemble(rows, lons):
    """Unisce le righe (anche di richieste diverse) in una griglia con il nord in cima."""
    nx = len(lons)
    if nx < 2:
        raise RuntimeError("griglia non valida (una sola colonna)")
    keys = sorted(rows.keys(), reverse=True)
    ny = len(keys)
    if ny < 2:
        raise RuntimeError("griglia non valida (una sola riga)")
    for k in keys:
        if len(rows[k]) != nx:
            raise RuntimeError("griglia incompleta: il download potrebbe essere stato interrotto")
    span = (keys[0] - keys[-1]) / 1e6
    expected = round(span / SPACING) + 1
    if abs(expected - ny) > 1:
        raise RuntimeError("mancano delle righe nella griglia (%d trovate, %d attese)" % (ny, expected))
    vals = array.array("h")
    for k in keys:
        vals.extend(rows[k])
    meta = {
        "nx": nx, "ny": ny,
        "lon0": lons[0],
        "lat0": keys[0] / 1e6,
        "dlon": (lons[-1] - lons[0]) / (nx - 1),
        "dlat": span / (ny - 1),
    }
    return meta, vals


def lat_chunks(zone):
    ncols = int(round((zone["lon_max"] - zone["lon_min"]) / SPACING)) + 1
    step = max(20, MAX_CELLS_PER_REQUEST // ncols) * SPACING
    chunks, a = [], zone["lat_min"]
    while a < zone["lat_max"] - 1e-9:
        b = min(zone["lat_max"], a + step)
        chunks.append((max(zone["lat_min"], a - 1.5 * SPACING), b))  # piccola sovrapposizione
        a = b
    return chunks


def build_grid(zone, out_dir):
    all_rows, lons = {}, None
    chunks = lat_chunks(zone)
    print(f"  griglia EMODnet in {len(chunks)} richieste…", flush=True)
    for n, (a, b) in enumerate(chunks, 1):
        url = ("https://erddap.emodnet.eu/erddap/griddap/bathymetry_dtm_2024.csv"
               "?elevation[(%.6f):1:(%.6f)][(%s):1:(%s)]" % (a, b, zone["lon_min"], zone["lon_max"]))
        raw = http_get(url)
        print(f"    richiesta {n}/{len(chunks)}: {len(raw) / 1048576:.1f} MB", flush=True)
        rows, ln = parse_rows(raw.decode("utf-8", errors="replace"))
        if lons is None:
            lons = ln
        elif len(ln) != len(lons):
            raise RuntimeError("le richieste hanno un numero di colonne diverso")
        all_rows.update(rows)
        time.sleep(2)
    meta, vals = assemble(all_rows, lons)
    path = os.path.join(out_dir, "grid.i16")
    if sys.byteorder == "big":
        vals.byteswap()
    with open(path, "wb") as f:
        vals.tofile(f)
    if sys.byteorder == "big":
        vals.byteswap()
    valid = [v for v in vals if v != NODATA]
    stats = {
        "cells": len(vals),
        "sea_cells": sum(1 for v in valid if v < 0),
        "deepest_m": -min(valid, default=0) / 10,
        "highest_m": max(valid, default=0) / 10,
        "nodata_cells": len(vals) - len(valid),
    }
    return meta, stats, os.path.getsize(path)


# ----------------------------------------------------------------------------
# Costa e scogli da OpenStreetMap
# ----------------------------------------------------------------------------
def overpass(query):
    last = None
    for ep in OVERPASS_ENDPOINTS:
        try:
            raw = http_get(ep, data=urllib.parse.urlencode({"data": query}).encode(), timeout=200, tries=3)
            data = json.loads(raw)
            remark = str(data.get("remark", ""))
            if "error" in remark.lower() or "timed out" in remark.lower():
                raise RuntimeError("Overpass: " + remark)
            return data
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"  {ep} non ha funzionato: {e}", flush=True)
    raise RuntimeError(f"nessun server Overpass ha risposto: {last}")


def extract_coast(data):
    ways = []
    for el in data.get("elements", []):
        geom = el.get("geometry")
        if not geom or len(geom) < 2:
            continue
        flat = []
        for p in geom:
            flat.append(round(p["lat"], 6))
            flat.append(round(p["lon"], 6))
        ways.append(flat)
    return ways


def extract_points(data):
    pts = []
    for el in data.get("elements", []):
        lat = el.get("lat", (el.get("center") or {}).get("lat"))
        lon = el.get("lon", (el.get("center") or {}).get("lon"))
        if lat is None or lon is None:
            continue
        t = el.get("tags") or {}
        kind = t.get("seamark:type") or t.get("natural") or ""
        level = t.get("seamark:rock:water_level") or t.get("seamark:wreck:water_level") or ""
        pts.append({"lat": round(lat, 6), "lon": round(lon, 6), "kind": kind,
                    "name": t.get("name", ""), "level": level})
    return pts


def build_coast(zone, out_dir):
    z = zone
    bb = "%s,%s,%s,%s" % (z["lat_min"], z["lon_min"], z["lat_max"], z["lon_max"])
    print("Scarico la linea di costa da OpenStreetMap…", flush=True)
    coast = overpass('[out:json][timeout:120];way["natural"="coastline"](%s);out geom;' % bb)
    ways = extract_coast(coast)
    print(f"  {len(ways)} tratti di costa", flush=True)
    time.sleep(5)  # gentilezza verso il server
    print("Scarico scogli, secche e relitti…", flush=True)
    pdata = overpass(
        '[out:json][timeout:120];('
        'node["seamark:type"~"^(rock|wreck|obstruction)$"](%s);'
        'node["natural"~"^(reef|shoal)$"](%s);'
        'way["natural"~"^(reef|shoal)$"](%s););out center tags;' % (bb, bb, bb))
    pts = extract_points(pdata)
    print(f"  {len(pts)} segnalazioni", flush=True)
    path = os.path.join(out_dir, "coast.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ways": ways, "points": pts}, f, separators=(",", ":"), ensure_ascii=False)
    return len(ways), len(pts), os.path.getsize(path)


def build_zone(zone):
    out_dir = os.path.join(DATA_DIR, zone["id"])
    os.makedirs(out_dir, exist_ok=True)
    built = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    info = {"zone": zone["name"], "built_utc": built, "status": "ok", "problems": []}

    meta, stats, grid_bytes = build_grid(zone, out_dir)  # se fallisce, la zona viene saltata
    meta.update({
        "zone": zone["name"], "built_utc": built,
        "bbox": [zone["lat_min"], zone["lon_min"], zone["lat_max"], zone["lon_max"]],
        "units": "decimetri di quota (negativo = mare), -32768 = dato mancante",
        "source": "EMODnet Bathymetry (bathymetry_dtm_2024) e OpenStreetMap contributors (ODbL)",
    })
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1, ensure_ascii=False)
    info.update({"grid_bytes": grid_bytes, **stats, "nx": meta["nx"], "ny": meta["ny"]})

    try:
        n_ways, n_pts, coast_bytes = build_coast(zone, out_dir)
        info.update({"coast_bytes": coast_bytes, "coast_ways": n_ways, "points": n_pts})
    except Exception as e:  # noqa: BLE001
        info["status"] = "parziale"
        info["problems"].append("costa e scogli non scaricati: " + str(e))
        print("  ATTENZIONE: " + info["problems"][-1], flush=True)

    with open(os.path.join(out_dir, "info.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, indent=1, ensure_ascii=False)
    return info


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    results, manifest = [], []
    t0 = time.time()
    for zone in ZONES:
        print(f"\n=== {zone['name']} ===", flush=True)
        t1 = time.time()
        try:
            info = build_zone(zone)
            info["seconds"] = round(time.time() - t1)
            results.append((zone, info, None))
            manifest.append({"id": zone["id"], "name": zone["name"],
                             "bbox": [zone["lat_min"], zone["lon_min"], zone["lat_max"], zone["lon_max"]],
                             "status": info["status"], "grid_bytes": info["grid_bytes"],
                             "coast_bytes": info.get("coast_bytes", 0)})
        except Exception as e:  # noqa: BLE001
            print("  ERRORE in questa zona: " + str(e), flush=True)
            results.append((zone, None, str(e)))
        time.sleep(5)

    with open(os.path.join(DATA_DIR, "zones.json"), "w", encoding="utf-8") as f:
        json.dump({"built_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "zones": manifest},
                  f, indent=1, ensure_ascii=False)

    print("\n=========== RIEPILOGO ===========")
    tot = 0
    for zone, info, err in results:
        if info is None:
            print(f"{zone['id']}: NON RIUSCITA - {err}")
            continue
        size = info["grid_bytes"] + info.get("coast_bytes", 0)
        tot += size
        print(f"{zone['id']}: {info['status']}, griglia {info['nx']}x{info['ny']}, "
              f"{size / 1024:.0f} KB totali, costa {info.get('coast_ways', '-')} tratti, "
              f"punti {info.get('points', '-')}, tempo {info['seconds']} s")
        for p in info["problems"]:
            print("   PROBLEMA: " + p)
    print(f"Totale file dati: {tot / 1048576:.2f} MB. Tempo totale: {round(time.time() - t0)} s")
    print("=================================")
    if not manifest:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print("ERRORE: " + str(e), flush=True)
        sys.exit(1)
