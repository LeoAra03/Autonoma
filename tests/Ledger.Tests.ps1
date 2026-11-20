#Requires -Version 5.1
BeforeAll {
    Import-Module (Join-Path $PSScriptRoot '../src/Autonoma.psm1') -Force
    $script:cfg = @{ stateDir = Join-Path ([IO.Path]::GetTempPath()) ('auto-' + [guid]::NewGuid()) }
    New-Item -ItemType Directory -Force -Path $script:cfg.stateDir | Out-Null
}
AfterAll { Remove-Item -LiteralPath $script:cfg.stateDir -Recurse -Force -ErrorAction SilentlyContinue }

Describe 'Add-LedgerEntry' {
    It 'chains entries' {
        Add-LedgerEntry -Config $script:cfg -Entry @{ kind = 'phase'; phase = 'A' }
        Add-LedgerEntry -Config $script:cfg -Entry @{ kind = 'phase'; phase = 'B' }
        Test-Ledger -Config $script:cfg | Should -BeTrue
    }
    It 'detects tampering' {
        $path = Join-Path $script:cfg.stateDir 'ledger.jsonl'
        $lines = Get-Content -LiteralPath $path
        $lines[0] = $lines[0].Replace('"phase":"A"', '"phase":"Z"')
        Set-Content -LiteralPath $path -Value $lines
        Test-Ledger -Config $script:cfg | Should -BeFalse
    }
}
