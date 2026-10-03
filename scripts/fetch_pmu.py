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
 
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data"
 
 
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
    if not musique:
        return []
    tokens = musique.strip().split()
    places = []
    for tok in tokens:
        if re.fullmatch(r"\(\d+\)", tok):
            continue
        m = re.match(r"^(\d{1,2})", tok)
        if m:
            places.append(int(m.group(1)))
        elif re.match(r"^[DTAa0]", tok, re.IGNORECASE):
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
 
 
def fetch_prix_precedents(date_ddmmyyyy: str, num_reunion, num_course):
    """Renvoie {numPmu: allocation_derniere_course} pour une course donnée,
    ou {} si l'endpoint échoue ou que sa structure a changé."""
    url = f"{PERF_BASE_URL}/{date_ddmmyyyy}/R{num_reunion}/C{num_course}/performances-detaillees"
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
    return result
 
 
def collect_entries(date_ddmmyyyy: str):
    programme = fetch_json(f"{BASE_URL}/{date_ddmmyyyy}")
    reunions = programme.get("programme", {}).get("reunions", [])
 
    entries = []
    for reunion in reunions:
        num_reunion = reunion.get("numOfficiel") or reunion.get("numExterne") or reunion.get("numReunion")
        courses = reunion.get("courses", [])
        for course in courses:
            if not is_galop(course):
                continue
            num_course = course.get("numOrdre") or course.get("numExterne")
            montant_prix = course.get("montantPrix")
 
            try:
                participants = fetch_json(
                    f"{BASE_URL}/{date_ddmmyyyy}/R{num_reunion}/C{num_course}/participants"
                ).get("participants", [])
            except requests.RequestException as e:
                print(f"  ! échec participants R{num_reunion}C{num_course}: {e}", file=sys.stderr)
                continue
 
            # Allocation de la dernière course de chaque cheval (pour détecter les
            # déclassements) — best-effort, peut échouer sans bloquer le reste.
            prix_precedents = fetch_prix_precedents(date_ddmmyyyy, num_reunion, num_course)
 
            for p in participants:
                cheval = p.get("nom", "")
                entraineur = p.get("entraineur", "")
                jockey = p.get("driver") or p.get("montePar") or ""
                musique = p.get("musique", "")
                num_pmu = p.get("numPmu")
 
                if not cheval or not entraineur:
                    continue
 
                entries.append({
                    "reunion": f"R{num_reunion}",
                    "course": f"C{num_course}",
                    "cheval": cheval,
                    "jockey": jockey,
                    "entraineur": entraineur,
                    "musique": musique,
                    "prixJour": montant_prix,
                    "prixPrec": prix_precedents.get(num_pmu),
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
 
    latest_path = OUTPUT_DIR / "latest.json"
    latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
 
    print(f"Écrit : {out_path} et {latest_path}")
 
 
if __name__ == "__main__":
    main()
 
