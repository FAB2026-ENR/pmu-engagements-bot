#!/usr/bin/env python3
"""
Remontée dans le temps : reconstruit les journées de galop passées (par défaut
du 01/07 au 01/10/2026) pour tester la stratégie Handicaps sur des courses
qu'elle n'a jamais vues.

Pour chaque journée :
  - toutes les courses de galop : jockey, entraîneur, arrivée de chaque partant
    (sert aux statistiques jockeys / entraîneurs, comme dans les bilans) ;
  - pour les handicaps de plat en France à 13 partants ou plus : en plus la
    musique, la note de forme, l'allocation de la dernière course (niveau),
    la corde, la carrière et le rapport gagnant.
Les fichiers vont dans data/historique/bilan-AAAA-MM-JJ.json, au même format
que les bilans du bot (les journées déjà faites sont sautées).

Témoin : les 08, 09 et 10/10 sont refaits aussi, pour comparer avec ce que le
bot avait enregistré le jour même.

Lancement : onglet Actions > « Remontée historique » > Run workflow. Le script
s'arrête de lui-même après ~45 minutes ; le relancer reprend où il s'était
arrêté.
"""

import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_pmu import (  # noqa: E402
    BASE_URL, CHAMPS_CHEVAL, CHAMPS_COURSE, fetch_json, fetch_prix_precedents, form_score,
    garder_champs, is_galop, is_reunion_france, parse_musique,
)
from fetch_bilan import extract_arrivee, fetch_cotes  # noqa: E402

SORTIE = Path(__file__).resolve().parent.parent / "data" / "historique"
DEBUT = date(2026, 7, 1)
FIN = date(2026, 10, 1)
TEMOINS = [date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 10)]
BUDGET_SECONDES = 45 * 60


def est_candidate(course, partants, france):
    """Handicap de plat en France à 13 partants ou plus (même règle que handicap.py)."""
    if not france or len(partants) < 13:
        return False
    if "HANDICAP" not in str(course.get("categorieParticularite") or ""):
        return False
    return (course.get("specialite") or "").upper() == "PLAT"


def non_partant(p):
    return "NON_PARTANT" in f"{p.get('statut') or ''} {p.get('incident') or ''}".upper()


def journee(d):
    ddmmyyyy = d.strftime("%d%m%Y")
    programme = fetch_json(f"{BASE_URL}/{ddmmyyyy}")
    entries, nb_suivies = [], 0
    for reunion in programme.get("programme", {}).get("reunions", []):
        num_r = reunion.get("numOfficiel") or reunion.get("numExterne") or reunion.get("numReunion")
        france = is_reunion_france(reunion)
        for course in reunion.get("courses", []):
            if not is_galop(course):
                continue
            num_c = course.get("numOrdre") or course.get("numExterne")
            try:
                participants = fetch_json(f"{BASE_URL}/{ddmmyyyy}/R{num_r}/C{num_c}/participants").get("participants", [])
            except Exception as e:
                print(f"  ! {d} R{num_r}C{num_c} : participants indisponibles ({type(e).__name__})", file=sys.stderr)
                continue
            partants = [p for p in participants if not non_partant(p)]
            suivie = est_candidate(course, partants, france)
            prix_prec, cotes = {}, {}
            if suivie:
                nb_suivies += 1
                prix_prec = fetch_prix_precedents(ddmmyyyy, num_r, num_c)
                try:
                    cotes = fetch_cotes(ddmmyyyy, num_r, num_c)
                except Exception:
                    cotes = {}
            info_course = garder_champs(course, CHAMPS_COURSE) if suivie else {
                k: course.get(k) for k in ("specialite", "categorieParticularite") if course.get(k)}
            for p in participants:
                place, statut = extract_arrivee(p)
                e = {
                    "reunion": f"R{num_r}", "course": f"C{num_c}", "cheval": p.get("nom", ""),
                    "jockey": p.get("driver") or p.get("montePar") or "", "entraineur": p.get("entraineur", ""),
                    "numPmu": p.get("numPmu"), "france": france, "arrivee": place, "statutArrivee": statut,
                    "infosCourse": info_course,
                }
                if suivie:
                    musique = p.get("musique", "")
                    e["musique"] = musique
                    e["prixJour"] = course.get("montantPrix")
                    e["prixPrec"] = prix_prec.get(p.get("numPmu"))
                    e["score"], _ = form_score(musique, e["prixJour"], e["prixPrec"])
                    e["nbCourses"] = len(parse_musique(musique))
                    infos = garder_champs(p, CHAMPS_CHEVAL)
                    e["infosCheval"] = {k: infos[k] for k in ("placeCorde", "nombreCourses", "nombreVictoires", "nombrePlaces") if k in infos}
                    c = cotes.get(p.get("numPmu"), {})
                    e["coteGagnant"] = c.get("gagnant")
                    e["cotePlace"] = c.get("place")
                    e["rapportsPublies"] = bool(cotes)
                entries.append(e)
            time.sleep(0.15)  # rester poli avec le serveur du PMU
    return {"date": d.isoformat(), "source": "historique", "entries": entries}, nb_suivies


def main():
    SORTIE.mkdir(parents=True, exist_ok=True)
    jours = [DEBUT + timedelta(days=i) for i in range((FIN - DEBUT).days + 1)] + TEMOINS
    depart = time.time()
    faits = 0
    for d in jours:
        f = SORTIE / f"bilan-{d.isoformat()}.json"
        if f.exists():
            continue
        if time.time() - depart > BUDGET_SECONDES:
            print("Temps écoulé : relancez le workflow pour continuer (les journées faites sont gardées).")
            break
        try:
            data, n = journee(d)
        except Exception as e:
            print(f"! {d} : journée indisponible ({type(e).__name__}: {e})", file=sys.stderr)
            continue
        f.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        faits += 1
        print(f"{d} : {len(data['entries'])} partants de galop, {n} handicap(s) suivi(s).")
    reste = sum(1 for d in jours if not (SORTIE / f"bilan-{d.isoformat()}.json").exists())
    print(f"{faits} journée(s) ajoutée(s) ; il en reste {reste}.")


if __name__ == "__main__":
    main()
