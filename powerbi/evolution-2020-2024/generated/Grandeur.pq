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
