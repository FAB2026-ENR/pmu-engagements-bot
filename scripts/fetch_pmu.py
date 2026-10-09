#!/usr/bin/env python3
"""
Récupère le programme du jour depuis l'API non officielle du PMU,
filtre les courses de galop, et calcule le meilleur engagement par
entraîneur (même logique que l'outil HTML engagements-entraineurs.html).

Tourne côté serveur (GitHub Actions) : aucun souci de CORS puisque ce
n'est pas un appel initié par un navigateur.

Source : https://offline.turfinfo.api.pmu.fr/rest/client/7/programme/DDMMYYYY
Cette API n'est pas documentée officiellement par le PMU — elle est
largement utilisée par la communauté turfiste depuis des années, mais
rien ne garantit sa stabilité dans le temps. Si le script casse, c'est
probablement parce que la structure de la réponse a changé.
"""

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

BASE_URL = "https://offline.turfinfo.api.pmu.fr/rest/client/7/programme"
# Endpoint séparé qui donne, par cheval, ses dernières performances passées
# (avec l'allocation de chacune) — utilisé pour détecter les déclassements.
PERF_BASE_URL = "https://online.turfinfo.api.pmu.fr/rest/client/61/programme"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; engagements-bot/1.0)",
    "Accept": "application/json",
}

# Noms de champs confirmés sur un exemple réel de réponse de cet endpoint :
# {"allure": "...", "participants": [{"numPmu": 1, "nomCheval": "...",
#  "coursesCourues": [{"date": ..., "allocation": 90000, ...}, ...]}, ...]}
# Les variantes supplémentaires restent en repli au cas où le PMU change le format.
PAST_RACES_KEYS = ["coursesCourues", "courses", "performances", "historiquePerformances"]
PRIZE_KEYS = ["allocation", "montantPrix", "dotation", "montant"]

# Combien d'exemples de diagnostic afficher dans les logs si l'extraction échoue
# (pour voir la vraie structure renvoyée sans devoir deviner une deuxième fois).
MAX_DEBUG_SAMPLES = 2
_debug_samples_shown = 0

# Spécialités considérées comme "galop" (on exclut le trot : ATTELE / MONTE).
GALOP_SPECIALITES = {"PLAT", "HAIES", "STEEPLE-CHASE", "CROSS-COUNTRY", "CROSS"}

# Pour les réunions à l'étranger (ex. Hong Kong), l'allocation de la dernière
# course (prixPrec) est dans la devise locale alors que l'allocation du jour
# (prixJour) est en euros — comparer les deux donnerait un faux déclassement
# identique sur tous les chevaux de la réunion. On neutralise donc le
# classShift pour les réunions non françaises (le score de forme, lui, reste
# calculé normalement à partir de la musique).
FRANCE_LABELS = {"FRANCE"}
FRANCE_CODES = {"FRA", "FR"}
_country_debug_shown = False


def is_reunion_france(reunion: dict) -> bool:
    """Best-effort : détecte si une réunion a lieu en France. Par défaut
    (champ absent ou format inattendu), on considère que c'est la France pour
    ne pas désactiver le déclassement partout si le nom du champ a changé —
    un échantillon est journalisé pour ajuster si besoin."""
    global _country_debug_shown
    pays = reunion.get("pays")
    if isinstance(pays, dict):
        libelle = (pays.get("libelle") or pays.get("nom") or pays.get("name") or "").upper()
        code = (pays.get("code") or "").upper()
        if libelle or code:
            return libelle in FRANCE_LABELS or code in FRANCE_CODES
    elif isinstance(pays, str) and pays:
        return pays.upper() in FRANCE_LABELS or pays.upper() in FRANCE_CODES

    if not _country_debug_shown:
        print(
            f"  ? champ pays introuvable sur la réunion, clés dispo: {list(reunion.keys())}",
            file=sys.stderr,
        )
        _country_debug_shown = True
    return True

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data"

# Allocation moyenne des dernières courses de chaque cheval, enregistrée pour
# étudier un autre calcul du déclassement (comparer avec plusieurs courses
# plutôt qu'avec la seule dernière). N'entre PAS dans la note pour l'instant.
NB_COURSES_MOYENNE = 3
PRIX_MOYENS = {}  # (num_reunion, num_course, num_pmu) -> allocation moyenne

# Critères enregistrés pour être testés plus tard, un par un (ils n'entrent
# PAS dans la note) : déferrage, corde, poids, valeur handicap, recul au trot,
# terrain, type de départ, nombre de partants… Le PMU ne documente pas ses
# champs : on garde ceux qui existent parmi ces noms possibles, et on écrit
# chaque jour la liste des champs réellement fournis dans data/champs-pmu.json.
CHAMPS_CHEVAL = [
    "deferre", "placeCorde", "handicapValeur", "handicapPoids", "poidsConditionMonte",
    "handicapDistance", "oeilleres", "nombreCourses", "nombreVictoires", "nombrePlaces",
    "nombrePlacesSecond", "nombrePlacesTroisieme", "age", "sexe", "driverChange",
    "jumentPleine", "indicateurInedit", "supplement", "allure", "avisEntraineur",
]
CHAMPS_COURSE = [
    "distance", "parcours", "corde", "typePiste", "penetrometre", "nombreDeclaresPartants",
    "categorieParticularite", "conditionSexe", "conditionAge", "categorieStatut",
    "discipline", "specialite", "typeDepart", "departImmediat", "libelle",
]
_champs_vus = {}


def garder_champs(source: dict, noms):
    """Copie les champs demandés qui existent, en ne gardant que des valeurs
    simples (texte, nombre, ou petit dictionnaire de valeurs simples)."""
    resultat = {}
    for nom in noms:
        val = source.get(nom)
        if val is None:
            continue
        if isinstance(val, (str, int, float, bool)):
            resultat[nom] = val
        elif isinstance(val, dict) and len(val) <= 6 and all(
            isinstance(v, (str, int, float, bool)) or v is None for v in val.values()
        ):
            resultat[nom] = val
    return resultat


def noter_champs(nature: str, source: dict):
    """Retient, une fois par passage, la liste des champs fournis par le PMU."""
    if nature not in _champs_vus and isinstance(source, dict):
        _champs_vus[nature] = sorted(source.keys())


def paris_today_ddmmyyyy() -> str:
    """Date du jour au format attendu par l'API PMU (fuseau Paris, approx. UTC+1/+2)."""
    # Approximation simple : UTC+2 (heure d'été). Si besoin de précision horaire
    # exacte toute l'année, remplacer par zoneinfo("Europe/Paris").
    now_paris = datetime.now(timezone.utc) + timedelta(hours=2)
    return now_paris.strftime("%d%m%Y")


def fetch_json(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=15)
    # On journalise le détail avant de lever une exception, pour ne plus jamais
    # perdre silencieusement la vraie cause d'un échec (statut HTTP, corps de
    # réponse) derrière un simple "indisponible".
    if resp.status_code != 200:
        print(
            f"  ! HTTP {resp.status_code} sur {url} — corps (300 premiers car.) : "
            f"{resp.text[:300]!r}",
            file=sys.stderr,
        )
    resp.raise_for_status()
    return resp.json()


def is_galop(course: dict) -> bool:
    specialite = (course.get("specialite") or "").upper()
    discipline = (course.get("discipline") or "").upper()
    return specialite in GALOP_SPECIALITES or discipline in GALOP_SPECIALITES


# --- Logique de score, portée depuis engagements-entraineurs.html ---

def parse_musique(musique: str):
    """Extrait les places de la musique PMU, de la plus récente à la plus ancienne.

    Chaque course = un résultat suivi d'une lettre de discipline minuscule
    (ex. 5p, 0h, Dp), sans espace entre les courses. Un chiffre 1-9 = la place,
    0 = non placé (au-delà de la 9e place), une majuscule D/T/A/R = disqualifié,
    tombé, arrêté, refusé... Ces deux derniers cas comptent comme une mauvaise
    place (10). Les marqueurs d'année, ex. (25), sont ignorés."""
    if not musique:
        return []
    texte = re.sub(r"\([^)]*\)", "", musique)
    places = []
    for m in re.finditer(r"(\d{1,2}|[DTAR])(?=[a-z]|\s|$)", texte):
        tok = m.group(1)
        if tok.isdigit():
            v = int(tok)
            places.append(10 if v == 0 else min(v, 10))
        else:
            places.append(10)
    return places


def class_shift(prix_jour, prix_prec):
    if not prix_jour or not prix_prec:
        return None
    delta = (prix_jour - prix_prec) / prix_prec
    if delta <= -0.2:
        return {"type": "descend", "pct": round(-delta * 100)}
    if delta >= 0.2:
        return {"type": "monte", "pct": round(delta * 100)}
    return {"type": "stable", "pct": round(abs(delta) * 100)}


def form_score(musique: str, prix_jour=None, prix_prec=None):
    places = parse_musique(musique)
    if not places and not (prix_jour and prix_prec):
        return None, None

    if places:
        weighted, weight_sum = 0.0, 0.0
        for i, p in enumerate(places):
            w = 1 / (i + 1)
            weighted += p * w
            weight_sum += w
        score = weighted / weight_sum
        recent = places[:5]
        podiums = sum(1 for p in recent if 1 <= p <= 3)
        score -= podiums * 0.4
    else:
        score = 5.0  # neutre si musique absente mais allocation connue

    shift = class_shift(prix_jour, prix_prec)
    if shift and shift["type"] == "descend":
        score -= min(shift["pct"] / 100, 1) * 1.5
    elif shift and shift["type"] == "monte":
        score += min(shift["pct"] / 100, 1) * 0.8

    return round(score, 2), shift


def extract_last_prize(participant_perf: dict):
    """Cherche, dans la fiche perf d'un cheval, la liste de ses courses passées
    et renvoie l'allocation de la plus récente. Tolérant aux variantes de
    nommage au cas où le PMU changerait le format."""
    global _debug_samples_shown
    for list_key in PAST_RACES_KEYS:
        past = participant_perf.get(list_key)
        if isinstance(past, list) and past:
            last = past[0]  # la plus récente est généralement en tête
            for prize_key in PRIZE_KEYS:
                if prize_key in last and last[prize_key]:
                    try:
                        return float(last[prize_key])
                    except (TypeError, ValueError):
                        continue
            # Liste de courses trouvée mais aucun champ de prix reconnu dedans :
            # affiche un échantillon pour ajuster PRIZE_KEYS la prochaine fois.
            if _debug_samples_shown < MAX_DEBUG_SAMPLES:
                print(f"  ? champ de prix introuvable, clés dispo dans '{last.keys()}': {last}", file=sys.stderr)
                _debug_samples_shown += 1
            return None
    # Même la liste de courses passées n'a pas été trouvée sous un nom connu.
    if _debug_samples_shown < MAX_DEBUG_SAMPLES:
        print(f"  ? liste de courses passées introuvable, clés dispo: {list(participant_perf.keys())}", file=sys.stderr)
        _debug_samples_shown += 1
    return None


def extract_prix_moyen(participant_perf: dict):
    """Moyenne des allocations des NB_COURSES_MOYENNE dernières courses du
    cheval (celles dont l'allocation est connue), ou None."""
    for list_key in PAST_RACES_KEYS:
        past = participant_perf.get(list_key)
        if not isinstance(past, list) or not past:
            continue
        prix = []
        for course in past:
            if not isinstance(course, dict):
                continue
            for prize_key in PRIZE_KEYS:
                try:
                    valeur = float(course.get(prize_key) or 0)
                except (TypeError, ValueError):
                    continue
                if valeur > 0:
                    prix.append(valeur)
                    break
            if len(prix) >= NB_COURSES_MOYENNE:
                break
        return round(sum(prix) / len(prix)) if prix else None
    return None


def fetch_prix_precedents(date_ddmmyyyy: str, num_reunion, num_course):
    """Renvoie {numPmu: allocation_derniere_course} pour une course donnée,
    ou {} si l'endpoint échoue ou que sa structure a changé."""
    # URL confirmée en inspectant le code source réel de pmu.fr (fetchPerformancesDetaillees) :
    # le suffixe /pretty est obligatoire, pas de paramètre specialisation ici.
    url = f"{PERF_BASE_URL}/{date_ddmmyyyy}/R{num_reunion}/C{num_course}/performances-detaillees/pretty"
    try:
        data = fetch_json(url)
    except (requests.RequestException, ValueError) as e:
        print(f"  ! performances-detaillees indisponible R{num_reunion}C{num_course}: {e}", file=sys.stderr)
        return {}

    participants = data.get("participants", data if isinstance(data, list) else [])
    result = {}
    for p in participants:
        num_pmu = p.get("numPmu")
        if num_pmu is None:
            continue
        prix = extract_last_prize(p)
        if prix is not None:
            result[num_pmu] = prix
        try:
            moyen = extract_prix_moyen(p)
        except Exception:  # simple mesure d'étude : ne doit jamais bloquer le bot
            moyen = None
        if moyen is not None:
            PRIX_MOYENS[(str(num_reunion), str(num_course), num_pmu)] = moyen
    return result


def collect_entries(date_ddmmyyyy: str):
    programme = fetch_json(f"{BASE_URL}/{date_ddmmyyyy}")
    reunions = programme.get("programme", {}).get("reunions", [])

    entries = []
    for reunion in reunions:
        num_reunion = reunion.get("numOfficiel") or reunion.get("numExterne") or reunion.get("numReunion")
        reunion_france = is_reunion_france(reunion)
        if not reunion_france:
            print(f"  i réunion R{num_reunion} hors France détectée — déclassement désactivé pour cette réunion", file=sys.stderr)
        courses = reunion.get("courses", [])
        for course in courses:
            if not is_galop(course):
                continue
            num_course = course.get("numOrdre") or course.get("numExterne")
            montant_prix = course.get("montantPrix")
            try:
                noter_champs("course", course)
                infos_course = garder_champs(course, CHAMPS_COURSE)
            except Exception:  # mesure d'étude : ne doit jamais bloquer le bot
                infos_course = {}

            try:
                participants = fetch_json(
                    f"{BASE_URL}/{date_ddmmyyyy}/R{num_reunion}/C{num_course}/participants"
                ).get("participants", [])
            except requests.RequestException as e:
                print(f"  ! échec participants R{num_reunion}C{num_course}: {e}", file=sys.stderr)
                continue

            # Allocation de la dernière course de chaque cheval (pour détecter les
            # déclassements) — best-effort, peut échouer sans bloquer le reste.
            # Inutile (et risque de fausses alertes de devise) pour les réunions
            # à l'étranger : on n'appelle même pas l'endpoint dans ce cas.
            if reunion_france:
                prix_precedents = fetch_prix_precedents(date_ddmmyyyy, num_reunion, num_course)
            else:
                prix_precedents = {}

            for p in participants:
                cheval = p.get("nom", "")
                entraineur = p.get("entraineur", "")
                jockey = p.get("driver") or p.get("montePar") or ""
                musique = p.get("musique", "")
                num_pmu = p.get("numPmu")

                if not cheval or not entraineur:
                    continue
                try:
                    noter_champs("cheval", p)
                    infos_cheval = garder_champs(p, CHAMPS_CHEVAL)
                    # Gains : seuls les montants (carrière, année…), pour
                    # mesurer le niveau réel du cheval.
                    gains = p.get("gainsParticipant")
                    if isinstance(gains, dict):
                        montants = {k: v for k, v in gains.items()
                                    if isinstance(v, (int, float)) and not isinstance(v, bool)}
                        if montants:
                            infos_cheval["gains"] = montants
                except Exception:
                    infos_cheval = {}

                entries.append({
                    "reunion": f"R{num_reunion}",
                    "course": f"C{num_course}",
                    "cheval": cheval,
                    "jockey": jockey,
                    "entraineur": entraineur,
                    "musique": musique,
                    "prixJour": montant_prix,
                    "prixPrec": prix_precedents.get(num_pmu) if reunion_france else None,
                    "prixMoyen": PRIX_MOYENS.get((str(num_reunion), str(num_course), num_pmu)) if reunion_france else None,
                    "infosCheval": infos_cheval,
                    "infosCourse": infos_course,
                })
    return entries


def compute_best_engagements(entries):
    by_entraineur = {}
    for e in entries:
        by_entraineur.setdefault(e["entraineur"], []).append(e)

    results = []
    for entraineur, chevaux in by_entraineur.items():
        if len(chevaux) < 2:
            continue
        scored = []
        for c in chevaux:
            score, shift = form_score(c["musique"], c["prixJour"], c["prixPrec"])
            scored.append({**c, "score": score, "classShift": shift})
        ranked = sorted([c for c in scored if c["score"] is not None], key=lambda c: c["score"])
        for c in scored:
            c["meilleurEngagement"] = bool(ranked) and ranked[0]["cheval"] == c["cheval"] and ranked[0]["course"] == c["course"]
        results.append({"entraineur": entraineur, "chevaux": scored})

    results.sort(key=lambda r: r["entraineur"])
    return results


def main():
    date_ddmmyyyy = paris_today_ddmmyyyy()
    date_iso = datetime.strptime(date_ddmmyyyy, "%d%m%Y").strftime("%Y-%m-%d")

    print(f"Récupération du programme galop du {date_iso}…")
    entries = collect_entries(date_ddmmyyyy)
    print(f"{len(entries)} partants galop récupérés.")

    with_prix_prec = sum(1 for e in entries if e.get("prixPrec") is not None)
    print(f"{with_prix_prec}/{len(entries)} partants avec allocation de la dernière course trouvée.")

    best = compute_best_engagements(entries)
    print(f"{len(best)} entraîneur(s) avec 2+ engagements aujourd'hui.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": date_iso,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
        "bestEngagements": best,
    }

    out_path = OUTPUT_DIR / f"engagements-{date_iso}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        (OUTPUT_DIR / "champs-pmu.json").write_text(
            json.dumps({"date": date_iso, "champs": _champs_vus}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass

    latest_path = OUTPUT_DIR / "latest.json"
    latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Écrit : {out_path} et {latest_path}")


if __name__ == "__main__":
    main()
