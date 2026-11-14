#!/usr/bin/env python3
"""Prepara i tasselli batimetrici a tre livelli (base, medio, fine), a lotti.

Versione 8: FORCE_FINE=true scarica i tasselli fini in tutta l'area scelta (per piccoli pezzi mancanti, es. un porto).
Versione 7: se ERDDAP rifiuta un tassello, lo script aspetta (1 e 2 minuti) e riprova prima di darlo per fallito.
Versione 6: la costa si chiede prima dove c'e' terra nei tasselli fini; se Overpass non risponde fa pause e riprova.
Versione 5: la costa si chiede a Overpass in 4 richieste piu' leggere per quadrato (timeout 90 s, un tentativo per server).
Versione 4: l'AREA si sceglie a ogni lancio (variabile BBOX = "lat_min,lat_max,lon_min,lon_max").
I lotti si sommano: i file gia' presenti non vengono scaricati di nuovo.
Versione 3: RIPRENDE da dove si era fermata.
- I tasselli gia' presenti in tiles-data/ non vengono scaricati di nuovo.
- Ha un tempo massimo (MAX_MINUTES, predefinito 300): allo scadere si ferma in modo pulito,
  scrive tiles.json con quello che ha e un nuovo lancio continua dal punto raggiunto.
- Se Overpass non risponde per molti tasselli di fila, salta la parte costa e lo dice.
- tiles.json elenca i file REALMENTE presenti su disco (mai file mancanti).

Livelli (tutti con 240 celle per lato + 1 di sovrapposizione):
  C = 1 cella ogni 16 della griglia EMODnet (~1,7 km), tasselli di 4 gradi
  M = 1 cella ogni 4  (~420 m),                         tasselli di 1 grado
  F = tutte le celle  (~100 m),                         tasselli di 0,25 gradi
M si genera dove c'e' fondale tra 90 e 350 m; F dove c'e' fondale sotto i 125 m.
Scrive in tiles-data/: tiles.json, state.json, C|M|F/<ti>_<tj>.bin.gz, coast/<ti>_<tj>.json
Valori: quote in quarti di metro (int16 little-endian, nord in alto, -32768 = mancante).
Solo libreria standard di Python.
"""
import array, csv, gzip, io, json, math, os, sys, time, urllib.parse, urllib.request
from collections import deque
from datetime import datetime, timezone

def parse_bbox():
    raw = os.environ.get("BBOX", "").strip() or "39.0,41.3,9.5,15.8"   # predefinita: tratta Campania-Sardegna
    try:
        a, b, c, d = [float(v) for v in raw.replace(";", ",").split(",")]
    except ValueError:
        raise SystemExit("Area non valida: scrivere lat_min,lat_max,lon_min,lon_max (es. 39.0,41.3,9.5,15.8)")
    if not (a < b and c < d and 30 <= a and b <= 60 and 0 <= c and d <= 40):
        raise SystemExit("Area fuori dai limiti previsti (lat 30-60, lon 0-40, minimo < massimo): " + raw)
    return {"lat_min": a, "lat_max": b, "lon_min": c, "lon_max": d}


BBOX = parse_bbox()
OUT = "tiles-data"
SP = 960          # celle per grado
CELLS = 240
SCALE = 4         # quarti di metro
NODATA = -32768
STRIDE = {"C": 16, "M": 4, "F": 1}
MAX_FINE = int(os.environ.get("MAX_FINE", "3000"))   # limite di sicurezza sul numero di tasselli fini per lancio
MAX_MINUTES = float(os.environ.get("MAX_MINUTES", "300"))   # tempo massimo di lavoro
COAST_MAX_CONSECUTIVE_FAILS = 3   # dopo tanti fallimenti di fila si fa una pausa
COAST_RETRY_WAIT = 600            # secondi di pausa prima di riprovare (Overpass sovraccarico)
COAST_RETRY_ROUNDS = 3            # quante pause al massimo, poi la costa si interrompe
COAST_TILE_TRIES = 3              # tentativi per ogni quadrato di costa (i falliti si riprovano in coda)
COAST_SPLIT = 2   # ogni quadrato di 1 grado si chiede a Overpass in 2x2 richieste
PAUSE = 3         # secondi tra una richiesta e l'altra a ERDDAP
COAST_PAUSE = 4   # secondi tra una richiesta e l'altra a Overpass
BASE = "https://erddap.emodnet.eu/erddap/griddap/bathymetry_dtm_2024.csv"
UA = "mappa-isobate-prototipo/0.8 (progetto personale; GitHub Actions)"
OVERPASS = ["https://overpass-api.de/api/interpreter",
            "https://overpass.private.coffee/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.openstreetmap.fr/api/interpreter"]

START = time.time()


def out_of_time():
    return (time.time() - START) > MAX_MINUTES * 60


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
            if i < tries - 1:
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


FORCE_FINE = os.environ.get("FORCE_FINE", "").strip().lower() in ("1", "true", "si", "yes")   # tasselli fini in TUTTA l'area
FORCE_FINE_MAX = 400   # limite di sicurezza: oltre questo numero di tasselli fini forzati ci si ferma
PATIENT_ROUNDS = 3     # tentativi "pazienti" per ogni tassello (ERDDAP a volte rifiuta le connessioni per un po')
PATIENT_WAIT = 60      # secondi di attesa dopo il primo giro, raddoppiano al secondo


def fetch_tile_patient(level, ti, tj, L0, M0):
    last = None
    for r in range(PATIENT_ROUNDS):
        try:
            return fetch_tile(level, ti, tj, L0, M0)
        except Exception as e:  # noqa: BLE001
            last = e
            if r < PATIENT_ROUNDS - 1:
                w = PATIENT_WAIT * (r + 1)
                print(f"   {level} {ti}_{tj}: riprovo tra {w} s", flush=True)
                time.sleep(w)
    raise last


def tile_path(level, ti, tj):
    return os.path.join(OUT, level, f"{ti}_{tj}.bin.gz")


def write_tile(level, ti, tj, arr):
    d = os.path.join(OUT, level)
    os.makedirs(d, exist_ok=True)
    a = array.array("h", arr)
    if sys.byteorder == "big":
        a.byteswap()
    path = tile_path(level, ti, tj)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(gzip.compress(a.tobytes(), 9, mtime=0))
    os.replace(tmp, path)   # il file compare solo quando e' completo
    return os.path.getsize(path)


def read_tile(level, ti, tj):
    """Legge un tassello gia' salvato; None se manca o e' rovinato."""
    try:
        with gzip.open(tile_path(level, ti, tj), "rb") as f:
            a = array.array("h")
            a.frombytes(f.read())
        if sys.byteorder == "big":
            a.byteswap()
        return a if len(a) == (CELLS + 1) ** 2 else None
    except Exception:  # noqa: BLE001
        return None


# ---- costa e scogli ----
def overpass(q):
    """Un tentativo per server (timeout 90 s). Il server che risponde passa in testa per le richieste successive."""
    last = None
    for ep in list(OVERPASS):
        try:
            data = json.loads(get(ep, data=urllib.parse.urlencode({"data": q}).encode(), timeout=90, tries=1))
            rem = str(data.get("remark", "")).lower()
            if "error" in rem or "timed out" in rem:
                raise RuntimeError(rem)
            if OVERPASS[0] != ep:
                OVERPASS.remove(ep)
                OVERPASS.insert(0, ep)
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


def coast_path(ti, tj):
    return os.path.join(OUT, "coast", f"{ti}_{tj}.json")


def coast_tile(ti, tj, L0, M0):
    """Costa e scogli di un quadrato di 1 grado, chiesti a Overpass in COAST_SPLIT x COAST_SPLIT richieste piu' leggere.
    Il file si scrive solo se TUTTE le richieste riescono (una costa a meta' farebbe disegnare il mare sulla terra)."""
    st = CELLS * 4 / SP
    S0, W0 = L0 + ti * st, M0 + tj * st
    sub = st / COAST_SPLIT
    m = 0.01
    ways, pts = [], []
    for a in range(COAST_SPLIT):
        for c in range(COAST_SPLIT):
            s, w = S0 + a * sub, W0 + c * sub
            n, e = s + sub, w + sub
            bb = "%.5f,%.5f,%.5f,%.5f" % (s - m, w - m, n + m, e + m)
            q = ('[out:json][timeout:80];(way["natural"="coastline"](%s);'
                 'node["seamark:type"~"^(rock|wreck|obstruction)$"](%s);'
                 'node["natural"~"^(reef|shoal)$"](%s);way["natural"~"^(reef|shoal)$"](%s););out geom tags;' % (bb, bb, bb, bb))
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
                    if s <= la < n and w <= lo < e:  # ogni punto in un solo sotto-quadrato
                        pts.append({"lat": round(la, 6), "lon": round(lo, 6), "kind": t.get("seamark:type") or t.get("natural", ""),
                                    "name": t.get("name", ""), "level": t.get("seamark:rock:water_level", "")})
            time.sleep(COAST_PAUSE)
    if not ways and not pts:
        return 0
    d = os.path.join(OUT, "coast")
    os.makedirs(d, exist_ok=True)
    path = coast_path(ti, tj)
    with open(path + ".part", "w", encoding="utf-8") as f:
        json.dump({"ways": ways, "points": pts}, f, separators=(",", ":"))
    os.replace(path + ".part", path)
    return os.path.getsize(path)


# ---- stato (per riprendere) ----
def load_state():
    try:
        with open(os.path.join(OUT, "state.json"), encoding="utf-8") as f:
            st = json.load(f)
        st.setdefault("m_done", {})
        st.setdefault("coast_done", [])
        return st
    except Exception:  # noqa: BLE001
        return {"m_done": {}, "coast_done": []}


def save_state(st):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "state.json"), "w", encoding="utf-8") as f:
        json.dump(st, f, separators=(",", ":"))


def listing(sub, ext):
    d = os.path.join(OUT, sub)
    if not os.path.isdir(d):
        return []
    return sorted(n[:-len(ext)] for n in os.listdir(d) if n.endswith(ext))


def main():
    L0, M0 = anchors()
    print(f"Ancore griglia: lat {L0}, lon {M0}", flush=True)
    print(f"Tempo massimo: {MAX_MINUTES:.0f} minuti", flush=True)
    print("Area di questo lancio:", BBOX, flush=True)
    B = BBOX
    state = load_state()
    fails, notes = [], []
    stopped = False

    # --- C: base ---
    for ti in tile_range("C", B["lat_min"], B["lat_max"], L0):
        for tj in tile_range("C", B["lon_min"], B["lon_max"], M0):
            if os.path.exists(tile_path("C", ti, tj)):
                print(f"C {ti}_{tj} gia' presente", flush=True)
                continue
            if out_of_time():
                stopped = True
                continue
            try:
                write_tile("C", ti, tj, fetch_tile_patient("C", ti, tj, L0, M0))
                print(f"C {ti}_{tj} ok", flush=True)
            except Exception as e:  # noqa: BLE001
                fails.append(f"C {ti}_{tj}: {e}")
            time.sleep(PAUSE)

    # --- M: medio, e calcolo dei tasselli fini necessari ---
    fine_wanted = set()
    for ti in tile_range("M", B["lat_min"], B["lat_max"], L0):
        for tj in tile_range("M", B["lon_min"], B["lon_max"], M0):
            key = f"{ti}_{tj}"
            if key in state["m_done"]:
                for f in state["m_done"][key]:
                    a, b = f.split("_")
                    fine_wanted.add((int(a), int(b)))
                continue
            if out_of_time():
                stopped = True
                continue
            try:
                arr = fetch_tile_patient("M", ti, tj, L0, M0)
            except Exception as e:  # noqa: BLE001
                fails.append(f"M {ti}_{tj}: {e}")
                continue
            # tassello medio tenuto se contiene fondale tra 90 e 350 m
            if any(-350 * SCALE <= v <= -90 * SCALE for v in arr if v != NODATA):
                write_tile("M", ti, tj, arr)
            # tasselli fini necessari: 0,25 gradi = 60 celle medie
            mine = set()
            rows = [arr[r * 241:(r + 1) * 241] for r in range(241)][::-1]  # sud -> nord
            for a in range(4):
                for c in range(4):
                    for r in range(a * 60, a * 60 + 61):
                        if any(-125 * SCALE <= v < 0 for v in rows[r][c * 60:c * 60 + 61]):
                            mine.add((ti * 4 + a, tj * 4 + c))
                            break
            state["m_done"][key] = [f"{a}_{b}" for a, b in sorted(mine)]
            fine_wanted |= mine
            save_state(state)
            print(f"M {key} ok", flush=True)
            time.sleep(PAUSE)

    # --- F: fine ---
    if FORCE_FINE:
        forced = {(ti, tj) for ti in tile_range("F", B["lat_min"], B["lat_max"], L0)
                  for tj in tile_range("F", B["lon_min"], B["lon_max"], M0)}
        if len(forced) > FORCE_FINE_MAX:
            raise SystemExit(f"Con 'tasselli fini ovunque' l'area contiene {len(forced)} tasselli (massimo {FORCE_FINE_MAX}): usare un'area piu' piccola.")
        print(f"Tasselli fini forzati nell'area: {len(forced)}", flush=True)
        fine_wanted |= forced
    fine = sorted(fine_wanted)[:MAX_FINE]
    if len(fine_wanted) > MAX_FINE:
        notes.append(f"troppi tasselli fini ({len(fine_wanted)}): considerati solo {MAX_FINE}")
    todo = [(ti, tj) for ti, tj in fine if not os.path.exists(tile_path("F", ti, tj))]
    print(f"F: {len(fine)} richiesti, {len(fine) - len(todo)} gia' presenti, {len(todo)} da scaricare", flush=True)
    for n, (ti, tj) in enumerate(todo, 1):
        if out_of_time():
            stopped = True
            break
        try:
            write_tile("F", ti, tj, fetch_tile_patient("F", ti, tj, L0, M0))
        except Exception as e:  # noqa: BLE001
            fails.append(f"F {ti}_{tj}: {e}")
        if n % 10 == 0:
            print(f"F {n}/{len(todo)}", flush=True)
        time.sleep(PAUSE)

    # --- costa e scogli ---
    # Prima i quadrati di 1 grado dove i tasselli fini hanno celle di terra (la costa serve per ritagliare il mare),
    # poi quelli solo di mare (servono solo per scogli e relitti).
    land_m, sea_m = set(), set()
    for ti, tj in fine:
        arr = read_tile("F", ti, tj)
        if arr is None:
            continue
        (land_m if NODATA in arr else sea_m).add((ti // 4, tj // 4))
    sea_m -= land_m
    wanted = sorted(land_m) + sorted(sea_m)
    todo = [(ti, tj) for ti, tj in wanted
            if not os.path.exists(coast_path(ti, tj)) and f"{ti}_{tj}" not in state["coast_done"]]
    print(f"Costa: {len(wanted)} quadrati interessati ({len(land_m)} con terra), {len(todo)} da scaricare", flush=True)
    queue = deque((ti, tj, 0) for ti, tj in todo)
    consecutive, rounds_left = 0, COAST_RETRY_ROUNDS
    while queue:
        if out_of_time():
            stopped = True
            break
        ti, tj, att = queue.popleft()
        key = f"{ti}_{tj}"
        try:
            b = coast_tile(ti, tj, L0, M0)
            state["coast_done"].append(key)
            save_state(state)
            consecutive = 0
            print(f"costa {key} ok ({b} byte)", flush=True)
        except Exception as e:  # noqa: BLE001
            consecutive += 1
            if att + 1 < COAST_TILE_TRIES:
                queue.append((ti, tj, att + 1))      # si riprova in coda
            else:
                fails.append(f"costa {key}: {e}")
            if consecutive >= COAST_MAX_CONSECUTIVE_FAILS:
                if rounds_left > 0:
                    rounds_left -= 1
                    consecutive = 0
                    print(f"Overpass non risponde: pausa di {COAST_RETRY_WAIT // 60} minuti, poi riprovo", flush=True)
                    time.sleep(COAST_RETRY_WAIT)
                else:
                    notes.append(f"Overpass non risponde: costa interrotta con {len(queue) + 1} quadrati da fare, si riprende al prossimo lancio")
                    break
        time.sleep(COAST_PAUSE)

    if stopped:
        notes.append("tempo massimo raggiunto: lavoro parziale, rilanciare per continuare")

    levels = {lv: listing(lv, ".bin.gz") for lv in ("C", "M", "F")}
    coast = listing("coast", ".json")
    tot_bbox = dict(B)
    try:
        with open(os.path.join(OUT, "tiles.json"), encoding="utf-8") as f:
            old = json.load(f).get("bbox") or {}
        tot_bbox = {"lat_min": min(B["lat_min"], old.get("lat_min", B["lat_min"])),
                    "lat_max": max(B["lat_max"], old.get("lat_max", B["lat_max"])),
                    "lon_min": min(B["lon_min"], old.get("lon_min", B["lon_min"])),
                    "lon_max": max(B["lon_max"], old.get("lon_max", B["lon_max"]))}
    except Exception:  # noqa: BLE001
        pass
    meta = {"built_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "lat0": L0, "lon0": M0,
            "scale": SCALE, "levels": levels, "coast": coast, "bbox": tot_bbox, "failed": fails, "notes": notes}
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "tiles.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, separators=(",", ":"))
    save_state(state)

    print("\n=========== RIEPILOGO ===========")
    tot = 0
    for name, sub, ext in (("C", "C", ".bin.gz"), ("M", "M", ".bin.gz"), ("F", "F", ".bin.gz"), ("costa", "coast", ".json")):
        d = os.path.join(OUT, sub)
        names = listing(sub, ext)
        b = sum(os.path.getsize(os.path.join(d, n + ext)) for n in names)
        tot += b
        print(f"{name}: {len(names)} file, {b / 1024:.0f} KB")
    print(f"Totale: {tot / 1048576:.2f} MB. Tempo: {round(time.time() - START)} s. Problemi: {len(fails)}")
    for p in notes:
        print("  * " + p)
    for p in fails[:30]:
        print("  - " + p)
    print("=================================")
    if not levels["C"]:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print("ERRORE: " + str(e), flush=True)
        sys.exit(1)
