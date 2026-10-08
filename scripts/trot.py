#!/usr/bin/env python3
"""
Suivi du trot (attelé et monté), en parallèle du galop.

Ce fichier réutilise les trois scripts du galop sans les modifier : il les
charge, leur fait traiter les courses de trot au lieu du galop, et range
tout dans un dossier à part, data/trot/. Les données et les messages du
galop ne sont donc ni mélangés ni touchés.

Usage :
    python scripts/trot.py programme       # partants du jour (toutes les 30 min)
    python scripts/trot.py bilan           # arrivées, cotes, cumul (le soir)
    python scripts/trot.py telegram-matin  # messages du matin
    python scripts/trot.py telegram-soir   # messages du soir

Ce qui diffère du galop :
  - chaque partant garde sa discipline (attelé ou monté) ;
  - au trot, les disqualifications sont fréquentes et le PMU ne donne pas de
    place à un cheval disqualifié. Sans correction, ces chevaux sortiraient
    des comptes et la rentabilité serait surestimée. Ici, un cheval qui a
    pris le départ d'une course terminée et qui n'a pas de place est compté
    comme « non classé », donc comme un pari perdu ;
  - les croisements comparent attelé et monté (pas de « combinaison suivie »,
    qui est une hypothèse propre au galop) ;
  - pas de déclassement : au trot, la comparaison des allocations donnait
    presque tous les chevaux « en montée de catégorie », ce qui faussait les
    notes. La note du trot repose donc uniquement sur la musique.

Les messages Telegram du trot commencent tous par « 🐎 TROT ».
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_pmu as fp  # noqa: E402
import fetch_bilan as fb  # noqa: E402
import notify_telegram as nt  # noqa: E402

TROT_DIR = Path(__file__).resolve().parent.parent / "data" / "trot"
ENTETE_TELEGRAM = "🐎 TROT"

# Place fictive donnée à un cheval parti mais non classé (disqualifié,
# arrêté, distancé…) : il compte comme arrivé hors des trois premiers.
PLACE_NON_CLASSE = 99
NON_PARTANT = "NON_PARTANT"
STATUT_KEYS = ["statut", "incident", "statutArrivee", "statutParticipant"]

DISCIPLINE_LIBELLES = [("attelé", "attelé"), ("monté", "monté")]

# Version propre au trot, ajoutée à celle du galop : la changer fait
# recalculer tous les bilans du trot au passage du soir suivant.
# trot2 = note sans déclassement.
VERSION_TROT = "trot2"


# --- Reconnaître une course de trot ---

def discipline_trot(course: dict):
    """Renvoie « attelé », « monté », ou None si ce n'est pas du trot."""
    texte = f"{course.get('specialite') or ''} {course.get('discipline') or ''}".upper()
    if "MONTE" in texte:
        return "monté"
    if "ATTELE" in texte or "TROT" in texte:
        return "attelé"
    return None


def is_trot(course: dict) -> bool:
    return discipline_trot(course) is not None


# --- Programme du jour : mêmes données que le galop, plus la discipline ---

_collect_entries_galop = fp.collect_entries


def collect_entries_trot(date_ddmmyyyy: str):
    """Comme le script du galop, en notant au passage la discipline de
    chaque course (lue dans le programme que le script télécharge déjà)."""
    disciplines = {}
    fetch_d_origine = fp.fetch_json

    def fetch_et_note(url):
        data = fetch_d_origine(url)
        if url == f"{fp.BASE_URL}/{date_ddmmyyyy}" and isinstance(data, dict):
            for reunion in (data.get("programme") or {}).get("reunions") or []:
                num_reunion = reunion.get("numOfficiel") or reunion.get("numExterne") or reunion.get("numReunion")
                for course in reunion.get("courses") or []:
                    num_course = course.get("numOrdre") or course.get("numExterne")
                    disciplines[(f"R{num_reunion}", f"C{num_course}")] = discipline_trot(course)
        return data

    fp.fetch_json = fetch_et_note
    try:
        entries = _collect_entries_galop(date_ddmmyyyy)
    finally:
        fp.fetch_json = fetch_d_origine

    for e in entries:
        e["discipline"] = disciplines.get((e["reunion"], e["course"]))
    par_discipline = {}
    for e in entries:
        par_discipline[e["discipline"]] = par_discipline.get(e["discipline"], 0) + 1
    print(f"Trot — partants par discipline : {par_discipline}")
    return entries


# --- Bilan : compter les chevaux non classés comme des paris perdus ---

_extract_arrivee_galop = fb.extract_arrivee


def extract_arrivee_trot(participant: dict):
    place, statut = _extract_arrivee_galop(participant)
    if place is None:
        for key in STATUT_KEYS:
            val = participant.get(key)
            if isinstance(val, str) and "NON" in val.upper() and "PARTANT" in val.upper():
                return None, NON_PARTANT
    return place, statut


def compter_non_classes(entries):
    """Dans chaque course terminée (un gagnant est connu), un cheval sans
    place et qui n'est pas non-partant devient « non classé ». Renvoie le
    nombre de chevaux concernés."""
    par_course = {}
    for e in entries:
        par_course.setdefault((e.get("reunion"), e.get("course")), []).append(e)
    corriges = 0
    for chevaux in par_course.values():
        if not any(e.get("arrivee") == 1 for e in chevaux):
            continue  # course pas encore courue ou arrivée inconnue
        for e in chevaux:
            if e.get("arrivee") is None and e.get("statutArrivee") != NON_PARTANT:
                e["arrivee"] = PLACE_NON_CLASSE
                e["nonClasse"] = True
                corriges += 1
    return corriges


_build_croisements_galop = fb.build_croisements


def build_croisements_trot(bilan_entries):
    resultat = _build_croisements_galop(bilan_entries)
    resultat.pop("combinaison", None)
    retenus = [
        e for e in bilan_entries
        if e.get("score") is not None and fb.CROISEMENT_NOTE_MIN <= e["score"] < fb.CROISEMENT_NOTE_MAX
    ]
    resultat["discipline"] = [
        fb.aggregate(label, [e for e in retenus if e.get("discipline") == valeur])
        for label, valeur in DISCIPLINE_LIBELLES
    ]
    return resultat


_build_bilan_galop = fb.build_bilan


def build_bilan_trot(date_iso: str, date_ddmmyyyy: str):
    payload = _build_bilan_galop(date_iso, date_ddmmyyyy)
    if payload is None:
        return None
    entries = payload["entries"]
    corriges = compter_non_classes(entries)
    non_partants = sum(1 for e in entries if e.get("statutArrivee") == NON_PARTANT)
    print(f"  Trot {date_iso} : {corriges} cheval(aux) non classé(s) compté(s) comme perdant(s), {non_partants} non-partant(s) écarté(s).")
    # Les totaux ont été calculés avant la correction : on les refait.
    payload["buckets"], payload["classShiftBuckets"] = fb.build_aggregates(entries)
    payload["croisements"] = fb.build_croisements(entries)
    return payload


# --- Messages Telegram propres au trot ---

_send_galop = nt.send
_fmt_place_galop = nt.fmt_place


def send_trot(text: str):
    _send_galop(f"{ENTETE_TELEGRAM}\n{text}")


def fmt_place_trot(place):
    return "non classé" if place >= PLACE_NON_CLASSE else _fmt_place_galop(place)


def selection_matin_trot():
    """Tous les partants de trot du jour notés 0 à 0,99, avec leur discipline."""
    path = TROT_DIR / "latest.json"
    if not path.exists():
        print("data/trot/latest.json introuvable.", file=sys.stderr)
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("entries", [])

    retenus = []
    for e in entries:
        score, shift = fp.form_score(e.get("musique", ""))
        if nt.dans_la_tranche(score):
            retenus.append({**e, "score": score, "classShift": shift})
    retenus.sort(key=nt.ordre_course)

    lines = [f"🎯 Chevaux notés {nt.NOTE_LIBELLE} — {data.get('date', '')}"]
    if not retenus:
        lines.append(f"Aucun cheval dans cette tranche sur {len(entries)} partants.")
    else:
        mot = "chevaux" if len(retenus) > 1 else "cheval"
        lines.append(f"{len(retenus)} {mot} sur {len(entries)} partants (tous entraîneurs confondus)")
        lines.append("")

        def format_ligne(c):
            discipline = f" ({c['discipline']})" if c.get("discipline") else ""
            return (
                f"• {c.get('reunion', '')}{c.get('course', '')}{discipline} — {c['cheval']} — "
                f"note {nt.fmt_note(c['score'])} — {c.get('entraineur', '')}"
            )

        lines += nt.lignes_par_pays(retenus, nt.pays_des_reunions(data.get("date", "")), format_ligne)
        lines.append("")
        lines.append("Résultat de ces chevaux ce soir, avec le bilan.")
    nt.send("\n".join(lines))


def croisements_soir_trot():
    """La tranche 0 à 0,99 du trot découpée par critère, sur le cumul."""
    path = TROT_DIR / "bilan-cumul.json"
    if not path.exists():
        print("data/trot/bilan-cumul.json introuvable : pas de croisements.", file=sys.stderr)
        return
    try:
        cumul = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        print("data/trot/bilan-cumul.json illisible : pas de croisements.", file=sys.stderr)
        return
    croisements = cumul.get("croisements")
    if not croisements:
        return

    nb_jours = sum(1 for j in cumul.get("jours", []) if j.get("avecArrivee"))
    lines = [
        f"🔀 Croisements — chevaux notés {nt.NOTE_LIBELLE}",
        f"Cumul sur {nb_jours} jour(s), {croisements.get('n', 0)} partants arrivés",
        "g = gagnants, p = placés ; rentabilité gagnant / placé pour 1 €",
    ]

    def bloc(titre, groupes):
        rows = [g for g in (groupes or []) if g.get("n")]
        if not rows:
            return
        lines.append("")
        lines.append(titre)
        for g in rows:
            roi_g = "—" if g.get("roiGagnant") is None else nt.fmt_roi(g["roiGagnant"])
            roi_p = "—" if g.get("roiPlace") is None else nt.fmt_roi(g["roiPlace"])
            mot = "partant" if g["n"] == 1 else "partants"
            lines.append(f"• {g['label']} : {g['n']} {mot}, {g['nGagnants']} g, {g['nPlaces']} p → {roi_g} / {roi_p}")

    bloc("Selon la discipline", croisements.get("discipline"))
    bloc("Selon la cote du cheval", croisements.get("cote"))
    bloc("Selon le nombre de courses dans la musique", croisements.get("nbCourses"))
    bloc("Selon l'entraîneur", croisements.get("engagement"))
    bloc("Selon le pays de la réunion", croisements.get("pays"))
    nt.send("\n".join(lines))


# --- Branchement : les scripts du galop travaillent sur le trot ---

def brancher_sur_le_trot():
    # Pas de déclassement au trot : on ne va pas chercher l'allocation de la
    # dernière course, et le bilan l'ignore pour tous les jours.
    fp.fetch_prix_precedents = lambda *args, **kwargs: {}
    fb.CLASS_SHIFT_MIN_DATE = "9999-12-31"
    fb.BILAN_VERSION = f"{fb.BILAN_VERSION}-{VERSION_TROT}"
    fp.OUTPUT_DIR = TROT_DIR
    fb.OUTPUT_DIR = TROT_DIR
    nt.DATA_DIR = TROT_DIR
    fp.is_galop = is_trot
    fp.collect_entries = collect_entries_trot
    fb.extract_arrivee = extract_arrivee_trot
    fb.build_croisements = build_croisements_trot
    fb.build_bilan = build_bilan_trot
    nt.send = send_trot
    nt.fmt_place = fmt_place_trot


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    brancher_sur_le_trot()
    if mode == "programme":
        fp.main()
    elif mode == "bilan":
        fb.main()
    elif mode == "telegram-matin":
        selection_matin_trot()
        nt.morning()
    elif mode == "telegram-soir":
        nt.selection_soir()
        croisements_soir_trot()
        nt.bilan()
    else:
        print("Usage : trot.py programme|bilan|telegram-matin|telegram-soir", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
