#Requires -Version 5.1
BeforeAll { Import-Module (Join-Path $PSScriptRoot '../src/Autonoma.psm1') -Force }

Describe 'ConvertFrom-RoadmapLine' {
    It 'parses a well-formed line' {
        $p = ConvertFrom-RoadmapLine -Line 'SEC-01|security||PLAYBOOK|secrets|gitleaks gate passes'
        $p.id | Should -Be 'SEC-01'
        $p.axis | Should -Be 'security'
        $p.type | Should -Be 'PLAYBOOK'
        $p.title | Should -Be 'secrets'
        $p.criterion | Should -Be 'gitleaks gate passes'
    }
    It 'parses dependencies' {
        $p = ConvertFrom-RoadmapLine -Line 'SEC-02|security|SEC-01|PLAYBOOK|scan|deps green'
        @($p.depends) | Should -Contain 'SEC-01'
    }
    It 'skips blank and comment lines' {
        ConvertFrom-RoadmapLine -Line '' | Should -BeNullOrEmpty
        ConvertFrom-RoadmapLine -Line '# id|axis|depends|type|title|criterion' | Should -BeNullOrEmpty
    }
    It 'rejects malformed lines' {
        ConvertFrom-RoadmapLine -Line 'too|few|fields' | Should -BeNullOrEmpty
        ConvertFrom-RoadmapLine -Line 'bad id!|axis||type|t|c' | Should -BeNullOrEmpty
    }
}

Describe 'Read-Roadmap' {
    It 'reads the example plan' {
        $root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
        $phases = Read-Roadmap -Path (Join-Path $root 'plans/example/roadmap')
        $phases.Count | Should -BeGreaterThan 0
        Test-GraphCycles -Phases $phases | Should -BeNullOrEmpty
    }
}
