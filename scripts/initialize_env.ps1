$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TargetEnv = Join-Path $ProjectRoot '.env'
if (-not (Test-Path -LiteralPath $TargetEnv)) {
    $SecretBytes = New-Object byte[] 32
    $SecretGenerator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $SecretGenerator.GetBytes($SecretBytes) } finally { $SecretGenerator.Dispose() }
    $SessionSecret = [Convert]::ToBase64String($SecretBytes)
    $Template = [System.IO.File]::ReadAllText((Join-Path $ProjectRoot '.env.example'))
    $Template = $Template.Replace('APP_SECRET_KEY=', "APP_SECRET_KEY=$SessionSecret")
    [System.IO.File]::WriteAllText($TargetEnv, $Template, (New-Object System.Text.UTF8Encoding($false)))
}
