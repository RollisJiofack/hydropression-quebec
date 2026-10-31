# HydroPression : Rapport Power BI

Nouvelle base de travail approuvée par Rollis le 31 octobre 2026, issue de la
disposition Markdown revue et corrigée. Ouvrir `HydroPression.pbip` dans Power BI
Desktop (version Store utilisée pour ce projet).

## Contenu

Le projet contient six pages : Pression actuelle, Risque d'étiage, Fiche station,
Portrait et tendances, Priorisation et Méthodologie. Le rapport PBIR est dans
`HydroPression.Report`, son modèle TMDL dans `HydroPression.SemanticModel`.

Les seuils, les règles P1/P2, l'amplification, les coefficients de consommation,
les rattachements BV_PRIM + SUPERFI, les séries 2020–2024 et la validité des
débits à la génération restent inchangés. Les 65 expressions métier et les
11 relations du modèle de référence sont conservées.

## Sources Et Actualisation

Les sources sont publiques et en ligne, avec authentification anonyme :

- [État des stations](https://rollisjiofack.github.io/hydropression-quebec/data/etat_pression.json)
- [Sites](https://rollisjiofack.github.io/hydropression-quebec/data/sites.csv)
- [Volumes annuels](https://rollisjiofack.github.io/hydropression-quebec/data/sites_annees.csv)
- [Correspondances station–site](https://rollisjiofack.github.io/hydropression-quebec/data/correspondance_sites_stations.csv)

Ce projet n'utilise ni fichier de données local, ni instantané TEST figé.
Avant publication, charger et actualiser ce projet de production, puis contrôler
les horodatages et les indicateurs dans le moteur. Le cache local et les
identifiants d'authentification ne sont pas versionnés.

Le modèle utilise le statut de fraîcheur calculé lors de la génération, sans
requalifier la pression ou la priorité par l'heure de consultation. Les dates de
mesure CEHQ, de collecte, de génération et d'import sont des étapes distinctes.
La programmation GitHub prévoit deux déclenchements par heure ; cela ne garantit
pas cette cadence observée, ni celle des imports Power BI.

## Publication

La cible approuvée est le rapport **HydroPression** de Mon espace de travail,
identifiant `a32875f1-e960-4533-a8a4-f4dd9948aca7` :
[rapport cible](https://app.powerbi.com/groups/me/reports/a32875f1-e960-4533-a8a4-f4dd9948aca7).
Publier sous ce nom et vérifier le remplacement du bon rapport. Ne pas remplacer
`HydroPression_Quebec_Unifiee` (`61beff4e-98f8-4e14-9754-0b2a86c0d54f`).

La présence de ces sources dans GitHub ne publie pas automatiquement le rapport
dans Power BI. Un commit, une publication Desktop, un import de données et une
modification de page SharePoint sont des opérations distinctes. Le remplacement
en ligne doit être confirmé sur le rapport cible, pas seulement dans le dépôt.

## Points De Vigilance

Les trois stations signalées restent Matawin, Croche et Vermillon. Les profils
amont cumulés ne sont pas un total provincial dédupliqué. Q10,7 et les distances
hydrographiques amont ne sont pas disponibles et ne sont pas inventés. Les cartes
Bing historiques sont conservées ; leur migration vers Azure Maps est distincte.

Les contrôles de la base TEST ont validé 989 assertions, mais les nombres de
stations calculables et de priorités peuvent varier avec les nouvelles mesures.
Il faut les rapprocher de la génération importée, sans figer les résultats du TEST.
