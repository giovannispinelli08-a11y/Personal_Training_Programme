#!/usr/bin/env python3
"""
Calcola le metriche derivate che la web app mostra e scrive data/metrics.json.

  - Prontezza del giorno (0-100): HRV, FC a riposo e sonno confrontati con la
    baseline personale, più il rapporto fatica/forma. Con un consiglio su come
    adattare la seduta prevista.
  - Serie giornaliere: HRV con banda di normalità, FC a riposo, sonno,
    forma (CTL), fatica (ATL), freschezza (TSB).
  - Per settimana: tempo nelle zone FC (dagli stream), monotonia e strain.
  - Per corsa: efficienza, deriva cardiaca (decoupling), cadenza, zone.
  - Prestazione: ritmo a FC fissa (indicatore aerobico), VO2max e stima
    teorica della mezza.

Legge:  data/wellness.csv, data/summary.csv, data/activities/*.csv,
        plan/config.yml, plan/sessions.csv
Scrive: data/metrics.json

Nessun dato inventato: dove mancano i numeri il campo è null.
"""

import csv
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WELLNESS = DATA / "wellness.csv"
SUMMARY = DATA / "summary.csv"
STREAMS = DATA / "activities"
CONFIG = ROOT / "plan" / "config.yml"
PLAN = ROOT / "plan" / "sessions.csv"
OUT = DATA / "metrics.json"

OGGI = date.today()

# Finestre per le baseline personali.
BASE_HRV_GIORNI = 60      # baseline HRV: ultimi 60 giorni
BASE_RHR_GIORNI = 30      # baseline FC a riposo: ultimi 30 giorni
MIN_CAMPIONI = 10         # sotto questa soglia la baseline non è affidabile
SERIE_GIORNI = 120        # quanti giorni esportare per i grafici
FC_RIFERIMENTO = 145      # FC a cui si misura il ritmo (metà Z2 con FCmax 195)
SONNO_OBIETTIVO_H = 8.0


def leggi_csv(p):
    if not p.exists():
        return []
    with p.open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def media(a):
    return sum(a) / len(a) if a else None


def devstd(a):
    if len(a) < 2:
        return None
    m = media(a)
    return math.sqrt(sum((x - m) ** 2 for x in a) / (len(a) - 1))


def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def r1(x, n=1):
    return None if x is None else round(x, n)


def lunedi_di(d):
    return d - timedelta(days=d.weekday())


# ------------------------------------------------------------------
# Wellness e prontezza
# ------------------------------------------------------------------

def carica_wellness():
    giorni = {}
    for r in leggi_csv(WELLNESS):
        try:
            d = date.fromisoformat(r["date"])
        except (KeyError, ValueError):
            continue
        sonno_s = num(r.get("sleepSecs"))
        giorni[d] = {
            "hrv": num(r.get("hrv")),
            "rhr": num(r.get("restingHR")),
            "sonno_h": sonno_s / 3600 if sonno_s else None,
            "sonno_score": num(r.get("sleepScore")),
            "vo2max": num(r.get("vo2max")),
            "peso": num(r.get("weight")),
            "passi": num(r.get("steps")),
            "ctl": num(r.get("ctl")),
            "atl": num(r.get("atl")),
            "load": num(r.get("ctlLoad")),
        }
    return giorni


def finestra(giorni, d, campo, n, includi_oggi=False):
    """Valori non nulli di `campo` negli n giorni prima di d."""
    out = []
    inizio = 0 if includi_oggi else 1
    for k in range(inizio, n + inizio):
        v = giorni.get(d - timedelta(days=k), {}).get(campo)
        if v is not None:
            out.append(v)
    return out


def punteggio_carico(ctl, atl):
    """Rapporto fatica/forma -> 0-100. Con una base piccola il rapporto
    oscilla molto, quindi sotto CTL 5 la componente non si usa."""
    if ctl is None or atl is None or ctl < 5:
        return None, None
    rapporto = atl / ctl
    if rapporto <= 1.0:
        s = 85
    elif rapporto <= 1.3:
        s = 85 - (rapporto - 1.0) / 0.3 * 25      # 85 -> 60
    elif rapporto <= 1.8:
        s = 60 - (rapporto - 1.3) / 0.5 * 45      # 60 -> 15
    else:
        s = 10
    return rapporto, s


def prontezza(giorni, d):
    """Punteggio 0-100 e dettaglio delle componenti per il giorno d."""
    g = giorni.get(d, {})
    comp = {}

    # HRV: si lavora sul logaritmo (distribuzione più simmetrica) e si
    # confronta con media e deviazione della baseline.
    base = [math.log(v) for v in finestra(giorni, d, "hrv", BASE_HRV_GIORNI) if v > 0]
    if g.get("hrv") and len(base) >= MIN_CAMPIONI:
        m, s = media(base), devstd(base) or 0.1
        z = (math.log(g["hrv"]) - m) / s
        comp["hrv"] = {
            "valore": r1(g["hrv"], 0),
            "baseline": r1(math.exp(m), 0),
            "z": r1(z, 2),
            "punti": r1(clamp(70 + 15 * z), 0),
            "peso": 0.35,
        }

    # FC a riposo: più alta del solito = peggio.
    base = finestra(giorni, d, "rhr", BASE_RHR_GIORNI)
    if g.get("rhr") and len(base) >= MIN_CAMPIONI:
        m, s = media(base), max(devstd(base) or 1.0, 1.0)
        z = (g["rhr"] - m) / s
        comp["rhr"] = {
            "valore": r1(g["rhr"], 0),
            "baseline": r1(m, 1),
            "delta": r1(g["rhr"] - m, 1),
            "z": r1(z, 2),
            "punti": r1(clamp(70 - 15 * z), 0),
            "peso": 0.20,
        }

    # Sonno: metà durata rispetto a 8 ore, metà punteggio Garmin.
    if g.get("sonno_h") is not None:
        p_durata = clamp(g["sonno_h"] / SONNO_OBIETTIVO_H * 100)
        p = p_durata if g.get("sonno_score") is None else (p_durata + g["sonno_score"]) / 2
        comp["sonno"] = {
            "ore": r1(g["sonno_h"], 1),
            "score": r1(g.get("sonno_score"), 0),
            "punti": r1(p, 0),
            "peso": 0.25,
        }

    rapporto, p = punteggio_carico(g.get("ctl"), g.get("atl"))
    if p is not None:
        comp["carico"] = {
            "rapporto": r1(rapporto, 2),
            "tsb": r1(g["ctl"] - g["atl"], 1),
            "punti": r1(p, 0),
            "peso": 0.20,
        }

    if not comp or ("hrv" not in comp and "rhr" not in comp and "sonno" not in comp):
        return None, comp

    peso_tot = sum(c["peso"] for c in comp.values())
    score = sum(c["punti"] * c["peso"] for c in comp.values()) / peso_tot
    return round(score), comp


def stato_hrv_settimana(giorni, d):
    """Metodo della media mobile a 7 giorni (Plews): la media della settimana
    di ln(HRV) confrontata con la baseline ± 0,5 deviazioni standard."""
    ultimi = [math.log(v) for v in finestra(giorni, d, "hrv", 7, includi_oggi=True) if v > 0]
    base = [math.log(v) for v in finestra(giorni, d - timedelta(days=7), "hrv", BASE_HRV_GIORNI) if v > 0]
    if len(ultimi) < 4 or len(base) < MIN_CAMPIONI:
        return None
    m7 = media(ultimi)
    m, s = media(base), devstd(base) or 0.1
    lo, hi = m - 0.5 * s, m + 0.5 * s
    stato = "sotto" if m7 < lo else ("sopra" if m7 > hi else "normale")
    return {
        "media7": round(math.exp(m7)),
        "banda": [round(math.exp(lo)), round(math.exp(hi))],
        "stato": stato,
    }


def livello(score):
    if score is None:
        return None
    if score >= 65:
        return "verde"
    if score >= 45:
        return "giallo"
    return "rosso"


QUALITA = {"tempo", "ripetute", "ritmo_gara", "gara"}


def consiglio(score, comp, hrv7, seduta):
    """Traduce il punteggio in un'indicazione pratica sulla seduta di oggi."""
    liv = livello(score)
    tipo = seduta["tipo"] if seduta else None
    minuti = num(seduta.get("minuti")) if seduta else None
    note = []

    if comp.get("rhr") and comp["rhr"]["delta"] is not None and comp["rhr"]["delta"] >= 5:
        note.append(f"FC a riposo {comp['rhr']['delta']:+.0f} bpm sopra la media: "
                    "possibile inizio di malanno, disidratazione o fatica.")
    if comp.get("sonno") and comp["sonno"]["ore"] is not None and comp["sonno"]["ore"] < 6:
        note.append(f"Solo {comp['sonno']['ore']:.1f} ore di sonno.")
    if hrv7 and hrv7["stato"] == "sotto":
        note.append("HRV media della settimana sotto la tua banda normale: "
                    "il recupero sta faticando da qualche giorno.")

    if liv is None:
        testo = "Dati di recupero insufficienti: segui il piano e le sensazioni."
    elif not seduta:
        testo = {
            "verde": "Giorno di riposo da piano. Recupero buono: una camminata o mobilità vanno benissimo.",
            "giallo": "Giorno di riposo da piano: sfruttalo davvero.",
            "rosso": "Giorno di riposo da piano, e ne hai bisogno: sonno e alimentazione al centro.",
        }[liv]
    elif liv == "verde":
        testo = "Via libera: seduta come da piano."
    elif liv == "giallo":
        if tipo in QUALITA and tipo != "gara":
            testo = "Recupero parziale: sostituisci la qualità con una corsa facile in Z2, sposta il lavoro di un giorno o due."
        elif tipo == "gara":
            testo = "Recupero parziale: parti prudente e decidi dopo i primi km."
        elif minuti:
            testo = f"Recupero parziale: seduta facile ma accorciata, circa {round(minuti * 0.75)}′, FC nella parte bassa della Z2."
        else:
            testo = "Recupero parziale: seduta facile, FC nella parte bassa della Z2."
    else:
        if tipo == "gara":
            testo = "Recupero scarso: in gara corri a sensazione, senza obiettivi di tempo."
        else:
            testo = "Recupero scarso: riposo oppure 20–30′ molto facili in Z1. La seduta non va recuperata."

    return liv, testo, note


# ------------------------------------------------------------------
# Stream delle corse
# ------------------------------------------------------------------

def minetti(grade):
    """Costo energetico della corsa in salita/discesa (J/kg/m), Minetti 2002.
    Diviso per il costo in piano dà il fattore per la velocità equivalente."""
    i = max(-0.3, min(0.3, grade))
    return (155.4 * i ** 5 - 30.4 * i ** 4 - 43.3 * i ** 3
            + 46.3 * i ** 2 + 19.5 * i + 3.6)


def analizza_stream(path, zone_bpm):
    """Zone, deriva cardiaca e cadenza di una corsa."""
    righe = leggi_csv(path)
    punti = []
    for r in righe:
        t = num(r.get("time"))
        hr = num(r.get("heartrate"))
        v = num(r.get("velocity_smooth"))
        g = num(r.get("grade_smooth"))
        c = num(r.get("cadence"))
        if t is None:
            continue
        punti.append((t, hr, v, (g or 0) / 100, c))
    if len(punti) < 20:
        return None

    zone = [0.0] * len(zone_bpm)
    campioni = []   # (dt, hr, velocità equivalente in piano)
    cad = []
    for (t0, hr, v, g, c), (t1, *_rest) in zip(punti, punti[1:]):
        dt = t1 - t0
        if dt <= 0 or dt > 30:   # pause o buchi di registrazione
            continue
        if hr:
            for k, (lo, hi) in enumerate(zone_bpm):
                if lo <= hr < hi or (k == 0 and hr < lo) or (k == len(zone_bpm) - 1 and hr >= hi):
                    zone[k] += dt
                    break
        if hr and v and v > 1.0:   # sotto 1 m/s è camminata o sosta
            campioni.append((dt, hr, v * minetti(g) / 3.6))
        if c and v and v > 1.5:
            cad.append((dt, c))

    # Deriva cardiaca (Pa:Hr): efficienza della prima metà contro la seconda.
    # Si scartano i primi 10 minuti, in cui la FC sta ancora salendo: su
    # corse più corte di 40' il numero misurerebbe solo il riscaldamento.
    decoupling = None
    t_tot = sum(dt for dt, *_ in campioni)
    if t_tot >= 40 * 60:
        acc, validi = 0.0, []
        for s in campioni:
            acc += s[0]
            if acc > 600:
                validi.append(s)
        met = sum(s[0] for s in validi) / 2
        a, b, acc = [], [], 0.0
        for s in validi:
            (a if acc < met else b).append(s)
            acc += s[0]

        def ef(seg):
            tt = sum(s[0] for s in seg)
            return (sum(s[2] * s[0] for s in seg) / tt) / (sum(s[1] * s[0] for s in seg) / tt)

        if a and b:
            decoupling = (ef(a) - ef(b)) / ef(a) * 100

    cadenza = None
    if cad:
        tt = sum(dt for dt, _ in cad)
        c = sum(c * dt for dt, c in cad) / tt
        cadenza = c * 2 if c < 120 else c   # Garmin registra i passi di un piede

    return {
        "zone_s": [round(z) for z in zone],
        "decoupling": decoupling,
        "cadenza": cadenza,
    }


def carica_corse(zone_bpm):
    corse = []
    for r in leggi_csv(SUMMARY):
        if r.get("type") != "Run":
            continue
        dist = num(r.get("distance_m")) or 0
        tempo = num(r.get("moving_time_s")) or 0
        if dist < 1000 or tempo < 600:
            continue
        try:
            d = datetime.fromisoformat(r["start_local"]).date()
        except (KeyError, ValueError):
            continue
        gap = num(r.get("grade_adjusted_pace"))
        fc = num(r.get("avg_hr"))
        c = {
            "id": r["id"],
            "data": d,
            "km": dist / 1000,
            "min": tempo / 60,
            "pace_s": tempo / (dist / 1000),
            "gap": gap,
            "fc": fc,
            "ef": gap / fc * 1000 if gap and fc else None,
            "load": num(r.get("training_load")) or 0,
            "zone_s": None,
            "decoupling": None,
            "cadenza": None,
        }
        stream = STREAMS / f"{d.isoformat()}_{r['id']}.csv"
        if stream.exists():
            s = analizza_stream(stream, zone_bpm)
            if s:
                c.update(s)
        corse.append(c)
    corse.sort(key=lambda c: c["data"])
    return corse


def ritmo_a_fc(corse, fc_rif, fine, giorni=42):
    """Regressione velocità ~ FC sulle corse delle ultime `giorni` settimane,
    valutata alla FC di riferimento. Indica il ritmo 'a parità di cuore':
    se scende, la base aerobica migliora."""
    dati = [(c["fc"], c["gap"]) for c in corse
            if c["fc"] and c["gap"] and 0 <= (fine - c["data"]).days <= giorni]
    if len(dati) < 4:
        return None
    xs = [d[0] for d in dati]
    ys = [d[1] for d in dati]
    mx, my = media(xs), media(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx < 25:            # FC troppo simili tra loro: pendenza non stimabile
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    if b <= 0:
        return None
    # Estrapolare lontano dai dati non ha senso.
    if not (min(xs) - 8 <= fc_rif <= max(xs) + 8):
        return None
    v = my + b * (fc_rif - mx)
    return 1000 / v if v > 0 else None


# ------------------------------------------------------------------
# Prestazione
# ------------------------------------------------------------------

def tempo_da_vdot(vdot, metri):
    """Tempo previsto (s) per la distanza, formule di Daniels-Gilbert."""
    def vdot_di(t_min):
        v = metri / t_min
        vo2 = -4.60 + 0.182258 * v + 0.000104 * v * v
        pct = 0.8 + 0.1894393 * math.exp(-0.012778 * t_min) + 0.2989558 * math.exp(-0.1932605 * t_min)
        return vo2 / pct

    lo, hi = 30.0, 300.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if vdot_di(mid) > vdot:   # troppo veloce per questo VDOT
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2 * 60


def hms(sec):
    if sec is None:
        return None
    sec = round(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# ------------------------------------------------------------------

def main():
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    fcmax = cfg["atleta"]["fc_max"]
    zone_nomi = list(cfg["zone_fc"].keys())
    zone_bpm = [(lo * fcmax, hi * fcmax) for lo, hi in cfg["zone_fc"].values()]
    distanza_gara = cfg["gara_obiettivo"]["distanza_km"] * 1000

    giorni = carica_wellness()
    corse = carica_corse(zone_bpm)
    piano = leggi_csv(PLAN)
    piano_per_data = {}
    for r in piano:
        piano_per_data.setdefault(r.get("data"), r)

    # ---------- serie giornaliera ----------
    serie = []
    inizio = OGGI - timedelta(days=SERIE_GIORNI - 1)
    d = inizio
    while d <= OGGI:
        g = giorni.get(d, {})
        score, comp = prontezza(giorni, d)
        base = [math.log(v) for v in finestra(giorni, d, "hrv", BASE_HRV_GIORNI) if v > 0]
        banda = None
        if len(base) >= MIN_CAMPIONI:
            m, s = media(base), devstd(base) or 0.1
            banda = [round(math.exp(m - 0.5 * s)), round(math.exp(m + 0.5 * s))]
        ult7 = finestra(giorni, d, "hrv", 7, includi_oggi=True)
        rhr_base = finestra(giorni, d, "rhr", BASE_RHR_GIORNI)
        ctl, atl = g.get("ctl"), g.get("atl")
        serie.append({
            "data": d.isoformat(),
            "hrv": r1(g.get("hrv"), 0),
            "hrv7": round(math.exp(media([math.log(v) for v in ult7]))) if len(ult7) >= 3 else None,
            "hrv_banda": banda,
            "rhr": r1(g.get("rhr"), 0),
            "rhr_base": r1(media(rhr_base), 1) if len(rhr_base) >= MIN_CAMPIONI else None,
            "sonno_h": r1(g.get("sonno_h"), 2),
            "sonno_score": r1(g.get("sonno_score"), 0),
            "prontezza": score,
            "ctl": r1(ctl, 1),
            "atl": r1(atl, 1),
            "tsb": r1(ctl - atl, 1) if ctl is not None and atl is not None else None,
            "load": r1(g.get("load"), 0),
        })
        d += timedelta(days=1)

    # ---------- oggi ----------
    score, comp = prontezza(giorni, OGGI)
    hrv7 = stato_hrv_settimana(giorni, OGGI)
    seduta = piano_per_data.get(OGGI.isoformat())
    liv, testo, note = consiglio(score, comp, hrv7, seduta)
    ieri = next((s["prontezza"] for s in reversed(serie[:-1]) if s["prontezza"] is not None), None)
    oggi = {
        "data": OGGI.isoformat(),
        "prontezza": score,
        "livello": liv,
        "ieri": ieri,
        "componenti": comp,
        "hrv_settimana": hrv7,
        "seduta": {k: seduta[k] for k in ("tipo", "minuti", "fc_min", "fc_max", "note")} if seduta else None,
        "consiglio": testo,
        "note": note,
    }

    # ---------- settimane: zone, monotonia, strain ----------
    settimane = []
    lun_ora = lunedi_di(OGGI)
    for k in range(15, -1, -1):
        lun = lun_ora - timedelta(weeks=k)
        dom = lun + timedelta(days=6)
        cs = [c for c in corse if lun <= c["data"] <= dom]
        zone = [0] * len(zone_nomi)
        for c in cs:
            if c["zone_s"]:
                zone = [a + b for a, b in zip(zone, c["zone_s"])]
        carichi = [giorni.get(lun + timedelta(days=i), {}).get("load") or 0 for i in range(7)
                   if lun + timedelta(days=i) <= OGGI]
        tot = sum(carichi)
        monotonia = strain = None
        if len(carichi) == 7 and tot > 0:
            s = devstd(carichi)
            if s:
                monotonia = media(carichi) / s
                strain = tot * monotonia
        settimane.append({
            "lun": lun.isoformat(),
            "corse": len(cs),
            "min_corsa": round(sum(c["min"] for c in cs)),
            "km": r1(sum(c["km"] for c in cs), 1),
            "zone_min": [round(z / 60) for z in zone],
            "load_tot": round(tot),
            "monotonia": r1(monotonia, 2),
            "strain": round(strain) if strain else None,
        })

    # ---------- corse ----------
    corse_out = []
    for c in corse[-40:]:
        corse_out.append({
            "id": c["id"],
            "data": c["data"].isoformat(),
            "km": r1(c["km"], 2),
            "min": r1(c["min"], 1),
            "pace_s": round(c["pace_s"]),
            "fc": r1(c["fc"], 0),
            "ef": r1(c["ef"], 2),
            "decoupling": r1(c["decoupling"], 1),
            "cadenza": r1(c["cadenza"], 0),
            "zone_min": [round(z / 60, 1) for z in c["zone_s"]] if c["zone_s"] else None,
        })

    # ---------- prestazione ----------
    storia_ritmo = []
    for k in range(12, -1, -1):
        fine = lun_ora - timedelta(weeks=k) + timedelta(days=6)
        fine = min(fine, OGGI)
        p = ritmo_a_fc(corse, FC_RIFERIMENTO, fine)
        storia_ritmo.append({"data": fine.isoformat(), "pace_s": round(p) if p else None})
    ritmo_ora = next((s["pace_s"] for s in reversed(storia_ritmo) if s["pace_s"]), None)

    vo2 = [(d, g["vo2max"]) for d, g in sorted(giorni.items()) if g.get("vo2max")]
    vo2_ultimo = vo2[-1][1] if vo2 else None
    vo2_storia = [{"data": d.isoformat(), "vo2max": v} for d, v in vo2 if (OGGI - d).days <= 365]
    mezza_s = tempo_da_vdot(vo2_ultimo, distanza_gara) if vo2_ultimo else None

    # Polarizzazione delle ultime 4 settimane: quota di tempo sotto la soglia (Z1+Z2).
    z4 = [0] * len(zone_nomi)
    for s in settimane[-4:]:
        z4 = [a + b for a, b in zip(z4, s["zone_min"])]
    tot4 = sum(z4)
    polar = round((z4[0] + z4[1]) / tot4 * 100) if tot4 else None

    dec_recenti = [c["decoupling"] for c in corse if c["decoupling"] is not None
                   and (OGGI - c["data"]).days <= 28]
    cad_recenti = [c["cadenza"] for c in corse if c["cadenza"] and (OGGI - c["data"]).days <= 28]

    prestazione = {
        "fc_riferimento": FC_RIFERIMENTO,
        "ritmo_a_fc_s": ritmo_ora,
        "ritmo_a_fc_storia": storia_ritmo,
        "vo2max": vo2_ultimo,
        "vo2max_storia": vo2_storia,
        "mezza_teorica": hms(mezza_s),
        "mezza_teorica_s": round(mezza_s) if mezza_s else None,
        "polarizzazione_28": polar,
        "decoupling_medio_28": r1(media(dec_recenti), 1),
        "cadenza_media_28": r1(media(cad_recenti), 0),
    }

    out = {
        "generato": OGGI.isoformat(),
        "zone": zone_nomi,
        "zone_bpm": [[round(lo), round(hi)] for lo, hi in zone_bpm],
        "oggi": oggi,
        "giornaliero": serie,
        "settimane": settimane,
        "corse": corse_out,
        "prestazione": prestazione,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Scritto {OUT.relative_to(ROOT)} — prontezza oggi: {score} ({liv}).")


if __name__ == "__main__":
    main()
