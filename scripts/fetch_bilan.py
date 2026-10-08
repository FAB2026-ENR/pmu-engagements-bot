"""
Tourne en soirée, une fois les courses de galop du jour terminées.

Reprend le fichier data/engagements-{date}.json déjà produit dans la journée
par fetch_pmu.py (réunion/course/cheval/entraîneur/score/déclassement), va
chercher pour chaque course l'arrivée officielle et les cotes gagnant/placé
définitives, et produit un bilan :
  - pour chaque cheval déclassé (monte ou descend en classe), sa place
    d'arrivée et ses cotes ;
  - une répartition par score de forme, une tranche par note entière
    (0, 1, 2 … 9, 10+), avec taux de réussite et cote moyenne — pour
    objectiver si les bons scores gagnent effectivement à de belles cotes.

Pour les chevaux notés de 0 à 0,99 (tranche « 0 »), le bilan ajoute des
croisements : selon la cote du cheval, le nombre de courses lues dans sa
musique, sa place dans l'écurie de son entraîneur ce jour-là, et le pays de
la réunion (France ou étranger), plus une combinaison suivie (France et
5 courses ou plus).

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
from fetch_pmu import form_score, parse_musique, is_reunion_france  # noqa: E402

BASE_URL = "https://offline.turfinfo.api.pmu.fr/rest/client/7/programme"
# Accès « web » du PMU : contient les rapports des courses « exclu web »
# (e-Simple Gagnant, e-Simple Placé), absents de l'accès points de vente.
ONLINE_BASE_URL = "https://online.turfinfo.api.pmu.fr/rest/client/61/programme"
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
# sa course (rapportsPublies) ; 4 = les rapports des courses « exclu web » sont
# aussi récupérés (accès web du PMU) ; 5 = chaque partant garde sa cote, le
# pays de sa réunion, le nombre de courses de sa musique et sa place dans
# l'écurie du jour, et le bilan contient les croisements de la tranche « 0 » ;
# 6 = la cote manquante (réunions étrangères) est cherchée par l'accès web, le
# croisement par cote ne traite plus les gagnants à part, et une combinaison
# suivie (France + 5 courses ou plus) est ajoutée.
# Un bilan enregistré avec une version plus ancienne est recalculé
# automatiquement.
BILAN_VERSION = 6

# Croisements : ils portent sur les chevaux dont la note est comprise entre
# CROISEMENT_NOTE_MIN inclus et CROISEMENT_NOTE_MAX exclu (0 à 0,99).
CROISEMENT_NOTE_MIN = 0
CROISEMENT_NOTE_MAX = 1
COTE_BUCKETS = [
    ("moins de 3", None, 3),
    ("3 à 5,9", 3, 6),
    ("6 à 11,9", 6, 12),
    ("12 et plus", 12, None),
]
NB_COURSES_BUCKETS = [
    ("1 à 2 courses", 1, 3),
    ("3 à 4 courses", 3, 5),
    ("5 courses et plus", 5, None),
]
ENGAGEMENT_LIBELLES = [
    ("meilleur engagement de son entraîneur", "meilleur"),
    ("autre cheval d'un entraîneur à 2+ partants", "autre"),
    ("entraîneur à un seul partant", "seul"),
]
PAYS_LIBELLES = [("France", True), ("étranger", False)]
# Combinaison suivie, fixée à l'avance : réunion en France ET au moins
# COMBINAISON_NB_COURSES courses lues dans la musique.
COMBINAISON_NB_COURSES = 5
COMBINAISON_LIBELLE = "France et 5 courses ou plus"
# Test honnête de la combinaison : seuls comptent les chevaux courus à partir
# de cette date, puisque ceux d'avant ont servi à trouver la piste.
TEST_DEPUIS = "2026-10-08"

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
# Le PMU utilise plusieurs noms de pari selon le canal : "SIMPLE_GAGNANT",
# "SIMPLE_GAGNANT_INTERNATIONAL", "E_SIMPLE_GAGNANT" (courses « exclu web »),
# idem pour PLACE. On cherche donc ces mots n'importe où dans le nom (sans
# confondre avec "COUPLE_GAGNANT", qui ne contient pas "SIMPLE_GAGNANT").
GAGNANT_MARK = "SIMPLE_GAGNANT"
PLACE_MARK = "SIMPLE_PLACE"
DIVIDENDE_KEYS = ["dividendePourUnEuro", "rapport", "dividende", "montant"]
COMBINAISON_KEYS = ["combinaison", "numPmu", "num"]


def parse_rapports(data):
    """Extrait {numPmu: {'gagnant': float|None, 'place': float|None}} d'une
    réponse « rapports-definitifs »."""
    rapports_list = data if isinstance(data, list) else data.get("rapports", data.get("rapportsDefinitifs", []))
    result = {}
    for rapport in rapports_list or []:
        type_pari = (rapport.get("typePari") or "").upper()
        is_gagnant = GAGNANT_MARK in type_pari
        is_place = PLACE_MARK in type_pari
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
    return result


def fetch_rapports(url: str, label: str):
    try:
        data = fetch_json(url)
    except (requests.RequestException, ValueError) as e:
        print(f"  ! rapports-definitifs indisponible {label}: {e}", file=sys.stderr)
        return {}
    result = parse_rapports(data)
    if not result:
        debug_sample(f"rapports-definitifs vide/non reconnu {label}", data if not isinstance(data, list) else data[:2])
    return result


def fetch_cotes(date_ddmmyyyy: str, num_reunion, num_course):
    """Renvoie {numPmu: {'gagnant': float|None, 'place': float|None}}.
    Essaie d'abord l'accès habituel, puis l'accès web (courses « exclu web »)."""
    label = f"R{num_reunion}C{num_course}"
    path = f"{date_ddmmyyyy}/R{num_reunion}/C{num_course}/{RAPPORTS_URL_SUFFIX}"
    result = fetch_rapports(f"{BASE_URL}/{path}", label)
    if result:
        return result
    return fetch_rapports(f"{ONLINE_BASE_URL}/{path}?specialisation=INTERNET", label + " (web)")


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


# --- Données supplémentaires pour les croisements ---

# Cote du cheval telle que le PMU l'affiche (dernier rapport connu du simple
# gagnant). Contrairement aux rapports définitifs, elle existe pour tous les
# partants, pas seulement pour le gagnant et les placés.
COTE_DIRECTE_KEYS = ["dernierRapportDirect", "dernierRapportReference"]


def extract_cote_directe(participant: dict):
    for key in COTE_DIRECTE_KEYS:
        val = participant.get(key)
        if isinstance(val, dict):
            val = val.get("rapport")
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            return float(val)
    return None


def fetch_cotes_directes_web(date_ddmmyyyy: str, num_reunion, num_course):
    """Renvoie {nom du cheval: cote} lue par l'accès web du PMU. Sert pour les
    courses (surtout à l'étranger) dont l'accès habituel ne donne pas la cote
    des partants. Renvoie {} si l'accès web ne répond pas ou n'a rien."""
    url = f"{ONLINE_BASE_URL}/{date_ddmmyyyy}/R{num_reunion}/C{num_course}/participants?specialisation=INTERNET"
    try:
        data = fetch_json(url)
    except (requests.RequestException, ValueError) as e:
        print(f"  ! cotes web indisponibles R{num_reunion}C{num_course}: {e}", file=sys.stderr)
        return {}
    participants = data.get("participants", []) if isinstance(data, dict) else []
    result = {}
    for p in participants or []:
        if not isinstance(p, dict):
            continue
        nom = p.get("nom", "")
        cote = extract_cote_directe(p)
        if nom and cote is not None:
            result[nom] = cote
    if participants and not result:
        debug_sample(f"cote introuvable par l'accès web R{num_reunion}C{num_course}, clés dispo", list(participants[0].keys()) if isinstance(participants[0], dict) else participants[0])
    return result


def fetch_reunions_france(date_ddmmyyyy: str):
    """Renvoie {'R3': True, 'R7': False, ...} (True = réunion en France), ou
    {} si le programme du jour est indisponible."""
    try:
        programme = fetch_json(f"{BASE_URL}/{date_ddmmyyyy}")
    except (requests.RequestException, ValueError) as e:
        print(f"  ! programme indisponible pour le pays des réunions : {e}", file=sys.stderr)
        return {}
    if not isinstance(programme, dict):
        return {}
    result = {}
    for reunion in (programme.get("programme") or {}).get("reunions") or []:
        num = reunion.get("numOfficiel") or reunion.get("numExterne") or reunion.get("numReunion")
        if num is not None:
            result[f"R{num}"] = is_reunion_france(reunion)
    return result


def marquer_engagements(bilan_entries):
    """Ajoute à chaque partant sa place dans l'écurie du jour : « meilleur »
    (mieux noté d'un entraîneur ayant 2 partants ou plus), « autre » (autre
    cheval du même entraîneur) ou « seul » (entraîneur à un seul partant)."""
    par_entraineur = {}
    for e in bilan_entries:
        par_entraineur.setdefault(e.get("entraineur", ""), []).append(e)
    for chevaux in par_entraineur.values():
        if len(chevaux) < 2:
            for e in chevaux:
                e["engagement"] = "seul"
            continue
        notes = [e for e in chevaux if e.get("score") is not None]
        meilleur = min(notes, key=lambda e: e["score"]) if notes else None
        for e in chevaux:
            e["engagement"] = "meilleur" if e is meilleur else "autre"


def build_croisements(bilan_entries):
    """Découpe les chevaux notés 0 à 0,99 selon quatre critères. Chaque
    groupe est calculé comme une tranche du bilan (voir aggregate)."""
    retenus = [
        e for e in bilan_entries
        if e.get("score") is not None and CROISEMENT_NOTE_MIN <= e["score"] < CROISEMENT_NOTE_MAX
    ]

    def par_tranches(buckets, valeur):
        groupes = {label: [] for label, _, _ in buckets}
        inconnus = []
        for e in retenus:
            v = valeur(e)
            cible = inconnus
            if v is not None:
                for label, lo, hi in buckets:
                    if (lo is None or v >= lo) and (hi is None or v < hi):
                        cible = groupes[label]
                        break
            cible.append(e)
        resultat = [aggregate(label, groupes[label]) for label, _, _ in buckets]
        if inconnus:
            resultat.append(aggregate("inconnu", inconnus))
        return resultat

    def par_valeurs(libelles, valeur):
        groupes = {label: [] for label, _ in libelles}
        inconnus = []
        for e in retenus:
            v = valeur(e)
            label = next((lab for lab, attendu in libelles if v is not None and v == attendu), None)
            (groupes[label] if label else inconnus).append(e)
        resultat = [aggregate(label, groupes[label]) for label, _ in libelles]
        if inconnus:
            resultat.append(aggregate("inconnu", inconnus))
        return resultat

    def cote(e):
        # Uniquement la cote affichée, pour tous les chevaux : se rabattre sur
        # la cote définitive pour les seuls gagnants fausserait les groupes
        # (les gagnants seraient classés, les perdants sans cote ne le
        # seraient pas).
        return e.get("coteDirecte") or None

    def dans_combinaison(e):
        return e.get("france") is True and (e.get("nbCourses") or 0) >= COMBINAISON_NB_COURSES

    def groupe_test():
        """La combinaison sur les seuls chevaux courus depuis TEST_DEPUIS, avec
        la rentabilité au gagnant sans son plus gros gagnant (critère de
        décision fixé le 8 octobre), écrite dans le libellé."""
        chevaux = [e for e in retenus if dans_combinaison(e) and (e.get("jour") or "") >= TEST_DEPUIS]
        resultat = aggregate("", chevaux)
        valides = [
            e for e in chevaux
            if e.get("arrivee") is not None and e.get("rapportsPublies", True)
        ]
        gains = [e.get("coteGagnant") or 0 for e in valides if e["arrivee"] == 1]
        sans_top = resultat.get("roiGagnant")
        if gains and len(valides) > 1:
            sans_top = round(100 * (sum(gains) - max(gains) - (len(valides) - 1)) / (len(valides) - 1), 1)
        jour = datetime.strptime(TEST_DEPUIS, "%Y-%m-%d").strftime("%d/%m")
        texte = "—" if sans_top is None else f"{sans_top:+.1f}".replace(".", ",") + " %"
        resultat["label"] = f"🧪 TEST depuis le {jour} (sans le plus gros gagnant : {texte})"
        resultat["roiGagnantSansTop"] = sans_top
        return resultat

    return {
        "noteMin": CROISEMENT_NOTE_MIN,
        "noteMax": CROISEMENT_NOTE_MAX,
        "n": sum(1 for e in retenus if e.get("arrivee") is not None),
        "cote": par_tranches(COTE_BUCKETS, cote),
        "nbCourses": par_tranches(NB_COURSES_BUCKETS, lambda e: e.get("nbCourses") or None),
        "engagement": par_valeurs(ENGAGEMENT_LIBELLES, lambda e: e.get("engagement")),
        "pays": par_valeurs(PAYS_LIBELLES, lambda e: e.get("france")),
        "combinaison": [
            groupe_test(),
            aggregate(COMBINAISON_LIBELLE + ", depuis le début", [e for e in retenus if dans_combinaison(e)]),
            aggregate("tous les autres chevaux de la tranche", [e for e in retenus if not dans_combinaison(e)]),
        ],
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

    reunions_france = fetch_reunions_france(date_ddmmyyyy)

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
        cote_directe_par_cheval = {}
        for p in participants:
            nom = p.get("nom", "")
            if nom:
                arrivee_par_cheval[nom] = extract_arrivee(p)
                cote_directe_par_cheval[nom] = extract_cote_directe(p)
        # Cote absente pour au moins un cheval de la course qui nous intéresse :
        # on la demande à l'accès web (un seul appel par course concernée).
        if any(cote_directe_par_cheval.get(e["cheval"]) is None for e in course_entries):
            for nom, cote_web in fetch_cotes_directes_web(date_ddmmyyyy, num_reunion, num_course).items():
                if cote_directe_par_cheval.get(nom) is None:
                    cote_directe_par_cheval[nom] = cote_web

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
                "coteDirecte": cote_directe_par_cheval.get(e["cheval"]),
                "france": reunions_france.get(e["reunion"]),
                "nbCourses": len(parse_musique(e.get("musique", ""))),
                "jour": date_iso,
            })

    marquer_engagements(bilan_entries)
    avec_cote = sum(1 for e in bilan_entries if e.get("coteDirecte") is not None)
    print(f"  {avec_cote}/{len(bilan_entries)} partants avec une cote affichée trouvée ({date_iso}).")

    bucket_results, class_shift_results = build_aggregates(bilan_entries)
    return {
        "version": BILAN_VERSION,
        "date": date_iso,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "entries": bilan_entries,
        "buckets": bucket_results,
        "classShiftBuckets": class_shift_results,
        "croisements": build_croisements(bilan_entries),
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
        "croisements": build_croisements(all_entries),
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
