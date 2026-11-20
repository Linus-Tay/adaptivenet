# Windows PowerShell version of demo/run.sh (lab commands only). No AWS access is used.
#   powershell -ExecutionPolicy Bypass -File demo\run.ps1 lab-up | lab-down | lab-reset | lab-logs | status
# Needs Docker Desktop running and Python on PATH. Untested on Windows; the compose
# commands are the same ones demo/run.sh runs on macOS and Linux.
param([string]$Command = "lab-up")
$ErrorActionPreference = "Stop"
$Here = $PSScriptRoot
$Root = Split-Path $Here -Parent
$EnvFile = Join-Path $Here ".env.demo"   # ignored by git; holds only a random local lab token

if (-not (Test-Path $EnvFile)) {
    $bytes = New-Object byte[] 16
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    Set-Content -Path $EnvFile -Value "LAB_TOKEN=$token" -Encoding ascii -NoNewline
}
foreach ($d in "lab-live", "demo-models") {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root "data\$d") | Out-Null
}

function Lab { docker compose -f "$Root\compose.yaml" -f "$Here\compose.lab-overlay.yaml" --env-file $EnvFile @args }
function Fault {
    $env:PYTHONPATH = "$Root\services;$Root"
    Push-Location $Root; try { python -m demo.faultctl @args } finally { Pop-Location }
}

switch ($Command) {
    "lab-up" {
        Lab up --build -d
        Write-Host "Demo dashboard: http://127.0.0.1:3000   shop site: http://127.0.0.1:3001"
        Write-Host "Debug view of the decision pipeline: http://127.0.0.1:8192"
    }
    "lab-down"  { Lab down }
    "lab-logs"  { Lab logs -f --tail 50 }
    "lab-reset" {
        try { Fault lab-clear | Out-Null } catch { }
        Lab stop lab-controller
        Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $Root "data\lab-live\controller-journal.db")
        Lab restart gateway
        Lab start lab-controller
    }
    "status" { Fault status }
    default { Write-Host "usage: demo\run.ps1 lab-up|lab-down|lab-reset|lab-logs|status"; exit 2 }
}
