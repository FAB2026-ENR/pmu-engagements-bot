# Bot d'engagements PMU (galop)

Même principe que `crypto-alert-bot` : un script tourne gratuitement sur
les serveurs GitHub, interroge l'API PMU côté serveur (donc aucun
blocage CORS), et dépose un fichier JSON à jour dans ce dépôt.
`engagements-entraineurs.html` peut ensuite aller lire ce fichier
directement, sans copier-coller.

## Mise en place (une seule fois)

1. Crée un nouveau dépôt GitHub (public, c'est plus simple pour que le
   fichier JSON soit accessible librement en lecture) — par exemple
   `pmu-engagements-bot`.
2. Dépose tout le contenu de ce dossier à la racine du dépôt.
3. Va dans l'onglet **Settings → Actions → General** du dépôt et vérifie
   que "Workflow permissions" est sur **Read and write permissions**
   (nécessaire pour que le bot puisse pousser les fichiers JSON).
4. Va dans l'onglet **Actions**, le workflow "Récupération engagements
   PMU (galop)" doit apparaître. Lance-le une première fois à la main
   (bouton "Run workflow") pour vérifier que tout fonctionne.
5. Une fois que ça tourne, le fichier `data/latest.json` est mis à jour
   automatiquement toutes les 30 minutes entre 8h et 21h (heure de
   Paris).

## Vérifier que ça marche

Après une exécution réussie, ce fichier doit être accessible publiquement à :

```
https://raw.githubusercontent.com/<TON_PSEUDO>/<NOM_DU_DEPOT>/main/data/latest.json
```

Donne-moi cette URL une fois le dépôt créé — je branche le bouton
"import automatique" de l'outil HTML dessus.

## Limites connues

- Deux API PMU non officielles sont utilisées (`offline.turfinfo.api.pmu.fr`
  pour le programme/partants, `online.turfinfo.api.pmu.fr` pour l'historique
  des performances). Aucune des deux n'est officiellement documentée par le
  PMU. Elles sont stables depuis des années d'après la communauté turfiste,
  mais rien ne garantit qu'elles le restent — si le script commence à
  échouer d'un coup, c'est la première chose à vérifier.
- L'allocation de la **dernière course** de chaque cheval est maintenant
  récupérée automatiquement (endpoint `performances-detaillees`), mais la
  structure exacte de cette réponse n'a pas pu être vérifiée en conditions
  réelles avant la mise en place. Si `prixPrec` reste `null` pour tous les
  chevaux après la première exécution, regarde le fichier
  `data/latest.json` généré : s'il contient bien les données mais pas les
  allocations passées, il faut ajuster les noms de champs dans
  `extract_last_prize()` du script — dis-le-moi et je corrige.
- Seules les courses de **galop** sont gardées (Plat, Haies, Steeple,
  Cross) ; le trot (Attelé/Monté) est filtré.
- Le nombre d'appels réseau augmente avec cette fonctionnalité (un appel
  `performances-detaillees` par course, en plus du programme et des
  participants) — ça reste largement dans les limites gratuites de GitHub
  Actions, mais le workflow prendra un peu plus de temps à s'exécuter.
