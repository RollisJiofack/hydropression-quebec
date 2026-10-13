"""Generate an additive, offline-only TMDL deployment. Python standard library only."""

import argparse
import calendar
import csv
import hashlib
import json
import math
from pathlib import Path
import re
from textwrap import dedent
import uuid


ROOT = Path(__file__).resolve().parent
BASE_URL = "https://rollisjiofack.github.io/hydropression-quebec/"
NAMESPACE = uuid.UUID("9802b187-a663-4594-a0fd-426ad4c0ca54")
TABLES = {
    "FactSiteAnnee": ("sites_annees.csv", [
        ("num_site", "string", "Identifiant texte du site; grain unique site x annee."),
        ("annee", "int64", "Annee civile de declaration; aucune ligne pour une annee sans declaration."),
        ("mois_declares", "int64", "Nombre de mois distincts declares, 1 a 12; moins de 12 signale une annee partielle."),
        ("volume_brut_m3", "double", "m3; somme des volumes des seuls mois declares, sans imputation."),
        ("debit_brut_annuel_m3s", "double", "m3/s; volume brut declare / (365 ou 366 jours x 86400); annee civile meme si partielle."),
        ("debit_brut_juilaout_m3s", "double", "m3/s; maximum des debits mensuels juillet/aout du site; null si aucun des deux mois declare."),
        ("conso_nette_annuel_m3s", "double", "m3/s; consommation estimee des mois declares / secondes de l'annee civile; sans imputation."),
        ("conso_nette_juilaout_m3s", "double", "m3/s; maximum juillet/aout de consommation estimee par site; null si aucun mois declare."),
        ("conso_defaut100_annuel_m3s", "double", "m3/s; composante annuelle de consommation estimee au niveau 4, hypothese 100 %."),
    ]),
    "DimSite": ("sites.csv", [
        ("num_site", "string", "Cle texte unique; conserve les zeros initiaux."),
        ("nom_intervenant", "string", "Nom de l'intervenant, protocole details_intervenants.csv."),
        ("municipalite", "string", "Municipalite declaree; texte nullable."),
        ("secteur_scian", "string", "Secteur SCIAN; texte nullable."),
        ("code_scian", "string", "Code SCIAN en texte; aucun calcul numerique."),
        ("premiere_annee", "int64", "Premiere annee declaree dans la periode exportee."),
        ("derniere_annee", "int64", "Derniere annee declaree dans la periode exportee."),
        ("nb_annees_declarees", "int64", "Nombre d'annees distinctes ayant au moins un mois declare."),
        ("panel_2020_2024", "boolean", "Vrai si le site declare chaque annee de la periode exportee; au moins un mois suffit, pas 12."),
        ("idtronc", "string", "Identifiant texte du troncon d'accrochage; aucune modification du rattachement."),
        ("bv_prim", "string", "Bassin primaire du site; texte nullable."),
    ]),
    "CorrespondanceSiteStation": ("correspondance_sites_stations.csv", [
        ("station", "string", "Code texte identique a DimStation[code], codes composites et zeros initiaux conserves."),
        ("num_site", "string", "Site en amont; grain unique station x site, sans plafond de 20 sites."),
        ("en_etiage", "boolean", "Indicateur de participation a la pression d'etiage existante; ne filtre pas les series annuelles."),
    ]),
    "DimAnnee": (None, [("annee", "int64", "Annees distinctes de FactSiteAnnee via M; pas de calendrier artificiel ni d'annee imputee.")]),
    "Grandeur": (None, [
        ("index", "int64", "1 brut annuel (defaut), 2 brut juillet/aout, 3 nette annuelle, 4 nette juillet/aout."),
        ("libelle", "string", "Libelle du segment a selection unique, trie par index; table deconnectee."),
    ]),
}
TABLE_DESCRIPTIONS = {
    "FactSiteAnnee": "Lot evolution uniquement. Grain site x annee; sommes declarees sans imputation. Source Surface, coordonnees et mois valides; exclusions et cascade de consommation gerees par le backend. Perimetre des mesures: sites distincts en amont d'au moins une station visible, pas un total quebecois. Null reste absent, zero declare reste zero.",
    "DimSite": "Lot evolution uniquement. Une ligne par site de prelevement; attributs et panel calcules sur la periode de l'export. Panel ne signifie pas douze mois par an.",
    "CorrespondanceSiteStation": "Lot evolution uniquement. Une ligne par couple station x site amont, BV_PRIM + SUPERFI existant. Seule DimStation filtre cette table; transfert aux faits explicite par TREATAS dans les mesures, sans relation aux sites/faits.",
    "DimAnnee": "Lot evolution uniquement. Dimension importee par M depuis les annees distinctes des faits. Les KPI utilisent les bornes globales en ignorant les filtres de cette dimension seulement.",
    "Grandeur": "Lot evolution uniquement. Quatre choix deconnectes. Configurer la selection unique du segment dans le rapport; SELECTEDVALUE utilise index 1 par defaut. Aucun filtre relationnel.",
}
RELATIONSHIPS = [
    ("FactSiteAnnee.num_site", "DimSite.num_site"),
    ("FactSiteAnnee.annee", "DimAnnee.annee"),
    ("CorrespondanceSiteStation.station", "DimStation.code"),
]


def clean(value):
    return dedent(value).strip()


def measure(expression, description, fmt="0.000"):
    return {"expression": clean(expression), "description": description, "format": fmt}


MEASURES = {}
for name, column in [
    ("Brut annuel (m3/s)", "debit_brut_annuel_m3s"),
    ("Brut juillet-aout (m3/s)", "debit_brut_juilaout_m3s"),
    ("Nette annuelle (m3/s)", "conso_nette_annuel_m3s"),
    ("Nette juillet-aout (m3/s)", "conso_nette_juilaout_m3s"),
]:
    MEASURES[name] = measure(f"""
        IF (
            HASONEVALUE ( DimAnnee[annee] ),
            CALCULATE (
                SUM ( FactSiteAnnee[{column}] ),
                KEEPFILTERS ( TREATAS ( VALUES ( CorrespondanceSiteStation[num_site] ), FactSiteAnnee[num_site] ) )
            )
        )
    """, "m3/s; somme sur sites distincts au grain site x annee, stations visibles et autres filtres conserves. Une seule annee obligatoire. Sans site/valeur: BLANK; aucune conversion en zero. Juillet/aout: somme de maxima par site non necessairement simultanes; annuel: annee civile 365/366 jours, mois non declares non imputes.")

MEASURES.update({
    "Valeur tous les sites": measure("""
        VAR g = SELECTEDVALUE ( Grandeur[index], 1 )
        RETURN
            IF (
                HASONEVALUE ( DimAnnee[annee] ),
                SWITCH (
                    g,
                    1, [Brut annuel (m3/s)],
                    2, [Brut juillet-aout (m3/s)],
                    3, [Nette annuelle (m3/s)],
                    4, [Nette juillet-aout (m3/s)]
                )
            )
    """, "m3/s; grandeur choisie (defaut index 1), union distincte des sites amont des stations visibles. BLANK si plusieurs annees ou aucun site/valeur."),
    "Valeur panel constant": measure("""
        CALCULATE (
            [Valeur tous les sites],
            KEEPFILTERS ( DimSite[panel_2020_2024] = TRUE () )
        )
    """, "m3/s; meme mesure limitee au panel declarant chaque annee. Filtres DimSite conserves. Panel absent = BLANK, jamais zero artificiel."),
    "Contribution hors panel (m3/s)": measure("""
        VAR t = [Valeur tous les sites]
        VAR p = [Valeur panel constant]
        RETURN IF ( NOT ISBLANK ( t ) && NOT ISBLANK ( p ), t - p )
    """, "m3/s; tous moins panel pour la meme annee. Contribution hors panel, pas une decomposition temporelle d'effet de composition. BLANK si une valeur manque."),
    "Sites declarants": measure("""
        IF (
            HASONEVALUE ( DimAnnee[annee] ),
            CALCULATE (
                DISTINCTCOUNT ( FactSiteAnnee[num_site] ),
                KEEPFILTERS ( TREATAS ( VALUES ( CorrespondanceSiteStation[num_site] ), FactSiteAnnee[num_site] ) ),
                KEEPFILTERS ( FactSiteAnnee[mois_declares] >= 1 )
            )
        )
    """, "Sites distincts avec au moins un mois declare dans l'annee visible, sans plafond de 20; BLANK si aucun site ou plusieurs annees.", "0"),
    "Sites panel constant": measure("""
        CALCULATE (
            DISTINCTCOUNT ( DimSite[num_site] ),
            KEEPFILTERS ( DimSite[panel_2020_2024] = TRUE () ),
            KEEPFILTERS ( TREATAS ( VALUES ( CorrespondanceSiteStation[num_site] ), DimSite[num_site] ) )
        )
    """, "Nombre de sites distincts du panel rattaches aux stations visibles et filtres DimSite. Effectif constant, independant de l'axe annuel; BLANK si panel absent.", "0"),
    "Evolution du panel (%)": measure("""
        VAR a0 = CALCULATE ( MIN ( DimAnnee[annee] ), REMOVEFILTERS ( DimAnnee ) )
        VAR a1 = CALCULATE ( MAX ( DimAnnee[annee] ), REMOVEFILTERS ( DimAnnee ) )
        VAR v0 = CALCULATE ( [Valeur panel constant], REMOVEFILTERS ( DimAnnee ), TREATAS ( { a0 }, DimAnnee[annee] ) )
        VAR v1 = CALCULATE ( [Valeur panel constant], REMOVEFILTERS ( DimAnnee ), TREATAS ( { a1 }, DimAnnee[annee] ) )
        RETURN
            IF (
                NOT ISBLANK ( a0 ) && NOT ISBLANK ( a1 ) && a0 < a1
                    && NOT ISBLANK ( v0 ) && NOT ISBLANK ( v1 ),
                DIVIDE ( v1 - v0, v0 )
            )
    """, "Ratio (derniere - premiere)/premiere du panel aux bornes globales des annees importees. Ignore tout filtre DimAnnee, y compris axe et segment, conserve tous les autres. BLANK si moins de deux annees, borne absente ou depart nul. Pas un test statistique.", "0.0%"),
    "Part estimee a 100 %": measure("""
        VAR g = SELECTEDVALUE ( Grandeur[index], 1 )
        VAR num =
            CALCULATE (
                SUM ( FactSiteAnnee[conso_defaut100_annuel_m3s] ),
                KEEPFILTERS ( TREATAS ( VALUES ( CorrespondanceSiteStation[num_site] ), FactSiteAnnee[num_site] ) )
            )
        RETURN
            IF ( g = 3 && HASONEVALUE ( DimAnnee[annee] ), DIVIDE ( num, [Nette annuelle (m3/s)] ) )
    """, "Ratio des sommes sur les memes sites/annee, consommation annuelle seulement (index 3). BLANK pour brut, juillet/aout, plusieurs annees, denominateur nul/absent. Numerateur manquant non impute.", "0.0%"),
    "Sites annees partielles": measure("""
        CALCULATE (
            DISTINCTCOUNT ( FactSiteAnnee[num_site] ),
            KEEPFILTERS ( TREATAS ( VALUES ( CorrespondanceSiteStation[num_site] ), FactSiteAnnee[num_site] ) ),
            KEEPFILTERS ( FactSiteAnnee[mois_declares] >= 1 ),
            KEEPFILTERS ( FactSiteAnnee[mois_declares] < 12 )
        )
    """, "Nombre de sites distincts avec au moins une annee declaree partielle (1 a 11 mois) dans les annees visibles; absence totale d'annee non assimilee a zero ni a une annee partielle. BLANK si aucun.", "0"),
    "Sous-titre evolution": measure("""
        VAR a0 = CALCULATE ( MIN ( DimAnnee[annee] ), REMOVEFILTERS ( DimAnnee ) )
        VAR a1 = CALCULATE ( MAX ( DimAnnee[annee] ), REMOVEFILTERS ( DimAnnee ) )
        VAR nSites =
            COALESCE (
                CALCULATE (
                    DISTINCTCOUNT ( DimSite[num_site] ),
                    KEEPFILTERS ( TREATAS ( VALUES ( CorrespondanceSiteStation[num_site] ), DimSite[num_site] ) )
                ), 0
            )
        VAR nPanel = COALESCE ( [Sites panel constant], 0 )
        VAR nPartiels = COALESCE ( CALCULATE ( [Sites annees partielles], REMOVEFILTERS ( DimAnnee ) ), 0 )
        VAR g = SELECTEDVALUE ( Grandeur[index], 1 )
        VAR periode = IF ( NOT ISBLANK ( a0 ), FORMAT ( a0, "0" ) & "-" & FORMAT ( a1, "0" ), "aucune annee disponible" )
        VAR avertissement =
            IF (
                g = 1 || g = 3,
                " | Moyenne sur l'annee civile (365/366 jours), somme des seuls mois declares, sans imputation; "
                    & FORMAT ( nPartiels, "0" ) & " sites avec au moins une annee partielle",
                " | Somme de maxima juillet-aout par site, non necessairement simultanes"
            )
        VAR conso = IF ( g >= 3, " | Consommation estimee: un changement de methode d'estimation peut creer des variations artificielles", "" )
        RETURN
            IF (
                nSites = 0,
                "Evolution " & periode & " | Aucun site de prelevement rattache a la selection",
                "Evolution " & periode & " | " & FORMAT ( nSites, "0" )
                    & " sites distincts en amont d'au moins une station de la selection (pas un total quebecois)"
                    & " | Panel constant: " & FORMAT ( nPanel, "0" ) & " sites declarants chaque annee"
                    & IF ( nPanel = 0, " (panel absent: valeurs vides)", "" )
                    & avertissement & conso
            )
    """, "Texte dynamique, annees globales importees, perimetre union des sites amont et panel. Annuel: avertissement annee civile/absence d'imputation et compte distinct de sites partiels sur toute la periode; juillet/aout: maxima non simultanes. Consommation: avertissement methode. Message explicite sans site.", None),
})


def m_source(table):
    filename, columns = TABLES[table]
    if table == "DimAnnee":
        return clean("""
            let
                Source = FactSiteAnnee,
                Selected = Table.SelectColumns(Source, {"annee"}),
                Distinct = Table.Distinct(Selected),
                Typed = Table.TransformColumnTypes(Distinct, {{"annee", Int64.Type}}, "en-US"),
                Sorted = Table.Sort(Typed, {{"annee", Order.Ascending}})
            in
                Sorted
        """)
    if table == "Grandeur":
        return clean("""
            let
                Source = #table(
                    type table [index = Int64.Type, libelle = text],
                    {
                        {1, "Prelevement brut, moyenne annuelle"},
                        {2, "Prelevement brut, maximum juillet-aout"},
                        {3, "Consommation nette estimee, moyenne annuelle"},
                        {4, "Consommation nette estimee, maximum juillet-aout"}
                    }
                )
            in
                Source
        """)
    names = ", ".join(json.dumps(c[0]) for c in columns)
    numeric = ", ".join(json.dumps(c[0]) for c in columns if c[1] != "string")
    types = {"string": "type text", "int64": "Int64.Type", "double": "type number", "boolean": "type logical"}
    type_pairs = ",\n        ".join("{" + json.dumps(n) + ", " + types[t] + "}" for n, t, _ in columns)
    booleans = [n for n, t, _ in columns if t == "boolean"]
    bool_step = ""
    previous = "Nulls"
    if booleans:
        transformations = ", ".join('{"' + n + '", ParseBoolean, type logical}' for n in booleans)
        bool_step = f"""
    ParseBoolean = (v as any) as nullable logical =>
        if v = null then null else
        let s = Text.Lower(Text.Trim(Text.From(v, "en-US"))) in
            if List.Contains({{"true", "1"}}, s) then true
            else if List.Contains({{"false", "0"}}, s) then false
            else error Error.Record("BooleanContract", "Expected true/false or 1/0", v),
    Booleans = Table.TransformColumns(Nulls, {{{transformations}}}),"""
        previous = "Booleans"
    return clean(f"""let
    Source = Csv.Document(
        Web.Contents("{BASE_URL}", [RelativePath = "data/{filename}", Headers = [#"Cache-Control" = "no-cache", Pragma = "no-cache"]]),
        [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]
    ),
    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    Selected = Table.SelectColumns(Headers, {{{names}}}, MissingField.Error),
    Nulls = Table.ReplaceValue(Selected, "", null, Replacer.ReplaceValue, {{{numeric}}}),{bool_step}
    Typed = Table.TransformColumnTypes({previous}, {{
        {type_pairs}
    }}, "en-US")
in
    Typed""")


def tmdl_table(table):
    lines = [f"/// {TABLE_DESCRIPTIONS[table]}", f"table {table}"]
    for name, dtype, description in TABLES[table][1]:
        lines += ["", f"\t/// {description}", f"\tcolumn {name}", f"\t\tdataType: {dtype}", "\t\tsummarizeBy: none", f"\t\tsourceColumn: {name}"]
        if (table == "DimSite" and name == "num_site") or table in ("DimAnnee", "Grandeur") and name in ("annee", "index"):
            lines.append("\t\tisKey")
        if dtype in ("int64", "double"):
            lines.append("\t\tformatString: " + ("0.000" if dtype == "double" else "0"))
        if table == "Grandeur" and name == "libelle":
            lines.append("\t\tsortByColumn: index")
    if table == "FactSiteAnnee":
        for name, spec in MEASURES.items():
            quoted = name.replace("'", "''")
            lines += ["", f"\t/// {spec['description']}", f"\tmeasure '{quoted}' ="]
            lines += ["\t\t\t" + line for line in spec["expression"].splitlines()]
            if spec["format"]:
                lines.append(f"\t\tformatString: {spec['format']}")
            lines.append("\t\tdisplayFolder: Evolution 2020-2024")
    lines += ["", f"\tpartition {table} = m", "\t\tmode: import", "\t\tsource ="]
    lines += ["\t\t\t" + line for line in m_source(table).splitlines()]
    return "\n".join(lines) + "\n"


def tmdl_relationships():
    blocks = []
    for source, target in RELATIONSHIPS:
        identifier = str(uuid.uuid5(NAMESPACE, source + "->" + target))
        blocks.append(f"""relationship {identifier}
\tfromColumn: {source}
\ttoColumn: {target}
\tfromCardinality: many
\ttoCardinality: one
\tcrossFilteringBehavior: oneDirection
\tisActive: true
""")
    return "\n".join(blocks)


def deployment_script():
    definitions = [tmdl_table(table) for table in TABLES] + [tmdl_relationships()]
    return "createOrReplace\n\n" + "\n".join(
        "\n".join("\t" + line if line else "" for line in item.rstrip().splitlines()) for item in definitions
    ) + "\n"


def validate_structure(script):
    tables = re.findall(r"^\ttable (\w+)$", script, re.M)
    if tables != list(TABLES):
        raise ValueError(f"Unexpected table scope: {tables}")
    if len(re.findall(r"^\trelationship ", script, re.M)) != 3:
        raise ValueError("Exactly three new relationships are required")
    if re.search(r"^\t(?:ref|model|database|expression|role|culture)\b", script, re.M):
        raise ValueError("Existing/model objects cannot be deployment targets")
    if script.count("createOrReplace") != 1 or "crossFilteringBehavior: bothDirections" in script:
        raise ValueError("Invalid deployment command/filter direction")
    if "table DimStation" in script or "partition DimStation" in script:
        raise ValueError("Existing DimStation must never be replaced")
    return {"tables": tables, "relationships": 3, "measures": len(MEASURES), "validation": "structural only; no DAX/M engine execution"}


def parse_bool(value):
    if value.strip().lower() in ("true", "1"):
        return True
    if value.strip().lower() in ("false", "0"):
        return False
    raise ValueError(f"Invalid boolean {value!r}")


def validate_csv_contract(directory):
    """Read only. Checks exported contract, not upstream monthly aggregation."""
    data = {}
    for table, (filename, columns) in TABLES.items():
        if not filename:
            continue
        with (Path(directory) / filename).open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {n for n, _, _ in columns}
            if not required.issubset(reader.fieldnames or []):
                raise ValueError(f"{filename}: missing columns {sorted(required - set(reader.fieldnames or []))}")
            rows = []
            for line, raw in enumerate(reader, 2):
                row = {}
                for name, dtype, _ in columns:
                    value = raw[name]
                    if value is None:
                        raise ValueError(f"{filename}:{line}: truncated row")
                    if dtype == "string":
                        row[name] = value
                    elif not value.strip():
                        row[name] = None
                    elif dtype == "boolean":
                        row[name] = parse_bool(value)
                    else:
                        number = float(value)
                        if not math.isfinite(number) or (dtype == "int64" and not number.is_integer()):
                            raise ValueError(f"{filename}:{line}: invalid {name}")
                        row[name] = int(number) if dtype == "int64" else number
                rows.append(row)
            data[table] = rows
    if not data["FactSiteAnnee"]:
        raise ValueError("FactSiteAnnee is empty; cannot verify year/panel contract")
    for table, keys in (("FactSiteAnnee", ("num_site", "annee")), ("DimSite", ("num_site",)), ("CorrespondanceSiteStation", ("station", "num_site"))):
        seen = set()
        for row in data[table]:
            key = tuple(row[k] for k in keys)
            if any(v is None or v == "" for v in key) or key in seen:
                raise ValueError(f"{table}: empty/duplicate key {key}")
            seen.add(key)
    sites = {r["num_site"]: r for r in data["DimSite"]}
    years = sorted({r["annee"] for r in data["FactSiteAnnee"]})
    if years != list(range(years[0], years[-1] + 1)):
        raise ValueError("Fact years must form a contiguous panel period")
    site_years = {}
    for row in data["FactSiteAnnee"]:
        site = row["num_site"]
        if site not in sites or not 1900 <= row["annee"] <= 9999:
            raise ValueError(f"Unknown site/invalid year {site}")
        if row["mois_declares"] is None or not 1 <= row["mois_declares"] <= 12:
            raise ValueError(f"Invalid mois_declares for {site}")
        for n, t, _ in TABLES["FactSiteAnnee"][1]:
            if t == "double" and row[n] is not None and row[n] < 0:
                raise ValueError(f"Negative {n} for {site}")
        volume, flow = row["volume_brut_m3"], row["debit_brut_annuel_m3s"]
        if volume is None or flow is None:
            raise ValueError(f"Missing annual volume/flow for {site}")
        seconds = (366 if calendar.isleap(row["annee"]) else 365) * 86400
        if not math.isclose(flow * seconds, volume, rel_tol=1e-8, abs_tol=0.01):
            raise ValueError(f"Annual calendar denominator mismatch for {site}/{row['annee']}")
        net, fallback = row["conso_nette_annuel_m3s"], row["conso_defaut100_annuel_m3s"]
        if net is not None and fallback is not None and fallback > net + 1e-10:
            raise ValueError(f"100% component exceeds net for {site}")
        site_years.setdefault(site, set()).add(row["annee"])
    for site, row in sites.items():
        declared = site_years.get(site, set())
        if not declared:
            raise ValueError(f"DimSite {site} has no fact declaration")
        expected = (min(declared), max(declared), len(declared), declared == set(years))
        actual = tuple(row[k] for k in ("premiere_annee", "derniere_annee", "nb_annees_declarees", "panel_2020_2024"))
        if actual != expected:
            raise ValueError(f"Panel/year attributes mismatch for {site}: {actual} != {expected}")
    for row in data["CorrespondanceSiteStation"]:
        if row["num_site"] not in sites or row["en_etiage"] is None:
            raise ValueError(f"Invalid correspondence {row}")
    return {"rows": {k: len(v) for k, v in data.items()}, "years": years, "validation": "exported CSV contract only; upstream aggregation and live DimStation keys not verified"}


def generate():
    script = deployment_script()
    report = validate_structure(script)
    generated = ROOT / "generated"
    generated.mkdir(exist_ok=True)
    for table in TABLES:
        (generated / f"{table}.tmdl").write_text(tmdl_table(table), encoding="utf-8", newline="\n")
        (generated / f"{table}.pq").write_text(m_source(table) + "\n", encoding="utf-8", newline="\n")
    (generated / "relationships.tmdl").write_text(tmdl_relationships(), encoding="utf-8", newline="\n")
    (generated / "deploy.tmdl").write_text(script, encoding="utf-8", newline="\n")
    report["deploy_sha256"] = hashlib.sha256(script.encode("utf-8")).hexdigest()
    (generated / "manifest.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv-dir", type=Path, help="Read-only validation of three ready CSV exports before generation")
    args = parser.parse_args()
    csv_report = validate_csv_contract(args.csv_dir) if args.csv_dir else None
    report = generate()
    if csv_report:
        report["csv_contract"] = csv_report
    print(json.dumps(report, indent=2))
    print(f"Browser TMDL script: {ROOT / 'generated' / 'deploy.tmdl'}")


if __name__ == "__main__":
    main()
