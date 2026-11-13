# Volt AI installer for Windows 10/11 (no admin rights needed).
#
#   irm https://raw.githubusercontent.com/hossainsufian/volt-ai/main/scripts/install/install.ps1 | iex
#
# Installs Volt AI into %LOCALAPPDATA%\VoltAI with its own Python, the free
# local AI brain (Ollama + models), Start menu and desktop entries, the
# Ctrl+Alt+V on/off hotkey, the `volt` command (Volt Code), starts it with
# Windows (quietly), and lists it in Settings > Apps with an Uninstall button.
# Run it again any time to update. Based on Jarvis by isair (non-commercial licence).

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = 'Tls12'

$repo = 'hossainsufian/volt-ai'
$root = Join-Path $env:LOCALAPPDATA 'VoltAI'
$chatModel = 'qwen2.5:3b'
$embedModel = 'nomic-embed-text'

function Say($text) { Write-Host ''; Write-Host "Volt AI: $text" -ForegroundColor Cyan }
function Info($text) { Write-Host "   $text" }

if (-not [Environment]::Is64BitOperatingSystem) { throw 'Volt AI needs 64-bit Windows 10 or 11.' }
Say "Installing into $root"
New-Item -ItemType Directory "$root\app" -Force | Out-Null

# --- Python (via uv, a small, fast Python manager) ---------------------------------
$uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
if (-not (Test-Path $uv)) {
    Info 'Installing uv (Python manager)...'
    $env:UV_NO_MODIFY_PATH = '1'
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression | Out-Null
}
if (-not (Test-Path $uv)) { throw 'uv could not be installed. Check the internet connection and try again.' }
Info 'Getting Python 3.11...'
& $uv python install 3.11 2>$null | Out-Null
$python = Join-Path $root 'venv\Scripts\python.exe'
if (-not (Test-Path $python)) { & $uv venv --quiet --python 3.11 (Join-Path $root 'venv') }

# --- What Volt needs ----------------------------------------------------------------------
Info 'Downloading Volt AI...'
$tmp = Join-Path $env:TEMP 'volt-install'
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $tmp | Out-Null
Invoke-WebRequest "https://github.com/$repo/archive/refs/heads/main.zip" -OutFile "$tmp\volt.zip" -UseBasicParsing
Expand-Archive "$tmp\volt.zip" $tmp -Force
$new = Get-ChildItem $tmp -Directory | Select-Object -First 1 -ExpandProperty FullName
if (-not (Test-Path "$new\src\desktop_app")) { throw 'The download looks incomplete. Try again.' }
Info 'Installing what Volt needs (first time: several minutes)...'
& $uv pip install --quiet --python $python -r "$new\requirements-app.txt"
if ($LASTEXITCODE -ne 0) { throw 'Installing the Python packages failed. Check the internet connection and run this again.' }

# --- Free local AI brain (Ollama) ---------------------------------------------------------
$ollama = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
if (-not (Test-Path $ollama)) {
    Info 'Installing Ollama (the local AI engine)...'
    Invoke-WebRequest 'https://ollama.com/download/OllamaSetup.exe' -OutFile "$tmp\OllamaSetup.exe" -UseBasicParsing
    Start-Process "$tmp\OllamaSetup.exe" -ArgumentList '/VERYSILENT', '/NORESTART', '/SUPPRESSMSGBOXES' -Wait
}
if (-not (Test-Path $ollama)) { throw 'Ollama could not be installed. Install it from https://ollama.com/download and run this again.' }
$up = $false
try { Invoke-RestMethod 'http://127.0.0.1:11434/api/version' -TimeoutSec 3 | Out-Null; $up = $true } catch { }
if (-not $up) {
    Start-Process $ollama -ArgumentList 'serve' -WindowStyle Hidden
    for ($i = 0; $i -lt 30 -and -not $up; $i++) {
        Start-Sleep -Seconds 1
        try { Invoke-RestMethod 'http://127.0.0.1:11434/api/version' -TimeoutSec 2 | Out-Null; $up = $true } catch { }
    }
}
Info 'Downloading the AI models (about 2.3 GB the first time)...'
& $ollama pull $chatModel
& $ollama pull $embedModel

# --- Settings: start quietly with Windows; clicking the app opens the chat ---------------
& $python -c "import json,os; p=os.path.expanduser('~/.config/jarvis/config.json'); os.makedirs(os.path.dirname(p),exist_ok=True); c=json.load(open(p)) if os.path.exists(p) else {}; c.setdefault('ollama_chat_model','$chatModel'); json.dump(c,open(p,'w'),indent=2)"

# --- Files, Start menu, desktop, hotkey, volt command, Settings > Apps, start ------------
# The updater does all of this and starts Volt AI.
Invoke-Expression (Get-Content "$new\scripts\windows\update-volt.ps1" -Raw)
Copy-Item "$root\Start Volt AI.vbs" ([Environment]::GetFolderPath('Startup')) -Force
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

Say 'Installed. Open it from the Start menu: Volt AI.'
Info 'Volt Code: open a NEW PowerShell window and type  volt'
Info 'Ctrl+Alt+V turns Volt AI off and on. Run this line again any time to update.'
