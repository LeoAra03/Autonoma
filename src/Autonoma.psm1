#Requires -Version 5.1
Set-StrictMode -Version 2.0

$script:Version = '1.0.0'
function Get-AutonomaVersion { return $script:Version }

# Dot-source every internal module so all functions share one module scope.
Get-ChildItem -Path (Join-Path $PSScriptRoot 'internal') -Filter '*.ps1' |
    Sort-Object Name | ForEach-Object { . $_.FullName }

Export-ModuleMember -Function *
