#!/usr/bin/env python3
"""
Suivi « Handicaps » : handicaps de plat en France, 13 partants ou plus.

Dans chaque course retenue, les partants sont classés par le « classement
combiné » de la page Ligne Droite (onglet Mon pronostic) avec 9 critères :
note de forme, jockey (% victoires et % podiums), entraîneur (% victoires et
% podiums), cheval (% victoires et % places en carrière), niveau de la course
(descend le plus) et corde (la plus petite). Les gains de l'année ne sont pas
utilisés. Pour chaque critère, les partants sont classés de 1 au dernier ; on
fait la moyenne des rangs (sans donnée = rang du milieu).

Jockeys et entraîneurs : % sur leurs 20 dernières courses suivies, AVANT le
jour joué (5 courses minimum, sinon rang du milieu). Le matin, on publie nos
2 premiers (et le 3e en témoin) ; le soir, le résultat du jour et le cumul
depuis le 02/10 (02-07/10 complétés par data/rattrapage/), en simple gagnant
à 1 € par cheval, avec à part les courses nouvelles depuis le 15/10.

Appelé par notify_telegram.py : handicap.matin(send) et handicap.soir(send).
Ne doit jamais bloquer les autres messages.
"""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SUIVI_DEPUIS = "2026-10-02"   # début du suivi (02-07/10 complétés par data/rattrapage/)
NOUVEAU_DEPUIS = "2026-10-15"  # première journée jamais vue lors du choix de la stratégie
PARTANTS_MIN = 13
NB_DERNIERES = 20            # courses retenues pour juger jockeys et entraîneurs
MIN_COURSES_STAT = 5
CRITERES = ["note", "jv", "jp", "ev", "ep", "cv", "cp", "niv", "corde"]
SENS = {"note": 1, "jv": -1, "jp": -1, "ev": -1, "ep": -1, "cv": -1, "cp": -1, "niv": 1, "corde": 1}


def est_non_partant(e):
    statut = str(e.get("statutArrivee") or "").upper()
    return "NON" in statut and "PARTANT" in statut


def lettre_discipline(musique):
    texte = re.sub(r"\([^)]*\)", "", musique or "")
    m = re.search(r"(?:\d{1,2}|[DTAR])([a-z])", texte)
    return m.group(1) if m else None


def est_course_suivie(partants):
    """Plat, France, handicap, 13 partants ou plus."""
    if len(partants) < PARTANTS_MIN or partants[0].get("france") is not True:
        return False
    ic = partants[0].get("infosCourse") or {}
    if "HANDICAP" not in str(ic.get("categorieParticularite") or ""):
        return False
    if ic.get("specialite"):
        return ic["specialite"] == "PLAT"
    lettres = Counter(lettre_discipline(e.get("musique")) for e in partants)
    lettres.pop(None, None)
    return bool(lettres) and lettres.most_common(1)[0][0] == "p"


def completer(data):
    """Avant le 08/10, les bilans n'ont ni type de course ni infos cheval :
    on les reprend dans data/rattrapage/infos-AAAA-MM-JJ.json s'il existe."""
    f = DATA_DIR / "rattrapage" / f"infos-{data.get('date')}.json"
    if not f.exists():
        return data
    try:
        courses = json.loads(f.read_text(encoding="utf-8")).get("courses", {})
    except ValueError:
        return data
    for e in data.get("entries", []):
        c = courses.get(e.get("reunion", "") + e.get("course", ""))
        if not c:
            continue
        if not e.get("infosCourse"):
            e["infosCourse"] = c.get("infosCourse", {})
        h = c.get("chevaux", {}).get(str(e.get("numPmu")))
        if h and not e.get("infosCheval"):
            e["infosCheval"] = h.get("infosCheval", {})
    return data


def charger_bilans():
    """{date: bilan} pour tous les bilans galop enregistrés."""
    bilans = {}
    for f in sorted(DATA_DIR.glob("bilan-20*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if data.get("date"):
            bilans[data["date"]] = completer(data)
    return bilans


def historique_avant(bilans, jour):
    """Arrivées de chaque jockey / entraîneur sur les journées avant `jour`."""
    hist = {"j": defaultdict(list), "e": defaultdict(list)}
    for d in sorted(bilans):
        if d >= jour:
            break
        es = bilans[d].get("entries", [])
        finies = {e["reunion"] + e["course"] for e in es if e.get("arrivee") == 1}
        for e in es:
            if est_non_partant(e) or e.get("rapportsPublies") is False:
                continue
            a = e.get("arrivee")
            if a is None:
                if e["reunion"] + e["course"] not in finies:
                    continue
                a = 99
            for k, nom in (("j", e.get("jockey")), ("e", e.get("entraineur"))):
                if nom:
                    hist[k][nom].append(a)
    return hist


def stat(hist, k, nom):
    l = hist[k].get(nom, [])[-NB_DERNIERES:]
    if len(l) < MIN_COURSES_STAT:
        return None, None
    return sum(1 for a in l if a == 1) / len(l), sum(1 for a in l if a <= 3) / len(l)


def valeur(k, e, hist):
    c = e.get("infosCheval") or {}
    if k == "note":
        return e.get("score")
    if k in ("jv", "jp"):
        return stat(hist, "j", e.get("jockey"))[0 if k == "jv" else 1]
    if k in ("ev", "ep"):
        return stat(hist, "e", e.get("entraineur"))[0 if k == "ev" else 1]
    if k in ("cv", "cp"):
        n = c.get("nombreCourses") or 0
        if n < 3:
            return None
        return (c.get("nombreVictoires" if k == "cv" else "nombrePlaces") or 0) / n
    if k == "niv":
        if e.get("france") is not False and e.get("prixJour") and e.get("prixPrec"):
            return (e["prixJour"] - e["prixPrec"]) / e["prixPrec"]
        return None
    if k == "corde":
        return c.get("placeCorde") or None
    return None


def rangs(partants, k, hist):
    n = len(partants)
    out = [(n + 1) / 2] * n
    vals = sorted(((valeur(k, e, hist), i) for i, e in enumerate(partants) if valeur(k, e, hist) is not None),
                  key=lambda x: SENS[k] * x[0])
    r = 0
    while r < len(vals):
        s = r
        while s + 1 < len(vals) and vals[s + 1][0] == vals[r][0]:
            s += 1
        for q in range(r, s + 1):
            out[vals[q][1]] = (r + s) / 2 + 1
        r = s + 1
    return out


def classer(partants, hist):
    R = [rangs(partants, k, hist) for k in CRITERES]
    moy = [sum(r[i] for r in R) / len(CRITERES) for i in range(len(partants))]
    ordre = sorted(range(len(partants)), key=lambda i: (moy[i], partants[i].get("numPmu") or 0))
    return [(partants[i], moy[i]) for i in ordre]


def courses_suivies(entries):
    par = defaultdict(list)
    for e in entries:
        if not est_non_partant(e):
            par[e["reunion"] + e["course"]].append(e)
    return {c: ps for c, ps in sorted(par.items(), key=lambda x: (int(x[0][1:].split("C")[0]), int(x[0].split("C")[1]))) if est_course_suivie(ps)}


def nom(e):
    return f"{e.get('numPmu', '?')} {e.get('cheval', '')}"


def euros(x):
    return f"{x:.2f} €".replace(".", ",")


def pct(x):
    return ("+" if x >= 0 else "−") + f"{abs(x):.0f} %"


# ---------------------------------------------------------------- matin
def texte_matin():
    path = DATA_DIR / "latest.json"
    if not path.exists():
        return None
    from fetch_pmu import form_score
    data = json.loads(path.read_text(encoding="utf-8"))
    jour = data.get("date", "")
    entries = []
    for e in data.get("entries", []):
        score, _ = form_score(e.get("musique", ""), e.get("prixJour"), e.get("prixPrec"))
        entries.append({**e, "score": score})
    courses = courses_suivies(entries)
    lignes = ["🏇 Handicaps du jour — plat, France, 13 partants et plus", "Nos 2 premiers au classement combiné (9 critères), le 3e en témoin.", ""]
    if not courses:
        lignes.append("Aucun handicap de plat à 13 partants ou plus aujourd'hui. Pas de course, pas de jeu.")
    else:
        hist = historique_avant(charger_bilans(), jour)
        for code, ps in courses.items():
            cl = classer(ps, hist)
            libelle = ((ps[0].get("infosCourse") or {}).get("libelle") or "").title()
            lignes.append(f"{code} · {libelle} · {len(ps)} partants")
            lignes.append(f"   1. {nom(cl[0][0])}   2. {nom(cl[1][0])}   (3e : {nom(cl[2][0])})")
    lignes += ["", "Recherche en test, suivie chaque soir, perdants compris. Ni pronostic ni conseil de jeu."]
    return "\n".join(lignes)


# ---------------------------------------------------------------- soir
def resultats(bilans):
    """Liste de courses suivies depuis SUIVI_DEPUIS : (jour, code, classés)."""
    out = []
    for d in sorted(bilans):
        if d < SUIVI_DEPUIS:
            continue
        hist = historique_avant(bilans, d)
        for code, ps in courses_suivies(bilans[d].get("entries", [])).items():
            if not any(e.get("arrivee") == 1 for e in ps) or any(e.get("rapportsPublies") is False for e in ps):
                continue
            out.append((d, code, [e for e, _ in classer(ps, hist)]))
    return out


def gain(e):
    return (e.get("coteGagnant") or 0) if e.get("arrivee") == 1 else 0


def place_txt(e):
    a = e.get("arrivee")
    return "non classé" if a is None or a >= 90 else ("1er" if a == 1 else f"{a}e")


def texte_soir():
    path = DATA_DIR / "bilan-latest.json"
    if not path.exists():
        return None
    jour = json.loads(path.read_text(encoding="utf-8")).get("date", "")
    res = resultats(charger_bilans())
    if not res:
        return None
    lignes = ["🏇 Handicaps — résultat (plat, France, 13 partants et plus)", "Simple gagnant 1 € sur nos 2 premiers (3e en témoin).", ""]
    du_jour = [r for r in res if r[0] == jour]
    if du_jour:
        lignes.append(f"Le {jour[8:]}/{jour[5:7]} :")
    if not du_jour:
        lignes.append("Aujourd'hui : aucun handicap suivi.")
    for _, code, cl in du_jour:
        t = [f"{nom(e)} {place_txt(e)}{' ✅ ' + euros(gain(e)) if gain(e) else ''}" for e in cl[:3]]
        lignes.append(f"{code} : {t[0]} · {t[1]} · (3e : {t[2]})")
    lignes.append("")
    nouveaux = [r for r in res if r[0] >= NOUVEAU_DEPUIS]
    for titre_periode, lot in ((f"depuis le {SUIVI_DEPUIS[8:]}/{SUIVI_DEPUIS[5:7]}", res),
                               (f"courses nouvelles depuis le {NOUVEAU_DEPUIS[8:]}/{NOUVEAU_DEPUIS[5:7]}", nouveaux)):
        if not lot:
            continue
        for k in (2, 3):
            mise = k * len(lot)
            par_course = [sum(gain(e) for e in cl[:k]) for _, _, cl in lot]
            rapport = sum(par_course)
            trouves = sum(1 for g in par_course if g > 0)
            sans = (rapport - max(par_course) - (mise - k)) / (mise - k) * 100 if mise > k else 0
            titre = "Nos 2 premiers" if k == 2 else "Témoin, 3 premiers"
            lignes.append(f"{titre}, {titre_periode} : {len(lot)} courses, gagnant trouvé {trouves} fois, "
                          f"{euros(mise)} misés, {euros(rapport)} rapportés, {pct((rapport - mise) / mise * 100)} "
                          f"(sans la meilleure course : {pct(sans)}).")
        lignes.append("")
    lignes += ["Stratégie choisie le 14/10 : seules les courses à partir du 15/10 la testent vraiment. On suit sans rien changer jusqu'au 8 novembre."]
    return "\n".join(lignes)


def matin(send):
    try:
        txt = texte_matin()
    except Exception as e:  # ne jamais bloquer les autres messages
        print(f"Suivi handicaps (matin) ignoré : {type(e).__name__}: {e}")
        return
    if txt:
        send(txt)


def soir(send):
    try:
        txt = texte_soir()
    except Exception as e:
        print(f"Suivi handicaps (soir) ignoré : {type(e).__name__}: {e}")
        return
    if txt:
        send(txt)


if __name__ == "__main__":
    print(texte_matin())
    print()
    print(texte_soir())
