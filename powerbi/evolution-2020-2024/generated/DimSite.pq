let
    Source = Csv.Document(
        Web.Contents("https://rollisjiofack.github.io/hydropression-quebec/", [RelativePath = "data/sites.csv", Headers = [#"Cache-Control" = "no-cache", Pragma = "no-cache"]]),
        [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]
    ),
    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    Selected = Table.SelectColumns(Headers, {"num_site", "nom_intervenant", "municipalite", "secteur_scian", "code_scian", "premiere_annee", "derniere_annee", "nb_annees_declarees", "panel_2020_2024", "idtronc", "bv_prim"}, MissingField.Error),
    Nulls = Table.ReplaceValue(Selected, "", null, Replacer.ReplaceValue, {"premiere_annee", "derniere_annee", "nb_annees_declarees", "panel_2020_2024"}),
    ParseBoolean = (v as any) as nullable logical =>
        if v = null then null else
        let s = Text.Lower(Text.Trim(Text.From(v, "en-US"))) in
            if List.Contains({"true", "1"}, s) then true
            else if List.Contains({"false", "0"}, s) then false
            else error Error.Record("BooleanContract", "Expected true/false or 1/0", v),
    Booleans = Table.TransformColumns(Nulls, {{"panel_2020_2024", ParseBoolean, type logical}}),
    Typed = Table.TransformColumnTypes(Booleans, {
        {"num_site", type text},
        {"nom_intervenant", type text},
        {"municipalite", type text},
        {"secteur_scian", type text},
        {"code_scian", type text},
        {"premiere_annee", Int64.Type},
        {"derniere_annee", Int64.Type},
        {"nb_annees_declarees", Int64.Type},
        {"panel_2020_2024", type logical},
        {"idtronc", type text},
        {"bv_prim", type text}
    }, "en-US")
in
    Typed
