#!/usr/bin/env python3
"""
Envoie un message Telegram à partir des fichiers JSON déjà produits par le bot.

Usage :
    python scripts/notify_telegram.py morning   # meilleurs engagements du jour
    python scripts/notify_telegram.py bilan     # bilan du soir

Nécessite deux secrets GitHub (Settings > Secrets and variables > Actions) :
    TELEGRAM_BOT_TOKEN  : le token donné par @BotFather
    TELEGRAM_CHAT_ID    : l'identifiant de ta conversation avec le bot
Sans ces secrets, le script s'arrête proprement sans rien envoyer.
"""

import json
import os
import sys
from pathlib import Path

import requests

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MAX_LEN = 3800  # limite Telegram = 4096 caractères par message
MORNING_TOP_N = 20  # nombre de meilleurs engagements listés le matin


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
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": part, "disable_web_page_preview": "true"},
            timeout=15,
        )
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
        morning()
    elif mode == "bilan":
        bilan()
    else:
        print("Usage : notify_telegram.py morning|bilan", file=sys.stderr)


if __name__ == "__main__":
    main()
