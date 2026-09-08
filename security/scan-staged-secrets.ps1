$ErrorActionPreference = 'Stop'

function Get-StagedPath {
    git diff --cached --name-only --diff-filter=ACMR
}

# Secret patterns. $valueGroup[i] is the capture-group index of the secret
# token (0 = whole match). Patterns with no capture groups use whole match.
$patterns = @(
    '(?i)github_pat_[A-Za-z0-9_]{20,}',
    '(?i)gh[pousr]_[A-Za-z0-9_]{20,}',
    '(?i)sk-[A-Za-z0-9_-]{20,}',
    '(?i)AKIA[0-9A-Z]{16}',
    '(?i)(api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*["'']([^"'']{8,})["'']',
    '-----BEGIN (OPENSSH|RSA|EC|DSA|PRIVATE) KEY-----'
)
$valueGroup = @(1, 1, 1, 1, 2, 0)

# Tokens that look like documented examples/placeholders are ignored, so the
# scan stays usable on knowledge bases that show API-key examples. Real
# high-entropy secrets (random strings) do not match these markers.
$placeholder = '(?i)(<[^>]{1,80}>|your[-_ ][a-z0-9_-]{0,30}|example|placeholder|xxx+|abc123|\.\.\.)'

function Test-SecretLine {
    param([string]$Line)
    for ($i = 0; $i -lt $patterns.Count; $i++) {
        $m = [regex]::Match($Line, $patterns[$i])
        if (-not $m.Success) { continue }
        $g = $valueGroup[$i]
        $token = if ($g -gt 0 -and $m.Groups.Count -gt $g -and $m.Groups[$g].Success) {
            $m.Groups[$g].Value
        } else {
            $m.Value
        }
        if ($token -match $placeholder) { continue }
        return $true
    }
    return $false
}

$paths = @(Get-StagedPath)
if ($paths.Count -eq 0) {
    Write-Output 'secret-scan: no staged files'
    exit 0
}

$blockedNames = @(
    '(?i)(^|[\\/])\.env($|\.)',
    '(?i)\.(pem|key|p12|pfx)$',
    '(?i)(id_rsa|id_ed25519|authorized_keys)$'
)
$findings = New-Object System.Collections.Generic.List[string]

foreach ($path in $paths) {
    foreach ($namePattern in $blockedNames) {
        if ($path -match $namePattern) {
            $findings.Add("sensitive filename staged: $path")
        }
    }

    $content = git show ":$path" 2>$null
    if ($LASTEXITCODE -ne 0) { continue }
    foreach ($line in @($content -split "`r?`n")) {
        if (Test-SecretLine $line) {
            $findings.Add("secret-like content in staged file: $path")
            break
        }
    }
}

if ($findings.Count -gt 0) {
    Write-Error ('secret-scan: blocked commit`n' + (($findings | Sort-Object -Unique) -join "`n"))
    exit 1
}

Write-Output ("secret-scan: passed ({0} staged file(s))" -f $paths.Count)
exit 0