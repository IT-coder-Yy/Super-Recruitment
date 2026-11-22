$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TargetEnv = Join-Path $ProjectRoot '.env'
if (-not (Test-Path -LiteralPath $TargetEnv)) {
    $SecretBytes = New-Object byte[] 32
    $SecretGenerator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $SecretGenerator.GetBytes($SecretBytes) } finally { $SecretGenerator.Dispose() }
    $SessionSecret = [Convert]::ToBase64String($SecretBytes)
    $Template = [System.IO.File]::ReadAllText((Join-Path $ProjectRoot '.env.example'))
    $Template = $Template.Replace('APP_SECRET_KEY=', "APP_SECRET_KEY=$SessionSecret")
    $PendingEnv = Join-Path $ProjectRoot ('.' + [guid]::NewGuid().ToString('N') + '.runtime.env')
    try {
        [System.IO.File]::WriteAllText($PendingEnv, $Template, (New-Object System.Text.UTF8Encoding($false)))
        try {
            [System.IO.File]::Move($PendingEnv, $TargetEnv)
        } catch [System.IO.IOException] {
            if (-not [System.IO.File]::Exists($TargetEnv)) { throw }
            $SessionSecret = $null
        }
    } finally {
        [System.IO.File]::Delete($PendingEnv)
    }
}
