# Evolution 2020-2024: ajout Power BI

Livrable local uniquement. Aucune connexion a Power BI, aucun service/MCP,
aucun commit/push, aucune modification hors de ce dossier.

## Livrables

- `generated/deploy.tmdl`: script complet pour la vue TMDL du navigateur.
- `generated/*.pq`: cinq requetes M individuelles pour inspection ou import manuel.
- `generated/*.tmdl`: definitions individuelles et relations.
- `generated/manifest.json`: inventaire et SHA-256 du script faisant autorite.
- `generate.py`: generateur deterministe, Python standard uniquement.
- `test_generate.py`: tests structurels et du contrat CSV sur donnees synthetiques.
- `validation-examples.dax`: requetes a executer separement dans un vrai moteur apres import.
- `VALIDATION.md`: bilan exact des controles executes et des limites restantes.
- `prepare_offline_tom.py` et `validate_offline_tom.ps1`: validation TOM locale,
  sans lancement d'application ni connexion de modele.

Les fichiers `generated/` ont ete figes apres leur premiere generation pour la
previsualisation navigateur. Les fichiers de support n'en changent pas le contenu.

## Perimetre exact

Un seul `createOrReplace`, cible exclusivement cinq nouvelles tables et trois
nouvelles relations (UUID stables). Aucun `ref table`, `model`, `database`,
partition, mesure ou colonne d'une table existante n'est une cible du script.
La seule reference a `DimStation` est le cote dimension de la nouvelle relation.

| Table | Source / grain |
| --- | --- |
| FactSiteAnnee | `sites_annees.csv`, unique site x annee |
| DimSite | `sites.csv`, unique site |
| CorrespondanceSiteStation | `correspondance_sites_stations.csv`, unique station x site |
| DimAnnee | M, annees distinctes de FactSiteAnnee |
| Grandeur | M, quatre choix, deconnectee |

Trois relations actives, plusieurs-vers-un, filtre unique dimension vers faits:
FactSiteAnnee.num_site -> DimSite.num_site;
FactSiteAnnee.annee -> DimAnnee.annee;
CorrespondanceSiteStation.station -> DimStation.code.
Pas de relation entre correspondance et faits/sites. Les mesures transmettent
l'union distincte des sites avec `KEEPFILTERS(TREATAS(VALUES(...)))`.

Les trois CSV sont importes depuis la base constante
`https://rollisjiofack.github.io/hydropression-quebec/`, avec `RelativePath` sous
`data/`, UTF-8, virgule, types explicites et culture numerique `en-US`.
En-tetes HTTP `Cache-Control: no-cache` et `Pragma: no-cache`.
Aucune source `etat_pression.json` ni partition existante n'est remplacee.
Les cellules numeriques vides deviennent null, jamais zero; les cles restent
texte, y compris codes composites et zeros initiaux. Booleens: true/false ou 1/0.

## Mesures

Treize mesures hebergees uniquement dans FactSiteAnnee:
quatre debits de base, Valeur tous les sites, Valeur panel constant,
Contribution hors panel (m3/s), Sites declarants, Sites panel constant,
Evolution du panel (%), Part estimee a 100 %, Sites annees partielles,
Sous-titre evolution.

Les debits et valeurs exigent une seule annee. Sans site ou panel, BLANK est
conserve. Zero declare reste zero. Les comptes sont distincts et sans plafond.
Le KPI ignore toute la dimension DimAnnee, puis reapplique chaque borne avec
TREATAS; tous les autres filtres sont conserves. Une seule annee globale, borne
vide ou depart nul donne BLANK. Part estimee a 100 % existe seulement pour
index 3 (nette annuelle); brut et juillet/aout donnent BLANK.

Tous moins panel est une **contribution hors panel**, pas une mesure d'effet
de composition entre annees. Formats: debits `0.000`, comptes `0`, ratios `0.0%`.

Le sous-titre complet indique annees, perimetre non quebecois, panel absent,
annee civile 365/366 jours, sans imputation et nombre de sites avec annee
partielle, ou somme de maxima juillet/aout non necessairement simultanes.
Il avertit aussi sur les changements de methode d'estimation de consommation.
Pour un graphique contraint, conserver cette mesure dans une zone detaillee
sous le graphique et utiliser un titre court, sans changer la mesure:
`Evolution 2020-2024 | Sites amont distincts | Panel constant`.

## Generation et tests

Depuis ce dossier, avec Python 3:

```powershell
python -B -m unittest -v test_generate
python -B generate.py
python -B generate.py --csv-dir ../../backend/data
```

Le generateur ecrit exclusivement dans son propre `generated/`. Le parametre
`--csv-dir` ne fait que lire; une erreur de contrat arrete avant regeneration.
Il verifie colonnes, types, cles uniques, references sites, mois 1..12, donnees
non negatives/finies, denominateur annuel bissextile, composante 100 % <= nette,
continuite des annees et coherence exacte des attributs/panel DimSite.
Cela ne revalide pas l'agregation mensuelle du backend, les totaux amont source,
ni les cles du modele DimStation distant.

## Deploiement navigateur

1. Conserver la sauvegarde complete du modele existant. Verifier qu'aucune des
   cinq tables ni des trois UUID de relation n'existe deja avant le premier ajout.
2. Attendre la publication des trois CSV a leur URL Pages et valider leur contrat
   local. Verifier les codes, notamment composites, contre DimStation[code].
3. Coller `generated/deploy.tmdl` dans la vue TMDL et previsualiser. Les seules
   differences autorisees sont les cinq tables et trois relations nouvelles.
   Rejeter toute suppression/remplacement d'objet existant.
4. Appliquer seulement apres previsualisation correcte. Configurer la source Web
   anonyme et actualiser les nouvelles tables. Les en-tetes sans cache ne prouvent
   pas que les CSV sont publies ni que la source est effectivement rafraichie.
5. Segment Grandeur[libelle], trie par index, selection unique. La selection
   unique est une propriete du visuel, pas une contrainte TMDL. Index 1 est le
   defaut DAX, y compris si la selection est ambigue. Synchroniser le segment.
6. Recette moteur: quatre grandeurs; axe et segment annuel; station sans sites;
   station sans panel; station >20 sites; multi-stations partageant un site;
   panel <= tous; part 100 vide hors index 3; valeurs/pressions existantes identiques.

## Limites de validation

Les tests Python valident le generateur, sa structure et le contrat synthetique,
pas l'execution DAX ou M. Aucune connexion a un moteur ou au service n'a ete
effectuee. Le parsing TOM hors ligne a reussi (bibliotheque 19.106.1.0): cinq
tables, trois relations et treize mesures, avec un simple stub DimStation local
pour resoudre la reference. Ce parsing ne valide pas les resultats des mesures
ni le wrapper de commande createOrReplace. L'acceptation TMDL navigateur, le
rafraichissement CSV et la recette moteur restent les portes de deploiement.

Pour reproduire le controle TOM dans PowerShell 7, extraire les bibliotheques
existantes de l'installation modeling MCP, sans executer son programme:

```powershell
python -B prepare_offline_tom.py --bundle 'C:\Users\jioro01\.vscode\extensions\analysis-services.powerbi-modeling-mcp-0.4.0-win32-x64\server\powerbi-modeling-mcp.exe'
./validate_offline_tom.ps1
```

Les copies de bibliotheques et le modele de test sont confines a `.offline-tom/`.
Le stub DimStation n'est pas une sauvegarde du modele reel et ne doit jamais
etre deploye. Seul `generated/deploy.tmdl` est le script navigateur.

L'ajout futur d'une annee est detecte par DimAnnee, mais le nom technique du
flag `panel_2020_2024` demeure celui du protocole. Decider explicitement avec le
backend du panel futur (periode complete ou fenetre glissante) avant publication
de 2025; ne pas interpreter silencieusement un flag historique comme un nouveau
panel. Les pressions et l'agregation legacy restent hors perimetre.
