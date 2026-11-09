#!/usr/bin/env python3
"""Prepara i tasselli batimetrici a tre livelli (base, medio, fine) per il test Campania-Sardegna.

Livelli (tutti con 240 celle per lato + 1 di sovrapposizione):
  C = 1 cella ogni 16 della griglia EMODnet (~1,7 km), tasselli di 4 gradi
  M = 1 cella ogni 4  (~420 m),                         tasselli di 1 grado
  F = tutte le celle  (~100 m),                         tasselli di 0,25 gradi
M si genera dove c'e' fondale tra 90 e 350 m; F dove c'e' fondale sotto i 125 m.
Scrive in tiles-data/: tiles.json, C|M|F/<ti>_<tj>.bin.gz, coast/<ti>_<tj>.json
Valori: quote in quarti di metro (int16 little-endian, nord in alto, -32768 = mancante).
Solo libreria standard di Python.
"""
import array, csv, gzip, io, json, math, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

BBOX = {"lat_min": 39.0, "lat_max": 41.3, "lon_min": 9.5, "lon_max": 15.8}  # tratta Campania-Sardegna
OUT = "tiles-data"
SP = 960          # celle per grado
CELLS = 240
SCALE = 4         # quarti di metro
NODATA = -32768
STRIDE = {"C": 16, "M": 4, "F": 1}
MAX_FINE = 220    # limite di sicurezza sul numero di tasselli fini
BASE = "https://erddap.emodnet.eu/erddap/griddap/bathymetry_dtm_2024.csv"
UA = "mappa-isobate-prototipo/0.2 (progetto personale; GitHub Actions)"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]


def get(url, data=None, timeout=300, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"   tentativo {i + 1} fallito: {e}", flush=True)
            time.sleep(10 * (i + 1))
    raise RuntimeError(f"download fallito: {last}")


def first_float(text):
    for line in text.splitlines()[1:]:
        try:
            return float(line.split(",")[0])
        except ValueError:
            continue
    raise RuntimeError("valore non trovato: " + text[:200])


def anchors():
    la = get(BASE + "?latitude[(34.0):1:(34.01)]").decode()
    lo = get(BASE + "?longitude[(5.0):1:(5.01)]").decode()
    return first_float(la), first_float(lo)


def tile_range(level, lo, hi, a0):
    st = CELLS * STRIDE[level] / SP
    return range(math.floor((lo - a0) / st), math.floor((hi - a0) / st) + 1)


def fetch_tile(level, ti, tj, L0, M0):
    """Restituisce un array 241x241 (nord in alto) di quarti di metro."""
    s = STRIDE[level]
    i0, j0 = CELLS * s * ti, CELLS * s * tj
    la, lb = L0 + i0 / SP, L0 + (i0 + CELLS * s) / SP
    oa, ob = M0 + j0 / SP, M0 + (j0 + CELLS * s) / SP
    url = BASE + "?elevation[(%.7f):%d:(%.7f)][(%.7f):%d:(%.7f)]" % (la, s, lb, oa, s, ob)
    text = get(url).decode("utf-8", errors="replace")
    if text.lstrip().startswith("<") or "error" in text[:300].lower():
        raise RuntimeError("risposta di errore: " + text[:200].replace("\n", " "))
    rd = csv.reader(io.StringIO(text))
    head = [h.strip().lower() for h in next(rd)]
    il, io_, iz = [next(i for i, h in enumerate(head) if h.startswith(p)) for p in ("lat", "lon", "elev")]
    rows, cur, ck = {}, None, None
    for r in rd:
        try:
            lat = float(r[il]); float(r[io_])
        except (ValueError, IndexError):
            continue
        k = round(lat * 1e6)
        if k != ck:
            ck, cur = k, array.array("h"); rows[k] = cur
        try:
            z = float(r[iz])
        except ValueError:
            z = float("nan")
        cur.append(NODATA if math.isnan(z) else max(-32767, min(32767, int(round(z * SCALE)))))
    keys = sorted(rows, reverse=True)
    if len(keys) != CELLS + 1 or any(len(rows[k]) != CELLS + 1 for k in keys):
        raise RuntimeError("tassello incompleto (%d righe)" % len(keys))
    if abs(keys[-1] / 1e6 - la) > 2e-5:
        raise RuntimeError("tassello non allineato alla griglia")
    out = array.array("h")
    for k in keys:
        out.extend(rows[k])
    return out


def write_tile(level, ti, tj, arr):
    d = os.path.join(OUT, level)
    os.makedirs(d, exist_ok=True)
    a = array.array("h", arr)
    if sys.byteorder == "big":
        a.byteswap()
    path = os.path.join(d, f"{ti}_{tj}.bin.gz")
    with open(path, "wb") as f:
        f.write(gzip.compress(a.tobytes(), 9))
    return os.path.getsize(path)


# ---- costa e scogli ----
def overpass(q):
    last = None
    for ep in OVERPASS:
        try:
            data = json.loads(get(ep, data=urllib.parse.urlencode({"data": q}).encode(), timeout=200, tries=3))
            rem = str(data.get("remark", "")).lower()
            if "error" in rem or "timed out" in rem:
                raise RuntimeError(rem)
            return data
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"Overpass: {last}")


def clip_run(pts, s, w, n, e):
    """Taglia una polilinea (lista di (lat,lon)) sul rettangolo; mantiene il verso."""
    runs, cur = [], []
    for (la1, lo1), (la2, lo2) in zip(pts, pts[1:]):
        t0, t1 = 0.0, 1.0
        dx, dy = lo2 - lo1, la2 - la1
        ok = True
        for p, q in ((-dx, lo1 - w), (dx, e - lo1), (-dy, la1 - s), (dy, n - la1)):
            if p == 0:
                if q < 0:
                    ok = False
                    break
            else:
                r = q / p
                if p < 0:
                    t0 = max(t0, r)
                else:
                    t1 = min(t1, r)
        if not ok or t0 > t1:
            if len(cur) > 1:
                runs.append(cur)
            cur = []
            continue
        a = (la1 + t0 * dy, lo1 + t0 * dx)
        b = (la1 + t1 * dy, lo1 + t1 * dx)
        if not cur or t0 > 0:
            if len(cur) > 1:
                runs.append(cur)
            cur = [a]
        cur.append(b)
        if t1 < 1:
            runs.append(cur)
            cur = []
    if len(cur) > 1:
        runs.append(cur)
    return runs


def coast_tile(ti, tj, L0, M0):
    st = CELLS * 4 / SP
    s, w = L0 + ti * st, M0 + tj * st
    n, e = s + st, w + st
    m = 0.01
    bb = "%.5f,%.5f,%.5f,%.5f" % (s - m, w - m, n + m, e + m)
    q = ('[out:json][timeout:150];(way["natural"="coastline"](%s);'
         'node["seamark:type"~"^(rock|wreck|obstruction)$"](%s);'
         'node["natural"~"^(reef|shoal)$"](%s);way["natural"~"^(reef|shoal)$"](%s););out geom tags;' % (bb, bb, bb, bb))
    ways, pts = [], []
    for el in overpass(q).get("elements", []):
        t = el.get("tags") or {}
        if el["type"] == "way" and t.get("natural") == "coastline" and el.get("geometry"):
            line = [(p["lat"], p["lon"]) for p in el["geometry"]]
            for run in clip_run(line, s - m, w - m, n + m, e + m):
                ways.append([round(v, 6) for p in run for v in p])
        else:
            if el["type"] == "node":
                la, lo = el["lat"], el["lon"]
            elif el.get("geometry"):
                la = sum(p["lat"] for p in el["geometry"]) / len(el["geometry"])
                lo = sum(p["lon"] for p in el["geometry"]) / len(el["geometry"])
            else:
                continue
            if s <= la < n and w <= lo < e:  # ogni punto in un solo tassello
                pts.append({"lat": round(la, 6), "lon": round(lo, 6), "kind": t.get("seamark:type") or t.get("natural", ""),
                            "name": t.get("name", ""), "level": t.get("seamark:rock:water_level", "")})
    if not ways and not pts:
        return 0
    d = os.path.join(OUT, "coast")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{ti}_{tj}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ways": ways, "points": pts}, f, separators=(",", ":"))
    return os.path.getsize(path)


def main():
    t0 = time.time()
    L0, M0 = anchors()
    print(f"Ancore griglia: lat {L0}, lon {M0}", flush=True)
    B = BBOX
    report = {"C": [0, 0], "M": [0, 0], "F": [0, 0], "coast": [0, 0]}  # [file, byte]
    fails, levels, fine_wanted = [], {"C": [], "M": [], "F": []}, set()

    for ti in tile_range("C", B["lat_min"], B["lat_max"], L0):
        for tj in tile_range("C", B["lon_min"], B["lon_max"], M0):
            try:
                b = write_tile("C", ti, tj, fetch_tile("C", ti, tj, L0, M0))
                levels["C"].append(f"{ti}_{tj}"); report["C"][0] += 1; report["C"][1] += b
                print(f"C {ti}_{tj} ok", flush=True)
            except Exception as e:  # noqa: BLE001
                fails.append(f"C {ti}_{tj}: {e}")
            time.sleep(2)

    for ti in tile_range("M", B["lat_min"], B["lat_max"], L0):
        for tj in tile_range("M", B["lon_min"], B["lon_max"], M0):
            try:
                arr = fetch_tile("M", ti, tj, L0, M0)
            except Exception as e:  # noqa: BLE001
                fails.append(f"M {ti}_{tj}: {e}")
                continue
            # tassello medio tenuto se contiene fondale tra 90 e 350 m
            if any(-350 * SCALE <= v <= -90 * SCALE for v in arr if v != NODATA):
                b = write_tile("M", ti, tj, arr)
                levels["M"].append(f"{ti}_{tj}"); report["M"][0] += 1; report["M"][1] += b
            # tasselli fini necessari: 0,25 gradi = 60 celle medie
            rows = [arr[r * 241:(r + 1) * 241] for r in range(241)][::-1]  # sud -> nord
            for a in range(4):
                for c in range(4):
                    for r in range(a * 60, a * 60 + 61):
                        if any(-125 * SCALE <= v < 0 for v in rows[r][c * 60:c * 60 + 61]):
                            fine_wanted.add((ti * 4 + a, tj * 4 + c))
                            break
            print(f"M {ti}_{tj} ok", flush=True)
            time.sleep(2)

    fine = sorted(fine_wanted)[:MAX_FINE]
    if len(fine_wanted) > MAX_FINE:
        fails.append(f"troppi tasselli fini ({len(fine_wanted)}): scaricati solo {MAX_FINE}")
    for n, (ti, tj) in enumerate(fine, 1):
        try:
            b = write_tile("F", ti, tj, fetch_tile("F", ti, tj, L0, M0))
            levels["F"].append(f"{ti}_{tj}"); report["F"][0] += 1; report["F"][1] += b
        except Exception as e:  # noqa: BLE001
            fails.append(f"F {ti}_{tj}: {e}")
        if n % 10 == 0:
            print(f"F {n}/{len(fine)}", flush=True)
        time.sleep(2)

    coast = []
    for ti in tile_range("M", B["lat_min"], B["lat_max"], L0):
        for tj in tile_range("M", B["lon_min"], B["lon_max"], M0):
            try:
                b = coast_tile(ti, tj, L0, M0)
                if b:
                    coast.append(f"{ti}_{tj}"); report["coast"][0] += 1; report["coast"][1] += b
            except Exception as e:  # noqa: BLE001
                fails.append(f"costa {ti}_{tj}: {e}")
            time.sleep(6)

    meta = {"built_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "lat0": L0, "lon0": M0,
            "scale": SCALE, "levels": levels, "coast": coast, "bbox": B, "failed": fails}
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "tiles.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, separators=(",", ":"))

    print("\n=========== RIEPILOGO ===========")
    tot = 0
    for k, (n, b) in report.items():
        tot += b
        print(f"{k}: {n} file, {b / 1024:.0f} KB")
    print(f"Totale: {tot / 1048576:.2f} MB. Tempo: {round(time.time() - t0)} s. Problemi: {len(fails)}")
    for p in fails[:30]:
        print("  - " + p)
    print("=================================")
    if report["C"][0] == 0:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print("ERRORE: " + str(e), flush=True)
        sys.exit(1)
