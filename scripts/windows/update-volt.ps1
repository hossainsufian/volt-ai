# Update Volt AI on Windows to the latest version on GitHub.
# Run in PowerShell (no admin needed):
#   irm https://raw.githubusercontent.com/hossainsufian/volt-ai/main/scripts/windows/update-volt.ps1 | iex
# After the first run you can also double-click "Update Volt AI" on the desktop.
# Your chats, images, settings and keys are not touched.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = 'Tls12'

$root = Join-Path $env:LOCALAPPDATA 'VoltAI'
$python = Join-Path $root 'venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw "Volt AI is not installed in $root yet." }

$tmp = Join-Path $env:TEMP 'volt-update'
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $tmp | Out-Null

Write-Host 'Downloading the latest Volt AI...' -ForegroundColor Cyan
Invoke-WebRequest 'https://github.com/hossainsufian/volt-ai/archive/refs/heads/main.zip' -OutFile "$tmp\volt.zip" -UseBasicParsing
Expand-Archive "$tmp\volt.zip" $tmp -Force
$new = Join-Path $tmp 'volt-ai-main'
if (-not (Test-Path "$new\src\desktop_app")) { throw 'The download looks incomplete. Try again.' }

Write-Host 'Stopping Volt AI...' -ForegroundColor Cyan
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, [StringComparison]::OrdinalIgnoreCase) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3

Write-Host 'Installing the new version...' -ForegroundColor Cyan
$old = Join-Path $root 'app\src.old'
Remove-Item $old -Recurse -Force -ErrorAction SilentlyContinue
if (Test-Path "$root\app\src") { Move-Item "$root\app\src" $old }
Copy-Item "$new\src" "$root\app\src" -Recurse
Copy-Item "$new\requirements.txt", "$new\LICENSE", "$new\README.md" "$root\app" -Force
Copy-Item "$new\scripts\windows\run.cmd", "$new\scripts\windows\Start Volt AI.vbs", "$new\scripts\windows\update-volt.ps1" $root -Force

# Add new Python packages if any were added (quick when nothing changed).
$uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
if (Test-Path $uv) { & $uv pip install --quiet --python $python 'anthropic>=2.6.0' }

# Keep the chat window from popping up every time Windows starts.
& $python -c "import json,os; p=os.path.expanduser('~/.config/jarvis/config.json'); c=json.load(open(p)) if os.path.exists(p) else {}; c['open_chat_on_start']=False; os.makedirs(os.path.dirname(p),exist_ok=True); json.dump(c,open(p,'w'),indent=2)"

# Desktop shortcut for future updates.
$desktop = [Environment]::GetFolderPath('Desktop')
$link = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop 'Update Volt AI.lnk'))
$link.TargetPath = 'powershell.exe'
$link.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$root\update-volt.ps1`""
$link.WorkingDirectory = $root
$link.Save()

Write-Host 'Starting Volt AI...' -ForegroundColor Cyan
Start-Process wscript.exe -ArgumentList "`"$root\Start Volt AI.vbs`""
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $old -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ''
Write-Host 'Volt AI is updated and running in the background.' -ForegroundColor Green
Write-Host 'Open it from the Volt AI icon by the clock.'
