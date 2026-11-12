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
# The venv's python.exe is a launcher: the real Volt processes run from uv's
# own Python, so match them by what they run, not by where python.exe lives.
function Get-VoltProcesses {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cmd.exe'" | Where-Object {
        ($_.CommandLine -match '-m (desktop_app|jarvis\.main)') -or
        ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) -or
        ($_.CommandLine -and $_.CommandLine.Contains("$root\run.cmd"))
    }
}
Get-VoltProcesses | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
for ($i = 0; $i -lt 30 -and (Get-VoltProcesses); $i++) { Start-Sleep -Seconds 1 }

Write-Host 'Installing the new version...' -ForegroundColor Cyan
# Copy over the old files rather than renaming the folder: Windows often
# keeps a folder busy (Explorer, antivirus) and refuses the rename.
robocopy "$new\src" "$root\app\src" /MIR /NFL /NDL /NJH /NJS /NP /R:3 /W:2 /XD __pycache__ | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Some files could not be replaced (robocopy code $LASTEXITCODE). Close Volt AI and try again." }
Copy-Item "$new\requirements.txt", "$new\LICENSE", "$new\README.md" "$root\app" -Force
$win = "$new\scripts\windows"
Copy-Item "$win\run.cmd", "$win\Start Volt AI.vbs", "$win\update-volt.ps1", "$win\uninstall-volt.ps1", "$win\volt-ai.ico", "$win\volt.cmd" $root -Force

# Add new Python packages if any were added (quick when nothing changed).
$uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
if (Test-Path $uv) { & $uv pip install --quiet --python $python 'anthropic>=2.6.0' 'psutil>=5.9.0' }

# Keep the chat window from popping up every time Windows starts.
& $python -c "import json,os; p=os.path.expanduser('~/.config/jarvis/config.json'); c=json.load(open(p)) if os.path.exists(p) else {}; c['open_chat_on_start']=False; os.makedirs(os.path.dirname(p),exist_ok=True); json.dump(c,open(p,'w'),indent=2)"

# Make Volt AI a proper app: Start menu and desktop entries with its icon,
# and an entry in Settings > Apps (with Uninstall). No admin rights needed.
$icon = "$root\volt-ai.ico"
function New-VoltShortcut($path, $target, $arguments, $description) {
    $link = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
    $link.TargetPath = $target
    $link.Arguments = $arguments
    $link.WorkingDirectory = $root
    $link.IconLocation = "$icon,0"
    $link.Description = $description
    $link.Save()
}
$openArgs = "`"$root\Start Volt AI.vbs`" --open-chat"
$updateArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$root\update-volt.ps1`""
$desktop = [Environment]::GetFolderPath('Desktop')
$programs = [Environment]::GetFolderPath('Programs')
New-VoltShortcut (Join-Path $desktop 'Volt AI.lnk') 'wscript.exe' $openArgs 'Open Volt AI'
New-VoltShortcut (Join-Path $desktop 'Update Volt AI.lnk') 'powershell.exe' $updateArgs 'Update Volt AI'
New-VoltShortcut (Join-Path $programs 'Volt AI.lnk') 'wscript.exe' $openArgs 'Open Volt AI'
New-VoltShortcut (Join-Path $programs 'Update Volt AI.lnk') 'powershell.exe' $updateArgs 'Update Volt AI'

# The "volt" command (Volt Code) works in any new terminal window.
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($userPath -split ';') -notcontains $root) {
    [Environment]::SetEnvironmentVariable('Path', ($userPath.TrimEnd(';') + ';' + $root).TrimStart(';'), 'User')
}

$key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\VoltAI'
New-Item $key -Force | Out-Null
$entry = @{
    DisplayName = 'Volt AI'
    DisplayIcon = $icon
    DisplayVersion = (Get-Date -Format 'yyyy.M.d')
    Publisher = 'Sufian Hossain (based on Jarvis by isair)'
    InstallLocation = $root
    UninstallString = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$root\uninstall-volt.ps1`""
    URLInfoAbout = 'https://github.com/hossainsufian/volt-ai'
}
foreach ($name in $entry.Keys) { Set-ItemProperty $key $name $entry[$name] }
Set-ItemProperty $key NoModify 1 -Type DWord
Set-ItemProperty $key NoRepair 1 -Type DWord

Write-Host 'Starting Volt AI...' -ForegroundColor Cyan
Start-Process wscript.exe -ArgumentList "`"$root\Start Volt AI.vbs`""
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ''
Write-Host 'Volt AI is updated and running in the background.' -ForegroundColor Green
Write-Host 'Open it from the Volt AI icon by the clock.'
