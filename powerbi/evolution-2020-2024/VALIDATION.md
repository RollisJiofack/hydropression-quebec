# Validation locale: 13 octobre 2026

## Script faisant autorite

`generated/deploy.tmdl`, fige pour la previsualisation navigateur.
SHA-256:

```text
6d37e881ddde887ded5922abac224c465064cc56337586e5212bbeeca317c6fb
```

Inventaire: 5 nouvelles tables, 3 nouvelles relations, 13 nouvelles mesures.
Aucune table, partition, colonne, mesure, expression ou relation existante
n'est une cible de remplacement. `DimStation.code` est uniquement reference
par le nouveau lien de correspondance. Aucune action navigateur, MCP modele,
service, commit ou push n'a ete effectuee.

## Controles executes

1. Lecture des 11 pages du PDF et inspection du schema de la page 5.
2. 21 tests `unittest`: tous passes. Correspondance exacte entre generateur et
   fichiers faisant autorite; empreinte; perimetre; sens des relations; URLs
   constantes/RelativePath/sans cache; types/nulls; garde annuelle; bornes KPI;
   part 100 uniquement annuelle nette; panel BLANK; libelle hors panel;
   formats et avertissements. Contrats CSV testes sur fixtures synthetiques:
   unicite, references, cles texte/composites, booleens, mois, denominateurs
   2020/2024 a 366 jours, incoherences du panel et nombres invalides.
3. Bibliotheques TOM 19.106.1.0 extraites en lecture seule du bundle installe
   dans l'extension Power BI Modeling MCP 0.4.0, sans executer le MCP.
4. `TmdlSerializer.DeserializeDatabaseFromFolder` a reussi sur les cinq
   definitions generees et trois relations. Un stub DimStation[code] exclusivement
   local resout la reference a la table existante; il n'est pas dans le script.
5. Controles objet TOM passes: inventaire, cardinalites Many->One, filtre
   OneDirection, relations actives, une partition Import par nouvelle table,
   treize mesures sous FactSiteAnnee.
6. Empreinte du script recontrolee apres validation: inchangee.

## Non valide

- Le wrapper `createOrReplace` est controle structurellement, pas par un parseur
  de commandes TOM; sa previsualisation navigateur est encore necessaire.
- Aucun DAX ou M n'a ete execute par un moteur. La deserialisation accepte
  les expressions comme metadonnees, sans prouver les resultats ni la syntaxe DAX/M.
- Les CSV reels n'etaient pas encore presents dans backend/data au dernier
  controle. Ni publication HTTP, ni refresh, ni credentials Web n'ont ete testes.
- L'unicite et le type des cles du DimStation distant ne sont pas verifies par
  le stub. La requete EXCEPT du fichier de recette doit renvoyer aucune ligne.
- La conservation des anciennes pressions doit etre comparee dans le vrai
  modele a la sauvegarde utilisateur. L'absence de cibles legacy est verifiee
  dans notre script, pas dans une application de celui-ci au service.
- Totaux mensuels backend, egalite avec n_sites_amont et non-regression des
  exports legacy restent sous la responsabilite du lot backend.

Les fichiers `generated/` n'ont pas ete modifies apres confirmation de leur
gel. Seuls les outils/documents de verification ont ete ajoutes ensuite.
