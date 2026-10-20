#!/usr/bin/env python3
"""
Rapports définitifs INTERNET (e-Simple, e-Couplé, e-Trio, e-2sur4...) : ce que
paie pmu.fr, qui peut être très différent des rapports des points de vente
(ex. R2C6 du 14/10 : trio 170,90 € en point de vente, e-Trio 497,80 €).

Pour toutes les courses déjà présentes dans data/historique/rapports-*.json et
large-*.json, écrit data/historique/erapports-AAAA-MM-JJ.json :
  {"R2C6": {"E_TRIO": {"6-7-12": 497.8}, "E_SIMPLE_GAGNANT": {"6": 17.4}, ...}}
(numéros de chaque combinaison triés, rapports en euros pour 1 €).
Les journées déjà faites sont sautées ; s'arrête de lui-même après ~45 min.
"""

import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_bilan import DIVIDENDE_KEYS, ONLINE_BASE_URL, RAPPORTS_URL_SUFFIX  # noqa: E402
from fetch_pmu import fetch_json  # noqa: E402

DOSSIER = Path(__file__).resolve().parent.parent / "data" / "historique"
BUDGET_SECONDES = 45 * 60


def tous_les_rapports(data):
    """{typePari: {combinaison triée: rapport pour 1 €}} pour tous les paris."""
    liste = data if isinstance(data, list) else data.get("rapports", data.get("rapportsDefinitifs", []))
    out = {}
    for rapport in liste or []:
        if not isinstance(rapport, dict):
            continue
        type_pari = (rapport.get("typePari") or "?").upper()
        ordre = "ORDRE" in type_pari  # garder l'ordre pour les paris dans l'ordre
        for combi in rapport.get("rapports", rapport.get("combinaisons", [])) or []:
            if not isinstance(combi, dict):
                continue
            nums = [int(n) for n in re.findall(r"\d+", str(combi.get("combinaison", combi.get("numPmu", ""))))]
            if not nums:
                continue
            cle = "-".join(map(str, nums if ordre else sorted(nums)))
            for dk in DIVIDENDE_KEYS:
                if combi.get(dk) is not None:
                    try:
                        out.setdefault(type_pari, {})[cle] = float(combi[dk]) / 100
                        break
                    except (TypeError, ValueError):
                        continue
    return out


def codes_du_jour(jour):
    codes = set()
    for prefixe in ("rapports", "large"):
        f = DOSSIER / f"{prefixe}-{jour}.json"
        if f.exists():
            d = json.loads(f.read_text(encoding="utf-8"))
            codes |= set(d.get("rapports", d) if prefixe == "large" else d)
    return sorted(codes)


def bilans_galop():
    """{jour: fichier bilan} : historique avant le 02/10, bilans du bot ensuite (jusqu'à hier)."""
    out = {}
    for f in DOSSIER.glob("bilan-*.json"):
        jour = f.stem.replace("bilan-", "")
        if jour < "2026-10-02":
            out[jour] = f
    hier = (date.today() - timedelta(days=1)).isoformat()
    for f in (DOSSIER.parent).glob("bilan-20*.json"):
        jour = f.stem.replace("bilan-", "")
        if "2026-10-02" <= jour <= hier:
            out[jour] = f
    return out


def main():
    """Toutes les courses de galop des bilans (pour tester par exemple un jockey),
    en complétant les fichiers déjà faits sans refaire ce qui y est."""
    bilans = bilans_galop()
    jours = sorted(set(bilans) | {f.stem.split("-", 1)[1] for f in DOSSIER.glob("rapports-*.json")}
                   | {f.stem.split("-", 1)[1] for f in DOSSIER.glob("large-*.json")})
    depart = time.time()
    faits = 0
    for jour in jours:
        sortie = DOSSIER / f"erapports-{jour}.json"
        resultat = json.loads(sortie.read_text(encoding="utf-8")) if sortie.exists() else {}
        codes = set(codes_du_jour(jour))
        if jour in bilans:
            try:
                entries = json.loads(bilans[jour].read_text(encoding="utf-8")).get("entries", [])
                codes |= {e["reunion"] + e["course"] for e in entries if e.get("reunion") and e.get("course")}
            except ValueError:
                pass
        manquants = sorted(codes - set(resultat))
        if not manquants:
            continue
        if time.time() - depart > BUDGET_SECONDES:
            print("Temps écoulé : relancez pour continuer.")
            break
        ddmmyyyy = jour[8:10] + jour[5:7] + jour[0:4]
        for code in manquants:
            num_r, num_c = code[1:].split("C")
            try:
                data = fetch_json(f"{ONLINE_BASE_URL}/{ddmmyyyy}/R{num_r}/C{num_c}/{RAPPORTS_URL_SUFFIX}?specialisation=INTERNET")
                resultat[code] = tous_les_rapports(data)
            except Exception as e:
                print(f"  ! {jour} {code} : rapports internet indisponibles ({type(e).__name__})", file=sys.stderr)
                resultat[code] = {}
            time.sleep(0.15)
        sortie.write_text(json.dumps(resultat, separators=(",", ":")), encoding="utf-8")
        faits += 1
        types = sorted({t for v in resultat.values() for t in v})
        print(f"{jour} : +{len(manquants)} course(s), {len(resultat)} au total ; paris : {', '.join(types)}")
    print(f"{faits} journée(s) complétée(s).")


if __name__ == "__main__":
    main()
