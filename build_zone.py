#!/usr/bin/env python3
"""Prepara i dati del Golfo di Napoli per la mappa delle isobate.

Scarica:
  - la griglia batimetrica EMODnet (ERDDAP, dataset bathymetry_dtm_2024)
  - la linea di costa e gli scogli/secche/relitti da OpenStreetMap (Overpass)
e scrive in site/data/napoli/:
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

ZONE = {
    "id": "napoli",
    "name": "Golfo di Napoli, Procida, Ischia e Capri",
    "lat_min": 40.50, "lon_min": 13.82,
    "lat_max": 40.90, "lon_max": 14.52,
}
OUT_DIR = os.path.join("site", "data", ZONE["id"])
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
def parse_grid_csv(text):
    """Legge il CSV di ERDDAP (latitude, longitude, elevation) e restituisce
    (meta, valori) con righe dal nord al sud. valori = array('h') in decimetri."""
    if text.lstrip().startswith("<") or "error" in text[:300].lower():
        raise RuntimeError("EMODnet ha risposto con un errore: " + text[:300].replace("\n", " "))
    reader = csv.reader(io.StringIO(text))
    head = [h.strip().lower() for h in next(reader)]
    i_lat = next((i for i, h in enumerate(head) if h.startswith("lat")), -1)
    i_lon = next((i for i, h in enumerate(head) if h.startswith("lon")), -1)
    i_z = next((i for i, h in enumerate(head) if i not in (i_lat, i_lon)), -1)
    if min(i_lat, i_lon, i_z) < 0:
        raise RuntimeError("il CSV non ha le colonne latitude, longitude, elevation")

    vals = array.array("h")
    first_lat = first_lon = lon_end_first = second_lat = last_lat = None
    nx = 0
    row_done = False
    for row in reader:
        try:
            la = float(row[i_lat])
            lo = float(row[i_lon])
        except (ValueError, IndexError):
            continue  # riga delle unità di misura o riga vuota
        try:
            z = float(row[i_z])
        except (ValueError, IndexError):
            z = float("nan")
        if first_lat is None:
            first_lat, first_lon = la, lo
        if not row_done:
            if len(vals) > 0 and la != first_lat:
                row_done = True
                nx = len(vals)
                second_lat = la
            else:
                lon_end_first = lo
        if math.isnan(z):
            vals.append(NODATA)
        else:
            vals.append(max(-32767, min(32767, int(round(z * 10)))))
        last_lat = la

    if not vals:
        raise RuntimeError("nessun dato numerico nel file")
    if not row_done or nx < 2:
        raise RuntimeError("griglia non valida (una sola riga o colonna)")
    ny = len(vals) // nx
    if ny * nx != len(vals):
        raise RuntimeError("griglia incompleta: il download potrebbe essere stato interrotto")

    if second_lat > first_lat:  # latitudini crescenti: porto il nord in cima
        flipped = array.array("h")
        for r in range(ny - 1, -1, -1):
            flipped.extend(vals[r * nx:(r + 1) * nx])
        vals = flipped

    meta = {
        "nx": nx, "ny": ny,
        "lon0": first_lon,
        "lat0": max(first_lat, last_lat),
        "dlon": (lon_end_first - first_lon) / (nx - 1),
        "dlat": abs(last_lat - first_lat) / (ny - 1),
    }
    return meta, vals


def build_grid():
    z = ZONE
    url = ("https://erddap.emodnet.eu/erddap/griddap/bathymetry_dtm_2024.csv"
           "?elevation[(%s):1:(%s)][(%s):1:(%s)]" % (z["lat_min"], z["lat_max"], z["lon_min"], z["lon_max"]))
    print("Scarico la griglia EMODnet…", flush=True)
    raw = http_get(url)
    print(f"  ricevuti {len(raw) / 1048576:.1f} MB di CSV", flush=True)
    meta, vals = parse_grid_csv(raw.decode("utf-8", errors="replace"))
    if sys.byteorder == "big":
        vals.byteswap()
    path = os.path.join(OUT_DIR, "grid.i16")
    with open(path, "wb") as f:
        vals.tofile(f)
    if sys.byteorder == "big":
        vals.byteswap()
    sea = sum(1 for v in vals if v != NODATA and v < 0)
    stats = {
        "cells": len(vals),
        "sea_cells": sea,
        "deepest_m": -min((v for v in vals if v != NODATA), default=0) / 10,
        "highest_m": max((v for v in vals if v != NODATA), default=0) / 10,
        "nodata_cells": sum(1 for v in vals if v == NODATA),
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


def build_coast():
    z = ZONE
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
    path = os.path.join(OUT_DIR, "coast.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ways": ways, "points": pts}, f, separators=(",", ":"), ensure_ascii=False)
    return len(ways), len(pts), os.path.getsize(path)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    built = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    info = {"zone": ZONE["name"], "built_utc": built, "status": "ok", "problems": []}

    meta, stats, grid_bytes = build_grid()  # se fallisce, si ferma qui
    meta.update({
        "zone": ZONE["name"], "built_utc": built,
        "bbox": [ZONE["lat_min"], ZONE["lon_min"], ZONE["lat_max"], ZONE["lon_max"]],
        "units": "decimetri di quota (negativo = mare), -32768 = dato mancante",
        "source": "EMODnet Bathymetry (bathymetry_dtm_2024) e OpenStreetMap contributors (ODbL)",
    })
    with open(os.path.join(OUT_DIR, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1, ensure_ascii=False)
    info.update({"grid_bytes": grid_bytes, **stats, "nx": meta["nx"], "ny": meta["ny"]})

    try:
        n_ways, n_pts, coast_bytes = build_coast()
        info.update({"coast_bytes": coast_bytes, "coast_ways": n_ways, "points": n_pts})
    except Exception as e:  # noqa: BLE001
        info["status"] = "parziale"
        info["problems"].append("costa e scogli non scaricati: " + str(e))
        print("ATTENZIONE: " + info["problems"][-1], flush=True)

    with open(os.path.join(OUT_DIR, "info.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, indent=1, ensure_ascii=False)

    print("\n=========== RIEPILOGO ===========")
    print(f"Stato: {info['status']}")
    print(f"Griglia: {meta['nx']} x {meta['ny']} celle, file {grid_bytes / 1024:.0f} KB")
    print(f"Profondita massima: {stats['deepest_m']:.0f} m, quota massima: {stats['highest_m']:.0f} m, "
          f"celle senza dato: {stats['nodata_cells']}")
    if "coast_bytes" in info:
        print(f"Costa: {info['coast_ways']} tratti, scogli/secche/relitti: {info['points']}, "
              f"file {info['coast_bytes'] / 1024:.0f} KB")
    for p in info["problems"]:
        print("PROBLEMA: " + p)
    print("=================================")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print("ERRORE: " + str(e), flush=True)
        sys.exit(1)
