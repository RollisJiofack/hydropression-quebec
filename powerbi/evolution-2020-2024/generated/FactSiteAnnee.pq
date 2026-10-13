let
    Source = Csv.Document(
        Web.Contents("https://rollisjiofack.github.io/hydropression-quebec/", [RelativePath = "data/sites_annees.csv", Headers = [#"Cache-Control" = "no-cache", Pragma = "no-cache"]]),
        [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]
    ),
    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    Selected = Table.SelectColumns(Headers, {"num_site", "annee", "mois_declares", "volume_brut_m3", "debit_brut_annuel_m3s", "debit_brut_juilaout_m3s", "conso_nette_annuel_m3s", "conso_nette_juilaout_m3s", "conso_defaut100_annuel_m3s"}, MissingField.Error),
    Nulls = Table.ReplaceValue(Selected, "", null, Replacer.ReplaceValue, {"annee", "mois_declares", "volume_brut_m3", "debit_brut_annuel_m3s", "debit_brut_juilaout_m3s", "conso_nette_annuel_m3s", "conso_nette_juilaout_m3s", "conso_defaut100_annuel_m3s"}),
    Typed = Table.TransformColumnTypes(Nulls, {
        {"num_site", type text},
        {"annee", Int64.Type},
        {"mois_declares", Int64.Type},
        {"volume_brut_m3", type number},
        {"debit_brut_annuel_m3s", type number},
        {"debit_brut_juilaout_m3s", type number},
        {"conso_nette_annuel_m3s", type number},
        {"conso_nette_juilaout_m3s", type number},
        {"conso_defaut100_annuel_m3s", type number}
    }, "en-US")
in
    Typed
