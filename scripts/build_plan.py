#!/usr/bin/env python3
"""
Espande plan/config.yml in plan/sessions.csv — una riga per seduta.

Deterministico: stessi input, stesso output. Nessuna decisione qui
dentro, solo aritmetica. Le decisioni stanno nel config.
"""

import csv
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "plan" / "config.yml"
OUT = ROOT / "plan" / "sessions.csv"

GIORNI = {
    "lunedi": 0, "martedi": 1, "mercoledi": 2, "giovedi": 3,
    "venerdi": 4, "sabato": 5, "domenica": 6,
}


def log(m):
    print(m, flush=True)


def pace_to_sec(p):
    """'6:50' -> 410"""
    m, s = p.split(":")
    return int(m) * 60 + int(s)


def lunedi_di(d):
    return d - timedelta(days=d.weekday())


def interpola(a, b, i, n):
    """Valore i-esimo (0-based) di una progressione lineare da a a b su n passi."""
    if n <= 1:
        return float(b)
    return a + (b - a) * i / (n - 1)


def zona_bpm(cfg, zona):
    if zona is None:
        return None, None
    lo, hi = cfg["zone_fc"][zona]
    fcmax = cfg["atleta"]["fc_max"]
    return round(lo * fcmax), round(hi * fcmax)


def main():
    if not CONFIG.exists():
        log(f"ERRORE: manca {CONFIG}")
        sys.exit(1)

    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    tipi = cfg["tipi_seduta"]
    pace_s = pace_to_sec(cfg["atleta"]["ritmo_facile_indicativo"])
    scarichi = set(cfg.get("settimane_scarico", []))
    f_scarico = cfg.get("fattore_scarico", 0.7)
    preferiti = {k: GIORNI[v] for k, v in cfg.get("giorni_preferiti", {}).items()}
    riempimento = [GIORNI[g] for g in cfg.get("giorni_riempimento", ["mercoledi", "sabato"])]

    gare = {}
    for g in cfg.get("gare_intermedie", []):
        gare[g["data"]] = g
    obiettivo = cfg["gara_obiettivo"]
    gare[obiettivo["data"]] = {
        "nome": obiettivo["nome"],
        "distanza_km": obiettivo["distanza_km"],
        "modalita": "gara",
    }

    righe = []
    settimana_globale = 0

    for blocco in cfg["blocchi"]:
        inizio = lunedi_di(blocco["dal"])
        fine = blocco["al"]
        n_sett = ((fine - inizio).days // 7) + 1
        schema = blocco["schema"]

        vol_a, vol_b = blocco["minuti_settimana"]
        lungo_a, lungo_b = blocco["lungo_minuti"]

        for i in range(n_sett):
            settimana_globale += 1
            w_start = inizio + timedelta(weeks=i)

            vol = interpola(vol_a, vol_b, i, n_sett)
            lungo = interpola(lungo_a, lungo_b, i, n_sett)

            scarico = settimana_globale in scarichi
            if scarico:
                vol *= f_scarico
                lungo *= f_scarico

            # Il lungo prende la sua quota; il resto si divide fra le altre
            # in proporzione al peso di ciascun tipo (le sedute di qualità
            # pesano più di quelle facili). I tipi a durata fissa (mobilità,
            # potenziamento) restano fuori dal volume settimanale.
            altre = [s for s in schema
                     if s not in ("lungo", "ritmo_gara", "riposo")
                     and "minuti_fissi" not in tipi[s]]
            pesi = {s: tipi[s].get("quota_volume", 1.0) for s in set(altre)}
            peso_tot = sum(pesi[s] for s in altre) or 1.0
            resto = max(vol - lungo, 0)

            # Assegnazione dei giorni: prima i tipi con un giorno preferito,
            # poi le facili riempiono i giorni liberi.
            occupati = set()
            giorno_di = {}
            for idx, tipo in enumerate(schema):
                if tipo == "riposo":
                    continue
                pref = preferiti.get(tipo)
                if pref is not None and pref not in occupati:
                    giorno_di[idx] = pref
                    occupati.add(pref)
            for idx, tipo in enumerate(schema):
                if tipo == "riposo" or idx in giorno_di:
                    continue
                for g in riempimento:
                    if g not in occupati:
                        giorno_di[idx] = g
                        occupati.add(g)
                        break
                else:
                    giorno_di[idx] = riempimento[idx % len(riempimento)]

            for j, tipo in enumerate(schema):
                if tipo == "riposo":
                    continue
                spec = tipi[tipo]
                data = w_start + timedelta(days=giorno_di[j])

                disciplina = spec.get("disciplina", "corsa")
                if "minuti_fissi" in spec:
                    minuti = spec["minuti_fissi"]
                elif tipo in ("lungo", "ritmo_gara"):
                    minuti = lungo
                else:
                    minuti = resto * pesi[tipo] / peso_tot

                if minuti < 12:
                    continue

                gara = gare.get(data)
                if gara:
                    righe.append({
                        "data": data.isoformat(),
                        "settimana": settimana_globale,
                        "blocco": blocco["id"],
                        "scarico": "",
                        "tipo": "gara",
                        "disciplina": "corsa",
                        "minuti": "",
                        "km_indicativi": gara["distanza_km"],
                        "fc_min": "",
                        "fc_max": "",
                        "note": f"{gara['nome']} — modalita: {gara.get('modalita', 'gara')}",
                    })
                    continue

                zona = spec.get("hr_zone")
                fmin, fmax = zona_bpm(cfg, zona)
                note = spec.get("nota", "")
                if spec.get("extra"):
                    note = (note + " " + spec["extra"]).strip()

                righe.append({
                    "data": data.isoformat(),
                    "settimana": settimana_globale,
                    "blocco": blocco["id"],
                    "scarico": "si" if scarico else "",
                    "tipo": tipo,
                    "disciplina": disciplina,
                    "minuti": round(minuti),
                    "km_indicativi": round(minuti * 60 / pace_s, 1) if disciplina == "corsa" else "",
                    "fc_min": fmin or "",
                    "fc_max": fmax or "",
                    "note": note,
                })

    # Le gare che non sono cadute su un giorno di seduta vanno aggiunte comunque.
    date_presenti = {r["data"] for r in righe}
    for d, g in gare.items():
        if d.isoformat() not in date_presenti:
            righe.append({
                "data": d.isoformat(),
                "settimana": "",
                "blocco": "",
                "scarico": "",
                "tipo": "gara",
                "disciplina": "corsa",
                "minuti": "",
                "km_indicativi": g["distanza_km"],
                "fc_min": "",
                "fc_max": "",
                "note": f"{g['nome']} — modalita: {g.get('modalita', 'gara')}",
            })

    righe.sort(key=lambda r: r["data"])

    campi = ["data", "settimana", "blocco", "scarico", "tipo", "disciplina",
             "minuti", "km_indicativi", "fc_min", "fc_max", "note"]
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campi)
        w.writeheader()
        w.writerows(righe)

    sett = max((r["settimana"] for r in righe if r["settimana"]), default=0)
    log(f"Scritto {OUT.relative_to(ROOT)}: {len(righe)} sedute su {sett} settimane.")


if __name__ == "__main__":
    main()
