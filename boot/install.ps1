# Ugo - bootstrap Windows one-liner (nessun prerequisito):
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; iex ((New-Object Net.WebClient).DownloadString('https://raw.githubusercontent.com/giovannimazza/ugo-local-ai-agent/main/boot/install.ps1'))"
#
# PERCHE' QUESTA FORMA: con `irm ... | iex` su PowerShell 5.1 GitHub raw viene
# valutato RIGA PER RIGA (errori 'stringa vuota' e 'MissingEndCurlyBrace') e
# senza TLS 1.2 il download puo' tornare vuoto. WebClient.DownloadString
# ritorna UNA stringa intera e il TLS 1.2 esplicito evita i 403/vuoto.
#
# Trova (o installa via winget) Python 3.10+, poi passa la mano a
# boot/install.py che scarica il sorgente, crea il venv e lancia
# `ugo setup` + `ugo run`. NON richiede amministratore.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # in PS 5.1 la barra rallenta i download 10x
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}
$Raw = "https://raw.githubusercontent.com/giovannimazza/ugo-local-ai-agent/main"

Write-Host "==============================================" -ForegroundColor Cyan
Write-Host " Ugo - installazione automatica (Windows)"      -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan

# --- 1. trova un Python >= 3.10, altrimenti installalo via winget -----------
function Find-Python {
    $cands = @()
    foreach ($root in @("$env:LOCALAPPDATA", "$env:ProgramFiles")) {
        if ($root) {
            $cands += Get-ChildItem -Path $root -Filter "python.exe" -Recurse -Depth 3 -ErrorAction SilentlyContinue |
                      Where-Object { $_.FullName -match "Python31[0-9]" } |
                      Select-Object -ExpandProperty FullName
        }
    }
    foreach ($cmd in @("python", "py")) {
        $p = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($p) { $cands = @($p.Source) + $cands }
    }
    # preferenza al 3.12 (stabile): i primi della lista vincono
    foreach ($exe in $cands) {
        try {
            $v = & $exe -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($v -match '^3\.12') { return $exe }
        } catch {}
    }
    foreach ($exe in $cands) {
        try {
            $v = & $exe -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($v -match '^3\.(10|11|13|14)') { return $exe }
        } catch {}
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host "`n==> Python 3.10+ non trovato: lo installo con winget..." -ForegroundColor Yellow
    winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    $py = Find-Python
    if (-not $py) {
        # ultimo ricorso: installer ufficiale per l'utente corrente
        $inst = "$env:TEMP\python-3.12.8-amd64.exe"
        Invoke-WebRequest "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe" -OutFile $inst
        Start-Process -FilePath $inst -ArgumentList "/quiet","InstallAllUsers=0","PrependPath=1" -Wait
        $py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
        if (-not (Test-Path $py)) {
            Write-Host "Installazione Python non riuscita: installalo da python.org e rilancia." -ForegroundColor Red
            exit 1
        }
    }
}
Write-Host "    Python OK: $py"

# --- 2. scarica l'installer Python completo e lancialo ----------------------
$tmp = Join-Path $env:TEMP "ugo-install"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
Invoke-WebRequest "$Raw/boot/install.py" -OutFile "$tmp\install.py"
Write-Host "    installer scaricato in $tmp"

# --- 3. via libera all'installer Python (sorgente, venv, setup, run) --------
& $py "$tmp\install.py"
exit $LASTEXITCODE
