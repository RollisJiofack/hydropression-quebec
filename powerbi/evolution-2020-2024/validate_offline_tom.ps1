# Offline TOM metadata parsing only. No Server, Connect, SaveChanges or refresh.
param(
    [string] $AssemblyDirectory = (Join-Path $PSScriptRoot '.offline-tom/assemblies'),
    [string] $ModelDirectory = (Join-Path $PSScriptRoot '.offline-tom/model')
)
$ErrorActionPreference = 'Stop'
foreach ($name in @('Newtonsoft.Json.dll', 'Microsoft.AnalysisServices.Runtime.Core.dll', 'Microsoft.AnalysisServices.Runtime.Windows.dll', 'Microsoft.AnalysisServices.Core.dll', 'Microsoft.AnalysisServices.dll', 'Microsoft.AnalysisServices.Tabular.dll', 'Microsoft.AnalysisServices.Tabular.Json.dll')) {
    $path = Join-Path $AssemblyDirectory $name
    if (Test-Path -LiteralPath $path) {
        [void][System.Reflection.Assembly]::LoadFrom($path)
    }
}
$db = [Microsoft.AnalysisServices.Tabular.TmdlSerializer]::DeserializeDatabaseFromFolder($ModelDirectory)
$expected = @('FactSiteAnnee', 'DimSite', 'CorrespondanceSiteStation', 'DimAnnee', 'Grandeur', 'DimStation')
$actual = @($db.Model.Tables | ForEach-Object Name)
if (@(Compare-Object $expected $actual).Count -ne 0) { throw 'Unexpected offline table inventory' }
if ($db.Model.Relationships.Count -ne 3) { throw 'Expected three relationships' }
$fact = $db.Model.Tables.Find('FactSiteAnnee')
if ($fact.Measures.Count -ne 13) { throw 'Expected thirteen measures' }
foreach ($relationship in $db.Model.Relationships) {
    if ($relationship.CrossFilteringBehavior.ToString() -ne 'OneDirection') { throw 'Unexpected filter direction' }
    if ($relationship.FromCardinality.ToString() -ne 'Many' -or $relationship.ToCardinality.ToString() -ne 'One') { throw 'Unexpected relationship cardinality' }
    if (-not $relationship.IsActive) { throw 'Inactive relationship' }
}
foreach ($name in $expected | Where-Object { $_ -ne 'DimStation' }) {
    $table = $db.Model.Tables.Find($name)
    if ($table.Partitions.Count -ne 1 -or $table.Partitions[0].Mode.ToString() -ne 'Import') { throw "Invalid Import partition: $name" }
}
[pscustomobject]@{
    Status = 'PASS: offline TMDL metadata deserialization and TOM graph checks'
    Tables = 5
    ReferenceStub = 'DimStation (offline only, not deployment target)'
    Relationships = $db.Model.Relationships.Count
    Measures = $fact.Measures.Count
    AssemblyVersion = [Microsoft.AnalysisServices.Tabular.TmdlSerializer].Assembly.GetName().Version.ToString()
    Limitation = 'Does NOT parse the createOrReplace command or execute DAX/M. No live model/service connection.'
} | ConvertTo-Json
