#!/usr/bin/env python3
"""
Complément de la remontée historique : les rapports définitifs du couplé
gagnant, du couplé placé et du trio (pour 1 €) des handicaps de plat en France
à 13 partants et plus, pour mesurer la rentabilité « couplé / trio avec nos
4 premiers ».

Lit data/historique/bilan-*.json (01/07 -> 01/10) et les bilans du bot
(02/10 -> hier), n'interroge que les courses suivies, et écrit
data/historique/rapports-AAAA-MM-JJ.json :
  {"R1C4": {"coupleGagnant": {"3-7": 12.4}, "couplePlace": {...}, "trio": {"3-7-12": 85.2}}}
Les journées déjà faites sont sautées ; s'arrête de lui-même après ~45 min.
"""

import json
import re
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_bilan import DIVIDENDE_KEYS, ONLINE_BASE_URL, RAPPORTS_URL_SUFFIX, parse_couples  # noqa: E402
from fetch_pmu import BASE_URL, fetch_json  # noqa: E402
import handicap  # noqa: E402

DOSSIER = Path(__file__).resolve().parent.parent / "data" / "historique"
BUDGET_SECONDES = 45 * 60


def parse_trio(data):
    """{'3-7-12': 85.2, ...} : trio (ordre indifférent), numéros triés, en euros pour 1 €."""
    liste = data if isinstance(data, list) else data.get("rapports", data.get("rapportsDefinitifs", []))
    result = {}
    for rapport in liste or []:
        if not isinstance(rapport, dict):
            continue
        type_pari = (rapport.get("typePari") or "").upper()
        if "TRIO" not in type_pari or "ORDRE" in type_pari:
            continue
        for combi in rapport.get("rapports", rapport.get("combinaisons", [])) or []:
            if not isinstance(combi, dict):
                continue
            numeros = sorted(int(n) for n in re.findall(r"\d+", str(combi.get("combinaison", ""))))
            if len(numeros) != 3:
                continue
            for dk in DIVIDENDE_KEYS:
                if combi.get(dk) is not None:
                    try:
                        result["-".join(map(str, numeros))] = float(combi[dk]) / 100
                        break
                    except (TypeError, ValueError):
                        continue
    return result


def rapports_course(ddmmyyyy, num_r, num_c):
    chemin = f"{ddmmyyyy}/R{num_r}/C{num_c}/{RAPPORTS_URL_SUFFIX}"
    for url in (f"{BASE_URL}/{chemin}", f"{ONLINE_BASE_URL}/{chemin}?specialisation=INTERNET"):
        try:
            data = fetch_json(url)
        except Exception:
            continue
        couples = parse_couples(data)
        trio = parse_trio(data)
        if couples["gagnant"] or trio:
            return {"coupleGagnant": couples["gagnant"], "couplePlace": couples["place"], "trio": trio}
    return {}


def journees():
    """{date: entries} : historique avant le 02/10, bilans du bot ensuite (jusqu'à hier)."""
    jours = {}
    for f in sorted(DOSSIER.glob("bilan-*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("date", "") < "2026-10-02":
            jours[d["date"]] = d.get("entries", [])
    hier = date.today().isoformat()
    for jour, d in handicap.charger_bilans().items():
        if "2026-10-02" <= jour < hier:
            jours[jour] = d.get("entries", [])
    return jours


def main():
    depart = time.time()
    faits = 0
    for jour, entries in sorted(journees().items()):
        sortie = DOSSIER / f"rapports-{jour}.json"
        if sortie.exists():
            continue
        if time.time() - depart > BUDGET_SECONDES:
            print("Temps écoulé : relancez pour continuer.")
            break
        ddmmyyyy = jour[8:10] + jour[5:7] + jour[0:4]
        resultat = {}
        for code in handicap.courses_suivies(entries):
            num_r, num_c = code[1:].split("C")
            resultat[code] = rapports_course(ddmmyyyy, num_r, num_c)
            time.sleep(0.15)
        sortie.write_text(json.dumps(resultat, separators=(",", ":")), encoding="utf-8")
        faits += 1
        print(f"{jour} : {len(resultat)} course(s), "
              f"{sum(1 for v in resultat.values() if v.get('coupleGagnant'))} couplé(s), "
              f"{sum(1 for v in resultat.values() if v.get('trio'))} trio(s).")
    print(f"{faits} journée(s) complétée(s).")


if __name__ == "__main__":
    main()
