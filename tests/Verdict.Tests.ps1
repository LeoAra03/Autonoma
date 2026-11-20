#Requires -Version 5.1
BeforeAll { Import-Module (Join-Path $PSScriptRoot '../src/Autonoma.psm1') -Force }

$patterns = @{
    state   = '^\s*[*_`]*ESTADO[*_`]*\s*:?\s*(verde|rojo|bloqueado)\b'
    evidence = '^\s*[*_`]*EVIDENCIA[*_`]*\s*:?\s*(.+)$'
    gates   = '^\s*[*_`]*GATES[*_`]*\s*:?\s*(.+)$'
    done    = '^\s*[*_`]*HECHO[*_`]*\s*:?\s*(.+)$'
    clean   = '^\s*[*_`>]*SIN HALLAZGOS[*_`]*\s*$'
}

Describe 'Read-Verdict' {
    It 'reads a full contract' {
        $v = Read-Verdict -Text ('some work...' + "`n" + 'ESTADO: verde' + "`n" + 'EVIDENCIA: dist/build.log' + "`n" + 'GATES: rel-tests') -Patterns $patterns
        $v.state | Should -Be 'verde'
        $v.evidence | Should -Be 'dist/build.log'
        $v.gates | Should -Be 'rel-tests'
    }
    It 'takes the last state marker in the tail' {
        $v = Read-Verdict -Text ('ESTADO: rojo' + "`n" + 'ESTADO: verde') -Patterns $patterns
        $v.state | Should -Be 'verde'
    }
    It 'detects the clean line' {
        $v = Read-Verdict -Text 'SIN HALLAZGOS' -Patterns $patterns
        $v.clean | Should -BeTrue
    }
}

Describe 'Test-Acceptance' {
    It 'treats a green without evidence as red' {
        Test-Acceptance -Verdict @{ state = 'verde'; evidence = '' } | Should -Be 'rojo'
    }
    It 'accepts a green with evidence' {
        Test-Acceptance -Verdict @{ state = 'verde'; evidence = 'logs/x.log' } | Should -Be 'verde'
    }
    It 'passes blocked through' {
        Test-Acceptance -Verdict @{ state = 'bloqueado'; evidence = 'captcha unresolvable' } | Should -Be 'bloqueado'
    }
    It 'treats an empty verdict as red' {
        Test-Acceptance -Verdict @{ state = ''; evidence = '' } | Should -Be 'rojo'
    }
}
