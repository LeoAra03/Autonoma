#Requires -Version 5.1
BeforeAll { Import-Module (Join-Path $PSScriptRoot '../src/Autonoma.psm1') -Force }

Describe 'ConvertTo-GlobRegex' {
    It 'matches nested paths with **' {
        Test-Glob -Glob '**/*.js' -Path 'src/deep/a.js' | Should -BeTrue
    }
    It 'does not cross directories with *' {
        Test-Glob -Glob '*.js' -Path 'src/a.js' | Should -BeFalse
    }
    It 'expands braces' {
        Test-Glob -Glob '**/*.{js,ts}' -Path 'a.ts' | Should -BeTrue
    }
}

Describe 'Get-ReadySet' {
    BeforeAll { $phases = @([pscustomobject]@{ id = 'A'; axis = 'x'; depends = @() }, [pscustomobject]@{ id = 'B'; axis = 'y'; depends = @('A') }) }
    It 'excludes phases with unmet dependencies' {
        @(Get-ReadySet -Phases $phases -States @{}).Count | Should -Be 1
    }
    It 'releases after the dependency is green' {
        @(Get-ReadySet -Phases $phases -States @{ A = 'verde' }).Count | Should -Be 1
    }
    It 'blocks a busy lane' {
        @(Get-ReadySet -Phases $phases -States @{} -Lanes @{ x = $true }).Count | Should -Be 0
    }
}

Describe 'Test-GraphCycles' {
    It 'accepts an acyclic graph' {
        Test-GraphCycles -Phases @([pscustomobject]@{ id = 'A'; depends = @() }) | Should -BeNullOrEmpty
    }
    It 'detects a cycle' {
        Test-GraphCycles -Phases @([pscustomobject]@{ id = 'A'; depends = @('B') }, [pscustomobject]@{ id = 'B'; depends = @('A') }) | Should -Match 'cycle'
    }
    It 'detects an unknown dependency' {
        Test-GraphCycles -Phases @([pscustomobject]@{ id = 'A'; depends = @('Z') }) | Should -Match 'unknown'
    }
}
