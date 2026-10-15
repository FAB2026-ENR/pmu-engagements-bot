#!/usr/bin/env python3
"""
Complément de la remontée historique : la cote de chaque partant (dernier
rapport direct du PMU) dans les handicaps de plat en France à 13 partants et
plus, pour tester l'idée « notre classement + une cote minimale ».

Lit data/historique/bilan-*.json, n'interroge que les courses suivies, et
écrit data/historique/cotes-AAAA-MM-JJ.json : {"R1C4": {"7": 8.5, ...}, ...}.
Les journées déjà faites sont sautées ; s'arrête de lui-même après ~45 min.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_bilan import extract_cote_directe, fetch_cotes_directes_web  # noqa: E402
from fetch_pmu import BASE_URL, fetch_json  # noqa: E402
import handicap  # noqa: E402

DOSSIER = Path(__file__).resolve().parent.parent / "data" / "historique"
BUDGET_SECONDES = 45 * 60


def main():
    depart = time.time()
    faits = 0
    for f in sorted(DOSSIER.glob("bilan-*.json")):
        jour = f.stem.replace("bilan-", "")
        sortie = DOSSIER / f"cotes-{jour}.json"
        if sortie.exists():
            continue
        if time.time() - depart > BUDGET_SECONDES:
            print("Temps écoulé : relancez pour continuer.")
            break
        data = json.loads(f.read_text(encoding="utf-8"))
        ddmmyyyy = jour[8:10] + jour[5:7] + jour[0:4]
        resultat = {}
        for code in handicap.courses_suivies(data.get("entries", [])):
            num_r, num_c = code[1:].split("C")
            cotes = {}
            try:
                for p in fetch_json(f"{BASE_URL}/{ddmmyyyy}/R{num_r}/C{num_c}/participants").get("participants", []):
                    c = extract_cote_directe(p)
                    if c is not None:
                        cotes[str(p.get("numPmu"))] = c
                if not cotes:  # repli : accès web du PMU (par nom de cheval)
                    noms = {e.get("cheval"): e.get("numPmu") for e in data["entries"] if e["reunion"] + e["course"] == code}
                    for nom, c in fetch_cotes_directes_web(ddmmyyyy, num_r, num_c).items():
                        if nom in noms:
                            cotes[str(noms[nom])] = c
            except Exception as e:
                print(f"  ! {jour} {code} : cotes indisponibles ({type(e).__name__})", file=sys.stderr)
            resultat[code] = cotes
            time.sleep(0.15)
        sortie.write_text(json.dumps(resultat, separators=(",", ":")), encoding="utf-8")
        faits += 1
        print(f"{jour} : {len(resultat)} course(s), {sum(len(v) for v in resultat.values())} cotes.")
    print(f"{faits} journée(s) complétée(s).")


if __name__ == "__main__":
    main()
