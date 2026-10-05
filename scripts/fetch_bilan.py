#!/usr/bin/env python3
"""
Tourne en soirée, une fois les courses de galop du jour terminées.

Reprend le fichier data/engagements-{date}.json déjà produit dans la journée
par fetch_pmu.py (réunion/course/cheval/entraîneur/score/déclassement), va
chercher pour chaque course l'arrivée officielle et les cotes gagnant/placé
définitives, et produit un bilan :
  - pour chaque cheval déclassé (monte ou descend en classe), sa place
    d'arrivée et ses cotes ;
  - une répartition par tranche de score de forme (ex. < 5, 5-7, 7-9, 9+)
    avec taux de réussite et cote moyenne — pour objectiver si les scores
    élevés gagnent effectivement à de belles cotes.

Comme pour fetch_pmu.py, les noms de champs PMU utilisés ici ne sont pas
documentés officiellement. Le code reste tolérant (plusieurs noms de champs
essayés) et journalise des échantillons de diagnostic en cas d'échec
d'extraction, pour pouvoir ajuster rapidement si le format réel diffère.
"""

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# Même calcul de score / déclassement que le script du matin.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_pmu import form_score  # noqa: E402

BASE_URL = "https://offline.turfinfo.api.pmu.fr/rest/client/7/programme"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; engagements-bot/1.0)",
    "Accept": "application/json",
}

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data"

# Tranches de pourcentage de déclassement (valeur absolue du %), par pas de
# 10 points — pour voir précisément à quel niveau de déclassement les
# chevaux arrivent le plus souvent et à quelle cote. Pas de tranche en
# dessous de 20% : c'est le seuil à partir duquel le script considère qu'il
# y a un déclassement (voir class_shift() dans fetch_pmu.py).
CLASS_PCT_BUCKETS = [
    ("20 – 29%", 20, 30),
    ("30 – 39%", 30, 40),
    ("40 – 49%", 40, 50),
    ("50 – 59%", 50, 60),
    ("60 – 69%", 60, 70),
    ("70 – 79%", 70, 80),
    ("80 – 89%", 80, 90),
    ("90%+", 90, None),
]

# Tranches de score de forme pour le bilan agrégé (plus bas = meilleure forme),
# une par note entière : la tranche "5" regroupe les scores de 5,0 à 5,99.
SCORE_BUCKETS = (
    [("< 0", None, 0)]
    + [(str(n), n, n + 1) for n in range(0, 10)]
    + [("10+", 10, None)]
)

# Version du format du bilan : 2 = calcul du score corrigé (lecture de la
# musique PMU) ; 3 = chaque partant indique si le PMU a publié les rapports de
# sa course (rapportsPublies). Un bilan enregistré avec une version plus
# ancienne est recalculé automatiquement.
BILAN_VERSION = 3

# Avant cette date, les engagements enregistrés mélangeaient des allocations
# en devises différentes (ex. Hong Kong) : le déclassement de ces jours-là est
# faux. On ne s'en sert donc pas, seul le score de forme est utilisé.
CLASS_SHIFT_MIN_DATE = "2026-10-04"

MAX_DEBUG_SAMPLES = 2
_debug_samples_shown = 0


def paris_today_ddmmyyyy() -> str:
    # Heure de Paris (été) moins 3h : une relance après minuit (jusqu'à 3h du
    # matin) bilante encore la journée de courses qui vient de se terminer.
    racing_day = datetime.now(timezone.utc) + timedelta(hours=2) - timedelta(hours=3)
    return racing_day.strftime("%d%m%Y")


def fetch_json(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=15)
    if resp.status_code != 200:
        print(f"  ! HTTP {resp.status_code} sur {url} — corps (300 car.) : {resp.text[:300]!r}", file=sys.stderr)
    resp.raise_for_status()
    return resp.json()


def debug_sample(label: str, obj):
    global _debug_samples_shown
    if _debug_samples_shown < MAX_DEBUG_SAMPLES:
        print(f"  ? {label}: {obj}", file=sys.stderr)
        _debug_samples_shown += 1


# --- Arrivée (place officielle d'un partant) ---

ARRIVEE_KEYS = ["ordreArrivee", "place", "placeArrivee", "position"]
STATUT_KEYS = ["statutArrivee", "statutParticipant", "incident", "statut"]


def extract_arrivee(participant: dict):
    """Renvoie (place:int|None, statut:str|None) pour un partant, une fois la
    course terminée. Tolérant à plusieurs noms/formats de champ possibles."""
    place = None
    for key in ARRIVEE_KEYS:
        val = participant.get(key)
        if val is None:
            continue
        if isinstance(val, dict):
            place = val.get("place")
            statut = val.get("statusArrivee") or val.get("statut")
            if place is not None or statut:
                return place, statut
        elif isinstance(val, (int, float)):
            place = int(val)
            break
    statut = None
    for key in STATUT_KEYS:
        val = participant.get(key)
        if isinstance(val, str) and val:
            statut = val
            break
    if place is None and statut is None:
        debug_sample("champ arrivée introuvable, clés dispo", list(participant.keys()))
    return place, statut


# --- Cotes gagnant / placé définitives ---

RAPPORTS_URL_SUFFIX = "rapports-definitifs"
# Le PMU utilise plusieurs variantes de typePari selon que la course est
# ouverte aux paris internationaux ou non : "SIMPLE_GAGNANT" vs
# "SIMPLE_GAGNANT_INTERNATIONAL" (idem pour PLACE). On matche par préfixe
# pour couvrir toutes les variantes sans devoir toutes les lister.
GAGNANT_PREFIX = "SIMPLE_GAGNANT"
PLACE_PREFIX = "SIMPLE_PLACE"
DIVIDENDE_KEYS = ["dividendePourUnEuro", "rapport", "dividende", "montant"]
COMBINAISON_KEYS = ["combinaison", "numPmu", "num"]


def fetch_cotes(date_ddmmyyyy: str, num_reunion, num_course):
    """Renvoie {numPmu: {'gagnant': float|None, 'place': float|None}}."""
    url = f"{BASE_URL}/{date_ddmmyyyy}/R{num_reunion}/C{num_course}/{RAPPORTS_URL_SUFFIX}"
    try:
        data = fetch_json(url)
    except (requests.RequestException, ValueError) as e:
        print(f"  ! rapports-definitifs indisponible R{num_reunion}C{num_course}: {e}", file=sys.stderr)
        return {}

    rapports_list = data if isinstance(data, list) else data.get("rapports", data.get("rapportsDefinitifs", []))
    result = {}
    for rapport in rapports_list or []:
        type_pari = (rapport.get("typePari") or "").upper()
        is_gagnant = type_pari.startswith(GAGNANT_PREFIX)
        is_place = type_pari.startswith(PLACE_PREFIX)
        if not is_gagnant and not is_place:
            continue
        for combi in rapport.get("rapports", rapport.get("combinaisons", [])):
            num_pmu = None
            for ck in COMBINAISON_KEYS:
                if ck in combi:
                    try:
                        num_pmu = int(str(combi[ck]).strip())
                        break
                    except (TypeError, ValueError):
                        continue
            if num_pmu is None:
                continue
            dividende = None
            for dk in DIVIDENDE_KEYS:
                if dk in combi and combi[dk] is not None:
                    try:
                        # Le PMU exprime les rapports en centimes pour 1€ misé
                        # (ex. 510 = 5,10€ rendus pour 1€) : on convertit en euros.
                        dividende = float(combi[dk]) / 100
                        break
                    except (TypeError, ValueError):
                        continue
            entry = result.setdefault(num_pmu, {"gagnant": None, "place": None})
            if is_gagnant:
                entry["gagnant"] = dividende
            else:
                entry["place"] = dividende

    if not result:
        debug_sample(f"rapports-definitifs vide/non reconnu R{num_reunion}C{num_course}", data if not isinstance(data, list) else data[:2])
    return result


def bucket_for_score(score):
    if score is None:
        return None
    for label, lo, hi in SCORE_BUCKETS:
        if (lo is None or score >= lo) and (hi is None or score < hi):
            return label
    return None


def bucket_for_pct(pct):
    if pct is None:
        return None
    for label, lo, hi in CLASS_PCT_BUCKETS:
        if (lo is None or pct >= lo) and (hi is None or pct < hi):
            return label
    return None


def aggregate(label, bucket_entries):
    """Taux de réussite (sur tous les partants dont l'arrivée est connue) et
    rentabilité d'une mise flat de 1€ par cheval.

    La rentabilité n'est calculée que sur les courses dont le PMU a publié les
    rapports (rapportsPublies) : sur les réunions étrangères, par exemple, il
    n'y a aucune cote, et compter un gagnant à 0€ fausserait tout. nRoi donne
    le nombre de partants réellement utilisés pour la rentabilité."""
    valides = [e for e in bucket_entries if e.get("arrivee") is not None]
    n = len(valides)
    n_gagnants = sum(1 for e in valides if e["arrivee"] == 1)
    n_places = sum(1 for e in valides if e["arrivee"] <= 3)

    cotes_gagnant_connues = [e["coteGagnant"] for e in valides if e["arrivee"] == 1 and e.get("coteGagnant") is not None]
    cote_moyenne = round(sum(cotes_gagnant_connues) / len(cotes_gagnant_connues), 2) if cotes_gagnant_connues else None

    valides_roi = [e for e in valides if e.get("rapportsPublies", True)]
    n_roi = len(valides_roi)
    retour_gagnant = sum((e.get("coteGagnant") or 0) for e in valides_roi if e["arrivee"] == 1)
    retour_place = sum((e.get("cotePlace") or 0) for e in valides_roi if e["arrivee"] <= 3)
    roi_gagnant = round(100 * (retour_gagnant - n_roi) / n_roi, 1) if n_roi else None
    roi_place = round(100 * (retour_place - n_roi) / n_roi, 1) if n_roi else None

    return {
        "label": label, "n": n, "nRoi": n_roi, "nGagnants": n_gagnants, "nPlaces": n_places,
        "tauxGagnant": round(100 * n_gagnants / n, 1) if n else None,
        "tauxPlace": round(100 * n_places / n, 1) if n else None,
        "coteGagnantMoyenne": cote_moyenne,
        "roiGagnant": roi_gagnant, "roiPlace": roi_place,
    }


def build_bilan(date_iso: str, date_ddmmyyyy: str):
    src_path = OUTPUT_DIR / f"engagements-{date_iso}.json"
    if not src_path.exists():
        print(f"Aucun fichier engagements-{date_iso}.json trouvé — rien à bilanter (le bot du matin n'a peut-être pas tourné).", file=sys.stderr)
        return None

    source = json.loads(src_path.read_text(encoding="utf-8"))
    entries = source.get("entries", [])
    if not entries:
        print("Fichier engagements trouvé mais vide.", file=sys.stderr)
        return None

    # Regroupe par course pour n'appeler chaque endpoint qu'une fois.
    by_course = {}
    for e in entries:
        by_course.setdefault((e["reunion"], e["course"]), []).append(e)

    bilan_entries = []
    for (reunion, course), course_entries in by_course.items():
        num_reunion = reunion.lstrip("R")
        num_course = course.lstrip("C")

        try:
            participants = fetch_json(
                f"{BASE_URL}/{date_ddmmyyyy}/R{num_reunion}/C{num_course}/participants"
            ).get("participants", [])
        except requests.RequestException as e:
            print(f"  ! échec participants (résultats) R{reunion}{course}: {e}", file=sys.stderr)
            participants = []
        arrivee_par_cheval = {}
        for p in participants:
            nom = p.get("nom", "")
            if nom:
                arrivee_par_cheval[nom] = extract_arrivee(p)

        cotes = fetch_cotes(date_ddmmyyyy, num_reunion, num_course)
        rapports_publies = bool(cotes)
        # Les cotes sont indexées par numPmu, pas par nom — on les relie via
        # la liste participants (seule source commune) plutôt que de les
        # réclamer séparément aux entrées sauvegardées (qui n'ont pas numPmu).
        cotes_par_cheval = {}
        for p in participants:
            nom = p.get("nom", "")
            num_pmu = p.get("numPmu")
            if nom and num_pmu in cotes:
                cotes_par_cheval[nom] = cotes[num_pmu]

        for e in course_entries:
            place, statut = arrivee_par_cheval.get(e["cheval"], (None, None))
            c = cotes_par_cheval.get(e["cheval"], {})
            prix_prec = e.get("prixPrec") if date_iso >= CLASS_SHIFT_MIN_DATE else None
            score, shift = form_score(e.get("musique", ""), e.get("prixJour"), prix_prec)
            bilan_entries.append({
                **e,
                "prixPrec": prix_prec,
                "score": score,
                "classShift": shift,
                "arrivee": place,
                "statutArrivee": statut,
                "coteGagnant": c.get("gagnant"),
                "cotePlace": c.get("place"),
                "rapportsPublies": rapports_publies,
            })

    bucket_results, class_shift_results = build_aggregates(bilan_entries)
    return {
        "version": BILAN_VERSION,
        "date": date_iso,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "entries": bilan_entries,
        "buckets": bucket_results,
        "classShiftBuckets": class_shift_results,
    }


def build_aggregates(bilan_entries):
    """Tranches de score de forme + tranches de % de déclassement (descend /
    monte) pour une liste de partants, d'un seul jour ou cumulés."""
    score_groups = {label: [] for label, _, _ in SCORE_BUCKETS}
    for e in bilan_entries:
        label = bucket_for_score(e.get("score"))
        if label:
            score_groups[label].append(e)
    bucket_results = [aggregate(label, score_groups[label]) for label, _, _ in SCORE_BUCKETS]

    class_shift_results = {}
    for shift_type in ("descend", "monte"):
        groups = {label: [] for label, _, _ in CLASS_PCT_BUCKETS}
        for e in bilan_entries:
            cs = e.get("classShift")
            if not cs or cs.get("type") != shift_type:
                continue
            label = bucket_for_pct(cs.get("pct"))
            if label:
                groups[label].append(e)
        class_shift_results[shift_type] = [aggregate(label, groups[label]) for label, _, _ in CLASS_PCT_BUCKETS]
    return bucket_results, class_shift_results


def build_cumul(racing_day_iso: str):
    """Additionne les bilans de tous les jours disponibles (data/engagements-*.json),
    en reconstruisant ceux qui manquent ou qui datent d'un ancien calcul, puis
    écrit data/bilan-cumul.json."""
    dates = sorted(
        p.stem.replace("engagements-", "")
        for p in OUTPUT_DIR.glob("engagements-*.json")
    )
    dates = [d for d in dates if d <= racing_day_iso]

    all_entries, jours = [], []
    for d in dates:
        bilan_path = OUTPUT_DIR / f"bilan-{d}.json"
        payload = None
        if bilan_path.exists():
            try:
                payload = json.loads(bilan_path.read_text(encoding="utf-8"))
            except ValueError:
                payload = None
            if payload and payload.get("version") != BILAN_VERSION:
                payload = None
        if payload is None:
            print(f"Reconstruction du bilan du {d}…")
            d_api = datetime.strptime(d, "%Y-%m-%d").strftime("%d%m%Y")
            try:
                payload = build_bilan(d, d_api)
            except requests.RequestException as e:
                print(f"  ! échec reconstruction {d}: {e}", file=sys.stderr)
                payload = None
            if payload:
                bilan_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if not payload:
            continue
        entries = payload.get("entries", [])
        avec_arrivee = sum(1 for e in entries if e.get("arrivee") is not None)
        jours.append({"date": d, "partants": len(entries), "avecArrivee": avec_arrivee})
        # Un jour sans aucune arrivée connue (courses non terminées ou API
        # indisponible) n'apporte rien : on ne l'ajoute pas aux totaux.
        if avec_arrivee:
            all_entries.extend(entries)

    bucket_results, class_shift_results = build_aggregates(all_entries)
    cumul = {
        "version": BILAN_VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "jours": jours,
        "nPartants": sum(1 for e in all_entries if e.get("arrivee") is not None),
        "classShiftDepuis": CLASS_SHIFT_MIN_DATE,
        "buckets": bucket_results,
        "classShiftBuckets": class_shift_results,
    }
    out = OUTPUT_DIR / "bilan-cumul.json"
    out.write_text(json.dumps(cumul, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Cumul écrit : {out} ({len(jours)} jour(s), {cumul['nPartants']} partants avec arrivée)")


def main():
    date_ddmmyyyy = paris_today_ddmmyyyy()
    date_iso = datetime.strptime(date_ddmmyyyy, "%d%m%Y").strftime("%Y-%m-%d")

    print(f"Construction du bilan du {date_iso}…")
    payload = build_bilan(date_iso, date_ddmmyyyy)
    if payload is None:
        print("Rien à écrire pour le jour.", file=sys.stderr)
    else:
        avec_arrivee = sum(1 for e in payload["entries"] if e.get("arrivee") is not None)
        print(f"{avec_arrivee}/{len(payload['entries'])} partants avec une arrivée trouvée.")

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUTPUT_DIR / f"bilan-{date_iso}.json"
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        latest_path = OUTPUT_DIR / "bilan-latest.json"
        latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Écrit : {out_path} et {latest_path}")

    build_cumul(date_iso)


if __name__ == "__main__":
    main()
