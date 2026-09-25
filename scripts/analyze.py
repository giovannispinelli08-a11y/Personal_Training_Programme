#!/usr/bin/env python3
"""
Confronta il piano con quello che è successo davvero e scrive
data/digest.md — un riassunto compatto, pensato per essere letto
al posto dei CSV grezzi.

Legge:  data/summary.csv, plan/sessions.csv, data/wellness.csv
Scrive: data/digest.md
"""

import csv
import json
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SUMMARY = DATA / "summary.csv"
WELLNESS = DATA / "wellness.csv"
PLAN = ROOT / "plan" / "sessions.csv"
METRICS = DATA / "metrics.json"
OUT = DATA / "digest.md"

OGGI = date.today()


def leggi_csv(p):
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def mmss(sec):
    if not sec or sec <= 0:
        return "—"
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def lunedi_di(d):
    return d - timedelta(days=d.weekday())


def carica_corse():
    corse = []
    for r in leggi_csv(SUMMARY):
        if r.get("type") != "Run":
            continue
        dist = f(r.get("distance_m"), 0)
        tempo = f(r.get("moving_time_s"), 0)
        if dist < 1000 or tempo < 600:
            continue  # scarta tracce spurie e attività troppo brevi
        gap = f(r.get("grade_adjusted_pace"))  # m/s corretti per pendenza
        corse.append({
            "data": datetime.fromisoformat(r["start_local"]).date(),
            "km": dist / 1000,
            "min": tempo / 60,
            "pace_s": tempo / (dist / 1000),
            "gap_ms": gap,
            "fc": f(r.get("avg_hr")),
            "fc_max": f(r.get("max_hr")),
            "disl": f(r.get("elev_gain_m"), 0),
            "load": f(r.get("training_load"), 0),
            "ctl": f(r.get("ctl_fitness")),
            "atl": f(r.get("atl_fatica")),
        })
    corse.sort(key=lambda c: c["data"])
    return corse


def carica_altro_sport():
    altre = []
    for r in leggi_csv(SUMMARY):
        if r.get("type") == "Run":
            continue
        try:
            d = datetime.fromisoformat(r["start_local"]).date()
        except (ValueError, KeyError):
            continue
        altre.append({
            "data": d,
            "tipo": r.get("type", "?"),
            "min": f(r.get("moving_time_s"), 0) / 60,
            "load": f(r.get("training_load"), 0),
        })
    return altre


def settimana_piano(piano, lunedi):
    """Sedute pianificate nella settimana che inizia il lunedi dato."""
    fine = lunedi + timedelta(days=7)
    out = []
    for r in piano:
        try:
            d = date.fromisoformat(r["data"])
        except ValueError:
            continue
        if lunedi <= d < fine:
            out.append(r)
    return out


def di_corsa(r):
    """Le sedute di supporto (mobilità, potenziamento) non sono volume di corsa."""
    return (r.get("disciplina") or "corsa") == "corsa"


def main():
    corse = carica_corse()
    altre = carica_altro_sport()
    piano = leggi_csv(PLAN)

    righe = []
    A = righe.append

    A(f"# Digest — {OGGI.isoformat()}")
    A("")

    if not corse:
        A("Nessuna corsa nei dati.")
        OUT.write_text("\n".join(righe), encoding="utf-8")
        return

    # ---------------- stato attuale ----------------
    ultime28 = [c for c in corse if (OGGI - c["data"]).days <= 28]
    ultime7 = [c for c in corse if (OGGI - c["data"]).days <= 7]
    ultima = corse[-1]

    A("## Stato")
    A("")
    A(f"- Ultima corsa: {ultima['data']} — {ultima['km']:.1f} km in {ultima['min']:.0f}' "
      f"({mmss(ultima['pace_s'])}/km, FC {ultima['fc']:.0f})" if ultima["fc"] else
      f"- Ultima corsa: {ultima['data']} — {ultima['km']:.1f} km")
    A(f"- Giorni dall'ultima corsa: {(OGGI - ultima['data']).days}")
    A(f"- Ultimi 7 giorni: {len(ultime7)} corse, {sum(c['km'] for c in ultime7):.1f} km, "
      f"{sum(c['min'] for c in ultime7):.0f} min")
    A(f"- Ultimi 28 giorni: {len(ultime28)} corse, {sum(c['km'] for c in ultime28):.1f} km, "
      f"{sum(c['min'] for c in ultime28):.0f} min")
    if ultime28:
        A(f"- Lungo più lungo (28gg): {max(c['km'] for c in ultime28):.1f} km")
    if ultima.get("ctl") is not None and ultima.get("atl") is not None:
        rapporto = ultima["atl"] / ultima["ctl"] if ultima["ctl"] else 0
        A(f"- Carico: CTL {ultima['ctl']:.1f} / ATL {ultima['atl']:.1f} (rapporto {rapporto:.2f})")
    A("")

    # ---------------- recupero ----------------
    metriche = json.loads(METRICS.read_text(encoding="utf-8")) if METRICS.exists() else {}
    oggi = metriche.get("oggi") or {}
    if oggi.get("prontezza") is not None:
        comp = oggi.get("componenti", {})
        A("## Recupero")
        A("")
        A(f"- Prontezza oggi: {oggi['prontezza']}/100 ({oggi['livello']})")
        if "hrv" in comp:
            A(f"- HRV: {comp['hrv']['valore']:.0f} ms (baseline {comp['hrv']['baseline']:.0f})")
        h7 = oggi.get("hrv_settimana")
        if h7:
            A(f"- HRV media 7 giorni: {h7['media7']} ms, banda normale {h7['banda'][0]}-{h7['banda'][1]} ({h7['stato']})")
        if "rhr" in comp:
            A(f"- FC a riposo: {comp['rhr']['valore']:.0f} bpm ({comp['rhr']['delta']:+.1f} sulla media)")
        if "sonno" in comp:
            A(f"- Sonno: {comp['sonno']['ore']:.1f} h")
        A(f"- Consiglio: {oggi['consiglio']}")
        A("")

    # ---------------- piano vs reale ----------------
    A("## Piano vs reale (ultime 4 settimane)")
    A("")
    A("| Settimana | Piano min | Reale min | % | Lungo piano | Lungo reale |")
    A("|---|---|---|---|---|---|")
    for k in range(3, -1, -1):
        lun = lunedi_di(OGGI) - timedelta(weeks=k)
        p = settimana_piano(piano, lun)
        p_min = sum(f(r["minuti"], 0) for r in p if di_corsa(r))
        p_lungo = max([f(r["minuti"], 0) for r in p if r["tipo"] in ("lungo", "ritmo_gara")] or [0])
        reali = [c for c in corse if lun <= c["data"] < lun + timedelta(days=7)]
        r_min = sum(c["min"] for c in reali)
        r_lungo = max([c["min"] for c in reali] or [0])
        pct = f"{r_min / p_min * 100:.0f}%" if p_min else "—"
        A(f"| {lun.isoformat()} | {p_min:.0f} | {r_min:.0f} | {pct} | "
          f"{p_lungo:.0f} | {r_lungo:.0f} |")
    A("")

    # ---------------- prossima settimana ----------------
    prossimo_lun = lunedi_di(OGGI) + timedelta(weeks=1)
    prossime = settimana_piano(piano, prossimo_lun)
    if prossime:
        A(f"## In programma (settimana dal {prossimo_lun.isoformat()})")
        A("")
        for r in prossime:
            fc = f" — FC {r['fc_min']}-{r['fc_max']}" if r["fc_min"] else ""
            durata = f"{r['minuti']}'" if r["minuti"] else ""
            A(f"- {r['data']} · {r['tipo']} {durata}{fc}")
        A("")

    # ---------------- efficienza ----------------
    A("## Efficienza aerobica")
    A("")
    A("Velocità corretta per pendenza divisa per battito (×1000). "
      "Sale = stesso costo cardiaco a velocità maggiore.")
    A("")
    validi = [c for c in corse if c["fc"] and c["gap_ms"]]
    recenti = validi[-10:]
    for c in recenti:
        ef = c["gap_ms"] / c["fc"] * 1000
        A(f"- {c['data']} · {c['km']:.1f} km · {mmss(c['pace_s'])}/km · "
          f"FC {c['fc']:.0f} · EF {ef:.1f}")
    if len(validi) >= 6:
        primi = validi[-6:-3]
        ultimi = validi[-3:]
        ef_p = sum(c["gap_ms"] / c["fc"] for c in primi) / len(primi) * 1000
        ef_u = sum(c["gap_ms"] / c["fc"] for c in ultimi) / len(ultimi) * 1000
        delta = (ef_u - ef_p) / ef_p * 100
        A("")
        A(f"Trend ultime 3 vs 3 precedenti: {delta:+.1f}%")
    A("")

    # ---------------- altri sport ----------------
    altre28 = [a for a in altre if (OGGI - a["data"]).days <= 28]
    if altre28:
        A("## Altro carico (28 giorni)")
        A("")
        per_tipo = {}
        for a in altre28:
            per_tipo.setdefault(a["tipo"], [0, 0])
            per_tipo[a["tipo"]][0] += a["min"]
            per_tipo[a["tipo"]][1] += a["load"]
        for t, (m, l) in sorted(per_tipo.items()):
            A(f"- {t}: {m:.0f} min, carico {l:.0f}")
        A("")

    # ---------------- segnalazioni ----------------
    A("## Segnalazioni")
    A("")
    segn = []

    giorni_stop = (OGGI - ultima["data"]).days
    if giorni_stop >= 10:
        segn.append(f"Ferm* da {giorni_stop} giorni: al rientro ripartire dal volume di due settimane fa, non da dove si era arrivati.")

    # salto di volume settimanale
    lun_corr = lunedi_di(OGGI)
    for k in (1, 2):
        a = [c for c in corse if lun_corr - timedelta(weeks=k) <= c["data"] < lun_corr - timedelta(weeks=k - 1)]
        b = [c for c in corse if lun_corr - timedelta(weeks=k + 1) <= c["data"] < lun_corr - timedelta(weeks=k)]
        ma, mb = sum(c["min"] for c in a), sum(c["min"] for c in b)
        if mb > 30 and ma > mb * 1.25:
            segn.append(f"Salto di volume: settimana del {(lun_corr - timedelta(weeks=k)).isoformat()} "
                        f"{ma:.0f} min contro {mb:.0f} della precedente (+{(ma / mb - 1) * 100:.0f}%).")

    # lungo che cresce troppo in fretta
    if len(ultime28) >= 2:
        lunghi = sorted(ultime28, key=lambda c: c["data"])
        for prev, cur in zip(lunghi, lunghi[1:]):
            pass
        max_28 = max(c["km"] for c in ultime28)
        prima = [c for c in corse if 28 < (OGGI - c["data"]).days <= 56]
        if prima:
            max_prima = max(c["km"] for c in prima)
            if max_28 > max_prima * 1.35 and max_prima > 3:
                segn.append(f"Il lungo è cresciuto da {max_prima:.1f} a {max_28:.1f} km in un mese (+{(max_28 / max_prima - 1) * 100:.0f}%).")

    # carico acuto
    if ultima.get("ctl") and ultima.get("atl"):
        if ultima["ctl"] > 0 and ultima["atl"] / ultima["ctl"] > 1.6:
            segn.append(f"Fatica acuta alta rispetto alla base (ATL/CTL {ultima['atl'] / ultima['ctl']:.2f}): "
                        f"settimana più leggera o un giorno in più di recupero.")

    # deriva della FC a parità di ritmo
    if len(validi) >= 6:
        ultimi3 = validi[-3:]
        prec3 = validi[-6:-3]
        ef_u = sum(c["gap_ms"] / c["fc"] for c in ultimi3) / 3
        ef_p = sum(c["gap_ms"] / c["fc"] for c in prec3) / 3
        if ef_u < ef_p * 0.94:
            segn.append("Efficienza in calo del 6% o più: possibile fatica accumulata, sonno o stress. "
                        "Se persiste due settimane, alleggerire.")

    # aderenza bassa
    lun_prec = lunedi_di(OGGI) - timedelta(weeks=1)
    p_prec = sum(f(r["minuti"], 0) for r in settimana_piano(piano, lun_prec) if di_corsa(r))
    r_prec = sum(c["min"] for c in corse if lun_prec <= c["data"] < lun_prec + timedelta(days=7))
    if p_prec > 0 and r_prec < p_prec * 0.6:
        segn.append(f"Settimana scorsa al {r_prec / p_prec * 100:.0f}% del piano: "
                    f"se si ripete, il piano va riscalato invece di accumulare arretrato.")

    # recupero
    if oggi.get("livello") == "rosso":
        segn.append(f"Prontezza bassa oggi ({oggi['prontezza']}/100): {oggi['consiglio']}")
    for n in oggi.get("note", []):
        segn.append(n)

    if segn:
        for s in segn:
            A(f"- {s}")
    else:
        A("- Nessuna anomalia.")

    OUT.write_text("\n".join(righe) + "\n", encoding="utf-8")
    print(f"Scritto {OUT.relative_to(ROOT)} ({len(righe)} righe).")


if __name__ == "__main__":
    main()
