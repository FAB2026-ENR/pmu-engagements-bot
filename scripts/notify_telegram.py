#!/usr/bin/env python3
"""
Envoie un message Telegram à partir des fichiers JSON déjà produits par le bot.

Usage :
    python scripts/notify_telegram.py morning   # le matin
    python scripts/notify_telegram.py bilan     # le soir

Le matin, deux messages :
  1. les chevaux dont la note de forme est comprise entre 0 et 0,99, parmi
     TOUS les partants du jour (pas seulement les entraîneurs à 2+ engagés),
     classés en deux groupes : réunions en France, puis à l'étranger ;
  2. les meilleurs engagements du jour (comme avant).

Le soir, trois messages :
  1. le résultat de ces mêmes chevaux notés 0 à 0,99 (arrivée, cotes,
     rentabilité d'une mise de 1 €), avec le cumul de la tranche depuis le
     début du suivi ;
  2. les croisements de cette tranche sur le cumul : la combinaison suivie
     (France et 5 courses ou plus), puis selon la cote, le nombre de courses
     courues, l'entraîneur et le pays ;
  3. le bilan général du jour (comme avant).

Nécessite deux secrets GitHub (Settings > Secrets and variables > Actions) :
    TELEGRAM_BOT_TOKEN  : le token donné par @BotFather
    TELEGRAM_CHAT_ID    : l'identifiant de ta conversation avec le bot
Sans ces secrets, le script s'arrête proprement sans rien envoyer.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

# Même calcul de note que le bot du matin et le bilan du soir.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_pmu import form_score  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MAX_LEN = 3800  # limite Telegram = 4096 caractères par message
MORNING_TOP_N = 20  # nombre de meilleurs engagements listés le matin

# Tranche de note suivie : de NOTE_MIN inclus à NOTE_MAX exclu, soit 0 à 0,99.
# C'est la tranche « 0 » du bilan. Pour suivre une autre tranche, changer ces
# deux valeurs (et CUMUL_LABEL, le nom de la tranche dans le cumul).
NOTE_MIN = 0
NOTE_MAX = 1
NOTE_LIBELLE = "0 à 0,99"
CUMUL_LABEL = "0"


def send(text: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Secrets Telegram absents : rien envoyé.", file=sys.stderr)
        return
    # Découpe en plusieurs messages si trop long, en coupant entre deux lignes.
    parts, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > MAX_LEN:
            parts.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        parts.append(current)
    for part in parts:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": part, "disable_web_page_preview": "true"},
                timeout=15,
            )
        except requests.RequestException as e:
            # Une panne de Telegram ne doit pas faire échouer le workflow :
            # sinon les données du jour ne seraient pas enregistrées. On
            # n'affiche que le type d'erreur (le détail contient le token).
            print(f"Échec Telegram ({type(e).__name__}) : message non envoyé.", file=sys.stderr)
            return
        if resp.status_code != 200:
            print(f"Échec Telegram HTTP {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
            return
    print(f"{len(parts)} message(s) Telegram envoyé(s).")


def tag_classe(shift):
    if not shift:
        return ""
    if shift.get("type") == "descend":
        return f" ↓{shift['pct']}%"
    if shift.get("type") == "monte":
        return f" ↑{shift['pct']}%"
    return ""


def fmt_cote(c):
    return "—" if c is None else f"{c:.2f}"


# --- Chevaux notés 0 à 0,99 ---

def fmt_note(score):
    return f"{score:.2f}".replace(".", ",")


def fmt_euro(c):
    return "—" if c is None else f"{c:.2f}".replace(".", ",")


def fmt_roi(valeur):
    return f"{valeur:+.1f}".replace(".", ",") + " %"


def dans_la_tranche(score):
    return score is not None and NOTE_MIN <= score < NOTE_MAX


def ordre_course(e):
    """Tri par réunion puis par course (R1C2 avant R1C10, avant R2C1)."""
    def num(txt):
        chiffres = "".join(ch for ch in str(txt or "") if ch.isdigit())
        return int(chiffres) if chiffres else 999
    return (num(e.get("reunion")), num(e.get("course")), e.get("cheval", ""))


def fmt_place(place):
    return "1er" if place == 1 else f"{place}e"


def pays_des_reunions(date_iso: str):
    """Renvoie {'R1': True, 'R7': False, ...} (True = réunion en France),
    lu dans le programme du PMU. Renvoie {} si c'est impossible : le message
    est alors envoyé sans séparation France / étranger."""
    try:
        from fetch_bilan import fetch_reunions_france
        return fetch_reunions_france(datetime.strptime(date_iso, "%Y-%m-%d").strftime("%d%m%Y"))
    except Exception as e:  # le message du matin doit partir quoi qu'il arrive
        print(f"Pays des réunions indisponible ({type(e).__name__}) : liste non séparée.", file=sys.stderr)
        return {}


def lignes_par_pays(retenus, pays, format_ligne):
    """Lignes du message, en deux groupes : France puis étranger."""
    if not pays:
        return [format_ligne(c) for c in retenus]
    lines = []
    groupes = [
        ("🇫🇷 France", [c for c in retenus if pays.get(c.get("reunion")) is True]),
        ("🌍 Étranger", [c for c in retenus if pays.get(c.get("reunion")) is False]),
    ]
    inconnus = [c for c in retenus if pays.get(c.get("reunion")) not in (True, False)]
    if inconnus:
        groupes.append(("Pays inconnu", inconnus))
    for titre, groupe in groupes:
        if lines:
            lines.append("")
        lines.append(f"{titre} ({len(groupe)})")
        lines += [format_ligne(c) for c in groupe] or ["aucun"]
    return lines


def selection_matin():
    """Message du matin : tous les partants du jour notés 0 à 0,99."""
    path = DATA_DIR / "latest.json"
    if not path.exists():
        print("latest.json introuvable.", file=sys.stderr)
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("entries", [])

    retenus = []
    for e in entries:
        score, shift = form_score(e.get("musique", ""), e.get("prixJour"), e.get("prixPrec"))
        if dans_la_tranche(score):
            retenus.append({**e, "score": score, "classShift": shift})
    retenus.sort(key=ordre_course)

    lines = [f"🎯 Chevaux notés {NOTE_LIBELLE} — {data.get('date', '')}"]
    if not retenus:
        lines.append(f"Aucun cheval dans cette tranche sur {len(entries)} partants.")
    else:
        mot = "chevaux" if len(retenus) > 1 else "cheval"
        lines.append(f"{len(retenus)} {mot} sur {len(entries)} partants (tous entraîneurs confondus)")
        lines.append("")

        def format_ligne(c):
            return (
                f"• {c.get('reunion', '')}{c.get('course', '')} — {c['cheval']} — "
                f"note {fmt_note(c['score'])}{tag_classe(c.get('classShift'))} — {c.get('entraineur', '')}"
            )

        lines += lignes_par_pays(retenus, pays_des_reunions(data.get("date", "")), format_ligne)
        lines.append("")
        lines.append("Résultat de ces chevaux ce soir, avec le bilan.")
    send("\n".join(lines))


def selection_soir():
    """Message du soir : arrivée et cotes des chevaux notés 0 à 0,99."""
    path = DATA_DIR / "bilan-latest.json"
    if not path.exists():
        print("bilan-latest.json introuvable.", file=sys.stderr)
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    retenus = [e for e in data.get("entries", []) if dans_la_tranche(e.get("score"))]
    retenus.sort(key=ordre_course)

    lines = [f"🎯 Résultat des chevaux notés {NOTE_LIBELLE} — {data.get('date', '')}"]
    if not retenus:
        lines.append("Aucun cheval dans cette tranche aujourd'hui.")
        send("\n".join(lines))
        return

    # Même calcul que le bilan : la rentabilité ne porte que sur les chevaux
    # arrivés dont le PMU a publié les cotes.
    arrives = [e for e in retenus if e.get("arrivee") is not None]
    gagnants = sum(1 for e in arrives if e["arrivee"] == 1)
    places = sum(1 for e in arrives if e["arrivee"] <= 3)
    avec_cotes = [e for e in arrives if e.get("rapportsPublies", True)]
    n_roi = len(avec_cotes)

    lines.append(f"{len(arrives)} arrivés sur {len(retenus)} : {gagnants} gagnant(s), {places} placé(s)")
    if n_roi:
        retour_g = sum((e.get("coteGagnant") or 0) for e in avec_cotes if e["arrivee"] == 1)
        retour_p = sum((e.get("cotePlace") or 0) for e in avec_cotes if e["arrivee"] <= 3)
        lines.append(
            f"Mise de 1 € par cheval : gagnant {fmt_roi(100 * (retour_g - n_roi) / n_roi)} / "
            f"placé {fmt_roi(100 * (retour_p - n_roi) / n_roi)} (sur {n_roi} avec cotes publiées)"
        )
    elif arrives:
        lines.append("Rentabilité non calculable : cotes non publiées par le PMU.")
    lines.append("")

    for e in retenus:
        debut = f"• {e.get('reunion', '')}{e.get('course', '')} — {e['cheval']} ({fmt_note(e['score'])}) : "
        place = e.get("arrivee")
        if place is None:
            statut = e.get("statutArrivee")
            fin = f"pas d'arrivée ({statut})" if statut else "pas d'arrivée connue"
        elif not e.get("rapportsPublies", True):
            fin = f"{fmt_place(place)} — cotes non publiées"
        elif place == 1:
            fin = f"1er — gagnant {fmt_euro(e.get('coteGagnant'))} / placé {fmt_euro(e.get('cotePlace'))}"
        elif place <= 3:
            fin = f"{fmt_place(place)} — placé {fmt_euro(e.get('cotePlace'))}"
        else:
            fin = fmt_place(place)
        lines.append(debut + fin)

    # Cumul de la tranche depuis le début du suivi, pour voir si elle tient.
    cumul_path = DATA_DIR / "bilan-cumul.json"
    if cumul_path.exists():
        try:
            cumul = json.loads(cumul_path.read_text(encoding="utf-8"))
        except ValueError:
            cumul = {}
        tranche = next((b for b in cumul.get("buckets", []) if b.get("label") == CUMUL_LABEL), None)
        if tranche and tranche.get("n"):
            nb_jours = sum(1 for j in cumul.get("jours", []) if j.get("avecArrivee"))
            roi_g = "—" if tranche.get("roiGagnant") is None else fmt_roi(tranche["roiGagnant"])
            roi_p = "—" if tranche.get("roiPlace") is None else fmt_roi(tranche["roiPlace"])
            lines.append("")
            lines.append(
                f"Cumul sur {nb_jours} jour(s) : {tranche['n']} partants, {tranche['nGagnants']} gagnants, "
                f"{tranche['nPlaces']} placés — gagnant {roi_g} / placé {roi_p} (sur {tranche.get('nRoi', 0)} avec cotes)"
            )
    send("\n".join(lines))


def croisements_soir():
    """Message du soir : la tranche 0 à 0,99 découpée selon quatre critères,
    sur le cumul depuis le début du suivi."""
    path = DATA_DIR / "bilan-cumul.json"
    if not path.exists():
        print("bilan-cumul.json introuvable : pas de croisements.", file=sys.stderr)
        return
    try:
        cumul = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        print("bilan-cumul.json illisible : pas de croisements.", file=sys.stderr)
        return
    croisements = cumul.get("croisements")
    if not croisements:
        print("Pas de croisements dans le cumul (ancienne version du bilan).", file=sys.stderr)
        return

    nb_jours = sum(1 for j in cumul.get("jours", []) if j.get("avecArrivee"))
    lines = [
        f"🔀 Croisements — chevaux notés {NOTE_LIBELLE}",
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
            roi_g = "—" if g.get("roiGagnant") is None else fmt_roi(g["roiGagnant"])
            roi_p = "—" if g.get("roiPlace") is None else fmt_roi(g["roiPlace"])
            mot = "partant" if g["n"] == 1 else "partants"
            lines.append(
                f"• {g['label']} : {g['n']} {mot}, {g['nGagnants']} g, {g['nPlaces']} p → {roi_g} / {roi_p}"
            )

    bloc("Combinaison suivie", croisements.get("combinaison"))
    bloc("Selon la cote du cheval", croisements.get("cote"))
    bloc("Selon le nombre de courses dans la musique", croisements.get("nbCourses"))
    bloc("Selon l'entraîneur", croisements.get("engagement"))
    bloc("Selon le pays de la réunion", croisements.get("pays"))
    send("\n".join(lines))


def morning():
    path = DATA_DIR / "latest.json"
    if not path.exists():
        print("latest.json introuvable.", file=sys.stderr)
        return
    data = json.loads(path.read_text(encoding="utf-8"))

    best = []
    for bloc in data.get("bestEngagements", []):
        for c in bloc["chevaux"]:
            if c.get("meilleurEngagement") and c.get("score") is not None:
                best.append({**c, "nb": len(bloc["chevaux"])})
    best.sort(key=lambda c: c["score"])

    lines = [f"🏇 Meilleurs engagements du {data.get('date', '')}", ""]
    if not best:
        lines.append("Aucun entraîneur avec 2 chevaux ou plus engagés aujourd'hui.")
    else:
        lines.append(f"Top {min(MORNING_TOP_N, len(best))} sur {len(best)} entraîneurs (score bas = meilleure forme) :")
        lines.append("")
        for c in best[:MORNING_TOP_N]:
            lines.append(
                f"• {c['cheval']} — {c['reunion']}{c['course']} — {c['entraineur']} "
                f"({c['nb']} engagés) — score {c['score']}{tag_classe(c.get('classShift'))}"
            )
        descendants = [c for c in best if (c.get("classShift") or {}).get("type") == "descend"]
        descendants = [c for c in descendants if c not in best[:MORNING_TOP_N]]
        if descendants:
            lines.append("")
            lines.append("Autres meilleurs engagements déclassés (↓) :")
            for c in descendants[:10]:
                lines.append(
                    f"• {c['cheval']} — {c['reunion']}{c['course']} — {c['entraineur']} "
                    f"— score {c['score']}{tag_classe(c.get('classShift'))}"
                )
    send("\n".join(lines))


def bilan():
    path = DATA_DIR / "bilan-latest.json"
    if not path.exists():
        print("bilan-latest.json introuvable.", file=sys.stderr)
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("entries", [])
    avec_arrivee = sum(1 for e in entries if e.get("arrivee") is not None)

    lines = [f"📊 Bilan du {data.get('date', '')}", f"{avec_arrivee}/{len(entries)} partants avec une arrivée connue", ""]

    def bloc(titre, buckets):
        rows = [b for b in (buckets or []) if b.get("n")]
        if not rows:
            return
        lines.append(titre)
        for b in rows:
            roi_g = "—" if b.get("roiGagnant") is None else f"{b['roiGagnant']:+.0f}%"
            roi_p = "—" if b.get("roiPlace") is None else f"{b['roiPlace']:+.0f}%"
            lines.append(
                f"  {b['label']} : {b['n']} partants, {b['nGagnants']} gagnants, {b['nPlaces']} placés "
                f"| cote moy. {fmt_cote(b.get('coteGagnantMoyenne'))} | rentab. G {roi_g} / P {roi_p}"
            )
        lines.append("")

    bloc("Par score de forme (mise 1€ par cheval) :", data.get("buckets"))
    cs = data.get("classShiftBuckets") or {}
    bloc("Déclassés ↓ (descendent en classe) :", cs.get("descend"))
    bloc("Montent en classe ↑ (comparaison) :", cs.get("monte"))

    declasses = [
        e for e in entries
        if (e.get("classShift") or {}).get("type") == "descend"
        and e.get("arrivee") is not None and e["arrivee"] <= 3
    ]
    declasses.sort(key=lambda e: e["arrivee"])
    if declasses:
        lines.append("Déclassés ↓ arrivés dans les 3 premiers :")
        for e in declasses[:20]:
            lines.append(
                f"  {e['arrivee']}. {e['cheval']} ({e['reunion']}{e['course']}) {tag_classe(e.get('classShift'))} "
                f"— gagnant {fmt_cote(e.get('coteGagnant'))} / placé {fmt_cote(e.get('cotePlace'))}"
            )
    else:
        lines.append("Aucun déclassé ↓ dans les 3 premiers aujourd'hui.")
    send("\n".join(lines))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "morning":
        selection_matin()
        morning()
    elif mode == "bilan":
        selection_soir()
        croisements_soir()
        bilan()
    else:
        print("Usage : notify_telegram.py morning|bilan", file=sys.stderr)


if __name__ == "__main__":
    main()
