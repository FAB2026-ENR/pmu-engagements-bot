#!/usr/bin/env python3
"""
Remontée élargie : toutes les courses de plat en France de 10 à 16 partants
(handicaps ou non), du 01/07 à hier, avec ce qu'il faut pour notre classement
(musique, note de forme, niveau, corde, carrière) et les rapports définitifs
(simple gagnant, couplé gagnant, couplé placé, trio).

Les handicaps de 13 partants et plus déjà complets dans data/historique/ sont
sautés (déjà faits). Écrit data/historique/large-AAAA-MM-JJ.json :
  {"date": ..., "entries": [...], "rapports": {"R1C4": {"gagnant": {"7": 8.5},
   "coupleGagnant": {...}, "couplePlace": {...}, "trio": {...}}}}
Les journées déjà faites sont sautées ; s'arrête de lui-même après ~45 min.
"""

import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_pmu import (  # noqa: E402
    BASE_URL, CHAMPS_CHEVAL, CHAMPS_COURSE, fetch_json, fetch_prix_precedents, form_score,
    garder_champs, is_reunion_france, parse_musique,
)
from fetch_bilan import (  # noqa: E402
    ONLINE_BASE_URL, RAPPORTS_URL_SUFFIX, extract_arrivee, parse_couples, parse_rapports,
)
from historique_rapports import parse_trio  # noqa: E402

DOSSIER = Path(__file__).resolve().parent.parent / "data" / "historique"
DEBUT = date(2026, 7, 1)
PARTANTS_MIN, PARTANTS_MAX = 10, 16
BUDGET_SECONDES = 45 * 60


def non_partant(p):
    return "NON_PARTANT" in f"{p.get('statut') or ''} {p.get('incident') or ''}".upper()


def deja_complete(course, nb):
    """Handicap de plat à 13+ : déjà dans les bilans historiques complets."""
    return nb >= 13 and "HANDICAP" in str(course.get("categorieParticularite") or "")


def rapports_course(ddmmyyyy, num_r, num_c):
    chemin = f"{ddmmyyyy}/R{num_r}/C{num_c}/{RAPPORTS_URL_SUFFIX}"
    for url in (f"{BASE_URL}/{chemin}", f"{ONLINE_BASE_URL}/{chemin}?specialisation=INTERNET"):
        try:
            data = fetch_json(url)
        except Exception:
            continue
        simples = parse_rapports(data)
        couples = parse_couples(data)
        trio = parse_trio(data)
        if simples or couples["gagnant"] or trio:
            return {
                "gagnant": {str(n): v["gagnant"] for n, v in simples.items() if v.get("gagnant")},
                "place": {str(n): v["place"] for n, v in simples.items() if v.get("place")},
                "coupleGagnant": couples["gagnant"], "couplePlace": couples["place"], "trio": trio,
            }
    return {}


def journee(d):
    ddmmyyyy = d.strftime("%d%m%Y")
    programme = fetch_json(f"{BASE_URL}/{ddmmyyyy}")
    entries, rapports = [], {}
    for reunion in programme.get("programme", {}).get("reunions", []):
        if not is_reunion_france(reunion):
            continue
        num_r = reunion.get("numOfficiel") or reunion.get("numExterne") or reunion.get("numReunion")
        for course in reunion.get("courses", []):
            if (course.get("specialite") or course.get("discipline") or "").upper() != "PLAT":
                continue
            num_c = course.get("numOrdre") or course.get("numExterne")
            try:
                participants = fetch_json(f"{BASE_URL}/{ddmmyyyy}/R{num_r}/C{num_c}/participants").get("participants", [])
            except Exception as e:
                print(f"  ! {d} R{num_r}C{num_c} : participants indisponibles ({type(e).__name__})", file=sys.stderr)
                continue
            nb = sum(1 for p in participants if not non_partant(p))
            if not PARTANTS_MIN <= nb <= PARTANTS_MAX or deja_complete(course, nb):
                continue
            prix_prec = fetch_prix_precedents(ddmmyyyy, num_r, num_c)
            info_course = garder_champs(course, CHAMPS_COURSE)
            for p in participants:
                place, statut = extract_arrivee(p)
                musique = p.get("musique", "")
                infos = garder_champs(p, CHAMPS_CHEVAL)
                e = {
                    "reunion": f"R{num_r}", "course": f"C{num_c}", "cheval": p.get("nom", ""),
                    "jockey": p.get("driver") or p.get("montePar") or "", "entraineur": p.get("entraineur", ""),
                    "numPmu": p.get("numPmu"), "france": True, "arrivee": place, "statutArrivee": statut,
                    "infosCourse": info_course, "musique": musique,
                    "prixJour": course.get("montantPrix"), "prixPrec": prix_prec.get(p.get("numPmu")),
                    "nbCourses": len(parse_musique(musique)),
                    "infosCheval": {k: infos[k] for k in ("placeCorde", "nombreCourses", "nombreVictoires", "nombrePlaces") if k in infos},
                }
                e["score"], _ = form_score(musique, e["prixJour"], e["prixPrec"])
                if non_partant(p):
                    e["statut"] = "NON_PARTANT"
                entries.append(e)
            rapports[f"R{num_r}C{num_c}"] = rapports_course(ddmmyyyy, num_r, num_c)
            time.sleep(0.15)
    return {"date": d.isoformat(), "source": "historique_large", "entries": entries, "rapports": rapports}


def main():
    DOSSIER.mkdir(parents=True, exist_ok=True)
    hier = date.today() - timedelta(days=1)
    jours = [DEBUT + timedelta(days=i) for i in range((hier - DEBUT).days + 1)]
    depart = time.time()
    faits = 0
    for d in jours:
        f = DOSSIER / f"large-{d.isoformat()}.json"
        if f.exists():
            continue
        if time.time() - depart > BUDGET_SECONDES:
            print("Temps écoulé : relancez le workflow pour continuer (les journées faites sont gardées).")
            break
        try:
            data = journee(d)
        except Exception as e:
            print(f"! {d} : journée indisponible ({type(e).__name__}: {e})", file=sys.stderr)
            continue
        f.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        faits += 1
        print(f"{d} : {len(data['rapports'])} course(s), {sum(1 for r in data['rapports'].values() if r.get('trio'))} trio(s).")
    reste = sum(1 for d in jours if not (DOSSIER / f"large-{d.isoformat()}.json").exists())
    print(f"{faits} journée(s) ajoutée(s) ; il en reste {reste}.")


if __name__ == "__main__":
    main()
