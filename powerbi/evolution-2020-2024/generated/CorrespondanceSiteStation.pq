let
    Source = Csv.Document(
        Web.Contents("https://rollisjiofack.github.io/hydropression-quebec/", [RelativePath = "data/correspondance_sites_stations.csv", Headers = [#"Cache-Control" = "no-cache", Pragma = "no-cache"]]),
        [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]
    ),
    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
    Selected = Table.SelectColumns(Headers, {"station", "num_site", "en_etiage"}, MissingField.Error),
    Nulls = Table.ReplaceValue(Selected, "", null, Replacer.ReplaceValue, {"en_etiage"}),
    ParseBoolean = (v as any) as nullable logical =>
        if v = null then null else
        let s = Text.Lower(Text.Trim(Text.From(v, "en-US"))) in
            if List.Contains({"true", "1"}, s) then true
            else if List.Contains({"false", "0"}, s) then false
            else error Error.Record("BooleanContract", "Expected true/false or 1/0", v),
    Booleans = Table.TransformColumns(Nulls, {{"en_etiage", ParseBoolean, type logical}}),
    Typed = Table.TransformColumnTypes(Booleans, {
        {"station", type text},
        {"num_site", type text},
        {"en_etiage", type logical}
    }, "en-US")
in
    Typed
