#!/usr/bin/env python3
"""
Rattrapage ponctuel des infos de course et de cheval du 02/10 au 08/10/2026.

Avant le 08/10, le bot n'enregistrait pas encore le type de course
(handicap…), ni la corde, la carrière et les gains des chevaux. Ce script va
les rechercher dans le programme PMU de ces journées et les range À PART,
dans data/rattrapage/infos-AAAA-MM-JJ.json, sans toucher aux bilans.

Le 08/10 est inclus comme TÉMOIN : ce jour-là le bot avait déjà enregistré
ces infos le matin même ; si le rattrapage donne les mêmes valeurs, on sait
que le PMU renvoie bien les données d'avant-course et pas celles d'aujourd'hui.

Lancement : onglet Actions > « Rattrapage infos (ponctuel) » > Run workflow.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_pmu import (  # noqa: E402
    BASE_URL, CHAMPS_CHEVAL, CHAMPS_COURSE, fetch_json, garder_champs, is_galop,
)

JOURS = ["2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"]
SORTIE = Path(__file__).resolve().parent.parent / "data" / "rattrapage"


def infos_du_jour(date_iso):
    ddmmyyyy = date_iso[8:10] + date_iso[5:7] + date_iso[0:4]
    programme = fetch_json(f"{BASE_URL}/{ddmmyyyy}")
    resultat = {}
    for reunion in programme.get("programme", {}).get("reunions", []):
        num_r = reunion.get("numOfficiel") or reunion.get("numExterne") or reunion.get("numReunion")
        for course in reunion.get("courses", []):
            if not is_galop(course):
                continue
            num_c = course.get("numOrdre") or course.get("numExterne")
            code = f"R{num_r}C{num_c}"
            chevaux = {}
            try:
                participants = fetch_json(f"{BASE_URL}/{ddmmyyyy}/R{num_r}/C{num_c}/participants").get("participants", [])
            except Exception as e:
                print(f"  ! {date_iso} {code} : participants indisponibles ({type(e).__name__})", file=sys.stderr)
                participants = []
            for p in participants:
                infos = garder_champs(p, CHAMPS_CHEVAL)
                gains = p.get("gainsParticipant")
                if isinstance(gains, dict):
                    montants = {k: v for k, v in gains.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
                    if montants:
                        infos["gains"] = montants
                chevaux[str(p.get("numPmu"))] = {"cheval": p.get("nom", ""), "infosCheval": infos}
            resultat[code] = {"infosCourse": garder_champs(course, CHAMPS_COURSE), "chevaux": chevaux}
            time.sleep(0.2)  # rester poli avec le serveur du PMU
    return resultat


def main():
    SORTIE.mkdir(parents=True, exist_ok=True)
    for d in JOURS:
        try:
            infos = infos_du_jour(d)
        except Exception as e:
            print(f"! {d} : programme indisponible ({type(e).__name__}: {e})", file=sys.stderr)
            continue
        n = sum(len(c["chevaux"]) for c in infos.values())
        (SORTIE / f"infos-{d}.json").write_text(json.dumps({"date": d, "courses": infos}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{d} : {len(infos)} courses de galop, {n} chevaux enregistrés.")


if __name__ == "__main__":
    main()
