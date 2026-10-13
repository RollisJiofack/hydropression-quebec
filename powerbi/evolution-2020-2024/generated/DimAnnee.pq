let
    Source = FactSiteAnnee,
    Selected = Table.SelectColumns(Source, {"annee"}),
    Distinct = Table.Distinct(Selected),
    Typed = Table.TransformColumnTypes(Distinct, {{"annee", Int64.Type}}, "en-US"),
    Sorted = Table.Sort(Typed, {{"annee", Order.Ascending}})
in
    Sorted
