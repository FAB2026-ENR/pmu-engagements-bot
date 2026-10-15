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

Alerte régularité : en plus, tout cheval de ces handicaps noté 0 à 0,99 avec
5 courses ou plus dans sa musique est signalé le matin et suivi le soir
(observation, très peu de cas).

Appelé par notify_telegram.py : handicap.matin(send) et handicap.soir(send).
Ne doit jamais bloquer les autres messages.
"""

import html
import json
import os
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


def nb_courses(e):
    if e.get("nbCourses") is not None:
        return e["nbCourses"]
    from fetch_pmu import parse_musique
    return len(parse_musique(e.get("musique", "")))


def est_alerte(e):
    """Alerte régularité : note de forme 0 à 0,99 et 5 courses ou plus dans la musique."""
    s = e.get("score")
    return s is not None and 0 <= s < 1 and nb_courses(e) >= 5


def note_txt(s):
    return f"{s:.2f}".replace(".", ",")


def nom(e):
    return f"{e.get('numPmu', '?')} {e.get('cheval', '')}"


def h(s):
    """Texte sûr pour le format HTML de Telegram."""
    return html.escape(str(s or ""), quote=False)


def euros(x):
    return f"{x:.2f} €".replace(".", ",")


def pct(x):
    return ("+" if x >= 0 else "−") + f"{abs(x):.0f} %"


def titre_course(ps):
    return ((ps[0].get("infosCourse") or {}).get("libelle") or "").title()


def fiche_alerte(code, e, rang, ps):
    """Bloc détaillé d'un cheval en alerte régularité (HTML)."""
    c = e.get("infosCheval") or {}
    lignes = [f"🟢 <b>{h(code)} — n°{h(e.get('numPmu'))} {h(e.get('cheval'))}</b>",
              f"   {h(titre_course(ps))} · {len(ps)} partants",
              f"   Note de forme <b>{note_txt(e['score'])}</b> · {nb_courses(e)} courses · musique <code>{h(e.get('musique'))}</code>",
              f"   Jockey {h(e.get('jockey'))} · entraîneur {h(e.get('entraineur'))}"
              + (f" · corde {c['placeCorde']}" if c.get("placeCorde") else ""),
              f"   <b>{rang}{'er' if rang == 1 else 'e'}</b> de notre classement combiné"]
    return "\n".join(lignes)


# ---------------------------------------------------------------- matin
def textes_matin():
    """(message principal, message d'alerte ou None), au format HTML."""
    path = DATA_DIR / "latest.json"
    if not path.exists():
        return None, None
    from fetch_pmu import form_score
    data = json.loads(path.read_text(encoding="utf-8"))
    jour = data.get("date", "")
    entries = []
    for e in data.get("entries", []):
        score, _ = form_score(e.get("musique", ""), e.get("prixJour"), e.get("prixPrec"))
        entries.append({**e, "score": score})
    courses = courses_suivies(entries)
    lignes = ["🏇 <b>HANDICAPS DU JOUR</b>",
              "<i>Plat · France · 13 partants et plus · classement combiné (9 critères)</i>", ""]
    alertes = []
    if not courses:
        lignes.append("Aucun handicap de plat à 13 partants ou plus aujourd'hui. Pas de course, pas de jeu.")
    else:
        hist = historique_avant(charger_bilans(), jour)
        for code, ps in courses.items():
            cl = classer(ps, hist)
            lignes.append(f"🟧 <b>{h(code)}</b> · {h(titre_course(ps))} · {len(ps)} partants")
            lignes.append(f"   🥇 <b>{h(nom(cl[0][0]))}</b>")
            lignes.append(f"   🥈 <b>{h(nom(cl[1][0]))}</b>")
            lignes.append(f"   ▫️ <i>3e : {h(nom(cl[2][0]))}</i>")
            lignes.append("")
            for rang, (e, _) in enumerate(cl, 1):
                if est_alerte(e):
                    alertes.append(fiche_alerte(code, e, rang, ps))
    lignes.append("⭐ Alerte régularité : " + (f"<b>{len(alertes)} cheval(aux)</b>, voir le message suivant." if alertes else "aucun cheval aujourd'hui."))
    lignes += ["", "<i>Recherche en test, suivie chaque soir, perdants compris. Ni pronostic ni conseil de jeu.</i>"]
    alerte = None
    if alertes:
        alerte = "\n".join(["🚨⭐ <b>ALERTE RÉGULARITÉ</b> ⭐🚨",
                            "<i>Cheval noté 0 à 0,99 avec 5 courses ou plus, dans un handicap de plat à 13 partants et plus.</i>",
                            ""] + [a + "\n" for a in alertes] +
                           ["<i>Profil très rare : 4 cas du 02 au 14/10, 3 gagnants. Simple observation, résultat ce soir.</i>"])
    return "\n".join(lignes), alerte


def texte_matin():
    principal, alerte = textes_matin()
    return "\n\n".join(x for x in (principal, alerte) if x)


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


def pastille(e):
    a = e.get("arrivee")
    return "🟢" if a == 1 else ("🟡" if a and a <= 3 else "🔴")


def ligne_cheval(e):
    g = gain(e)
    return f"{pastille(e)} {h(nom(e))} — {place_txt(e)}" + (f" · <b>{euros(g)}</b>" if g else "")


def bilan_periode(titre, lot):
    lignes = [f"📈 <b>{titre}</b> — {len(lot)} courses"]
    for k in (2, 3):
        mise = k * len(lot)
        par_course = [sum(gain(e) for e in cl[:k]) for _, _, cl in lot]
        rapport = sum(par_course)
        trouves = sum(1 for g in par_course if g > 0)
        sans = (rapport - max(par_course) - (mise - k)) / (mise - k) * 100 if mise > k else 0
        roi = (rapport - mise) / mise * 100
        rond = "🟢" if roi >= 0 else "🔴"
        qui = "Nos 2 premiers" if k == 2 else "<i>Témoin, 3 premiers</i>"
        lignes.append(f"   {rond} {qui} : <b>{pct(roi)}</b> · gagnant trouvé {trouves} fois · {euros(mise)} misés, {euros(rapport)} rapportés "
                      f"<i>(sans la meilleure course : {pct(sans)})</i>")
    return lignes


def texte_soir():
    path = DATA_DIR / "bilan-latest.json"
    if not path.exists():
        return None
    jour = json.loads(path.read_text(encoding="utf-8")).get("date", "")
    res = resultats(charger_bilans())
    if not res:
        return None
    lignes = ["🏇 <b>HANDICAPS — RÉSULTAT</b>",
              "<i>Plat · France · 13 partants et plus · simple gagnant 1 € sur nos 2 premiers</i>", ""]
    du_jour = [r for r in res if r[0] == jour]
    if du_jour:
        lignes.append(f"<b>Le {jour[8:]}/{jour[5:7]}</b>")
        for _, code, cl in du_jour:
            lignes.append(f"🟧 <b>{h(code)}</b>")
            lignes.append(f"   🥇 {ligne_cheval(cl[0])}")
            lignes.append(f"   🥈 {ligne_cheval(cl[1])}")
            lignes.append(f"   ▫️ <i>3e : {ligne_cheval(cl[2])}</i>")
        mise = 2 * len(du_jour)
        rap = sum(gain(e) for _, _, cl in du_jour for e in cl[:2])
        lignes.append(f"   Journée : {euros(mise)} misés, <b>{euros(rap)}</b> rapportés ({pct((rap - mise) / mise * 100)}).")
    else:
        lignes.append("Aujourd'hui : aucun handicap suivi.")
    lignes.append("")
    nouveaux = [r for r in res if r[0] >= NOUVEAU_DEPUIS]
    if nouveaux:
        lignes += bilan_periode(f"Le vrai test : courses nouvelles depuis le {NOUVEAU_DEPUIS[8:]}/{NOUVEAU_DEPUIS[5:7]}", nouveaux) + [""]
    lignes += bilan_periode(f"Depuis le {SUIVI_DEPUIS[8:]}/{SUIVI_DEPUIS[5:7]}", res) + [""]
    # Alerte régularité : chevaux notés 0 à 0,99 (5 courses ou plus) dans ces handicaps.
    alerte_jour = [(code, e) for d, code, cl in du_jour for e in cl if est_alerte(e)]
    toutes = [e for _, _, cl in res for e in cl if est_alerte(e)]
    lignes.append("⭐ <b>Alerte régularité</b>")
    for code, e in alerte_jour:
        lignes.append(f"   {h(code)} : {ligne_cheval(e)}")
    if not alerte_jour:
        lignes.append("   Aucun cheval aujourd'hui.")
    if toutes:
        rap = sum(gain(e) for e in toutes)
        gagnes = sum(1 for e in toutes if gain(e))
        lignes.append(f"   Depuis le {SUIVI_DEPUIS[8:]}/{SUIVI_DEPUIS[5:7]} : {len(toutes)} cheval(aux), {gagnes} gagnant(s), "
                      f"{euros(len(toutes))} misés, {euros(rap)} rapportés, <b>{pct((rap - len(toutes)) / len(toutes) * 100)}</b>. <i>Très peu de cas : simple observation.</i>")
    lignes += ["", "<i>Stratégie choisie le 14/10 : seules les courses à partir du 15/10 la testent vraiment. On suit sans rien changer jusqu'au 8 novembre.</i>"]
    return "\n".join(lignes)


# ---------------------------------------------------------------- envoi
def envoyer_html(texte, send_secours):
    """Envoie en HTML (gras, italique) ; en cas d'échec, renvoie en texte simple."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Secrets Telegram absents : rien envoyé.")
        return
    if len(texte) > 3800:  # trop long pour un seul message : texte simple, découpé par send()
        send_secours(re.sub(r"</?(b|i|code)>", "", html.unescape(texte)))
        return
    try:
        import requests
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat_id, "text": texte, "parse_mode": "HTML", "disable_web_page_preview": "true"},
                          timeout=15)
        if r.status_code == 200:
            return
        print(f"Envoi HTML refusé ({r.status_code}) : renvoi en texte simple.")
    except Exception as e:
        print(f"Envoi HTML impossible ({type(e).__name__}) : renvoi en texte simple.")
    send_secours(re.sub(r"</?(b|i|code)>", "", html.unescape(texte)))


def matin(send):
    try:
        principal, alerte = textes_matin()
    except Exception as e:  # ne jamais bloquer les autres messages
        print(f"Suivi handicaps (matin) ignoré : {type(e).__name__}: {e}")
        return
    for txt in (principal, alerte):
        if txt:
            envoyer_html(txt, send)


def soir(send):
    try:
        txt = texte_soir()
    except Exception as e:
        print(f"Suivi handicaps (soir) ignoré : {type(e).__name__}: {e}")
        return
    if txt:
        envoyer_html(txt, send)


if __name__ == "__main__":
    print(texte_matin())
    print()
    print(texte_soir())
