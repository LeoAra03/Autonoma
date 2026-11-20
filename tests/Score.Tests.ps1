#Requires -Version 5.1
BeforeAll { Import-Module (Join-Path $PSScriptRoot '../src/Autonoma.psm1') -Force }

Describe 'Get-AxisScore' {
    It 'ignores skipped gates' {
        Get-AxisScore -Results @(
            [pscustomobject]@{ status = 'pass'; weight = 50 },
            [pscustomobject]@{ status = 'skipped'; weight = 50 }
        ) | Should -Be 100
    }
    It 'returns null with no scored gates' {
        Get-AxisScore -Results @([pscustomobject]@{ status = 'skipped'; weight = 10 }) | Should -BeNullOrEmpty
    }
}

Describe 'Test-Floors' {
    It 'reports the missing axis' {
        $card = [pscustomobject]@{ axes = @{ security = 40 } }
        (Test-Floors -Card $card -Floors @{ security = 70 }).Count | Should -Be 1
    }
    It 'passes when floors are met' {
        $card = [pscustomobject]@{ axes = @{ security = 80 } }
        (Test-Floors -Card $card -Floors @{ security = 70 }).Count | Should -Be 0
    }
}

Describe 'Compare-Scorecard' {
    It 'flags a regression' {
        $b = [pscustomobject]@{ global = 50; axes = @{ security = 60 } }
        $a = [pscustomobject]@{ global = 45; axes = @{ security = 40 } }
        (Compare-Scorecard -Before $b -After $a).regressed | Should -Contain 'security'
    }
}
