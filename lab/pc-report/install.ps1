# One-time setup for the lab, run by hand on the PC (each step is safe to run again):
#   1. a random token, saved in %USERPROFILE%\.lab-token and as the cluster Secret tools/lab-intake (never printed);
#   2. the lab restarted so it reads the token;
#   3. Homepage's old Ingress removed, so lab.machaka.net has only one owner (the lab);
#   4. the scheduled task "lab-report": report.ps1 every 10 minutes, hidden.
# Run:  powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\Documents\Coding\Infra\lab\pc-report\install.ps1"
$ErrorActionPreference = 'Stop'
$TokenFile = Join-Path $env:USERPROFILE '.lab-token'
$Script = Join-Path $PSScriptRoot 'report.ps1'

if (-not (Test-Path $TokenFile)) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    [IO.File]::WriteAllText($TokenFile, [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_'))
    Write-Host "1. token created in $TokenFile"
} else { Write-Host "1. token already in $TokenFile" }
$token = (Get-Content $TokenFile -Raw).Trim()
kubectl -n tools create secret generic lab-intake --from-literal=token=$token --dry-run=client -o yaml | kubectl apply -f - | Out-Null
Write-Host '   Secret tools/lab-intake set'

kubectl -n tools rollout restart deploy/lab | Out-Null
kubectl -n tools rollout status deploy/lab --timeout=120s | Out-Null
Write-Host '2. lab restarted with the token'

kubectl -n tools delete ingress homepage --ignore-not-found | Out-Null
Write-Host '3. Homepage Ingress removed (Homepage itself still runs: kubectl -n tools port-forward svc/homepage 3000:80)'

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Script`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 10)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'lab-report' -Force -Action $action -Trigger $trigger -Settings $settings `
    -Description 'Reports this PC''s scheduled jobs (counts only) to lab.machaka.net every 10 minutes.' | Out-Null
Write-Host '4. scheduled task lab-report registered (every 10 minutes)'

& powershell -NoProfile -ExecutionPolicy Bypass -File $Script
if ($LASTEXITCODE -eq 0) { Write-Host 'First report sent: see https://lab.machaka.net/pc' }
else { Write-Host "First report failed: see $env:LOCALAPPDATA\lab-report.log" }
