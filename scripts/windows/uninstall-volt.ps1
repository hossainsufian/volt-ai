# Remove the Volt AI app from this Windows computer.
# Your chats, images, settings and keys are kept in your user folder
# (.local\share\volt-ai, .local\share\jarvis, .config\jarvis, .config\volt-ai),
# so installing Volt AI again brings everything back.

$ErrorActionPreference = 'Continue'
$root = Join-Path $env:LOCALAPPDATA 'VoltAI'

Add-Type -AssemblyName PresentationFramework
$answer = [System.Windows.MessageBox]::Show(
    "Remove Volt AI from this computer?`n`nYour chats, images and settings are kept.",
    'Uninstall Volt AI', 'YesNo', 'Question')
if ($answer -ne 'Yes') { exit }

# Stop Volt (the real processes run from uv's Python, so match what they run).
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cmd.exe'" | Where-Object {
    ($_.CommandLine -match '-m (desktop_app|jarvis\.main)') -or
    ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, [StringComparison]::OrdinalIgnoreCase))
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3

$desktop = [Environment]::GetFolderPath('Desktop')
$programs = [Environment]::GetFolderPath('Programs')
$startup = [Environment]::GetFolderPath('Startup')
Remove-Item (Join-Path $desktop 'Volt AI.lnk'), (Join-Path $desktop 'Update Volt AI.lnk'),
            (Join-Path $programs 'Volt AI.lnk'), (Join-Path $programs 'Update Volt AI.lnk'),
            (Join-Path $desktop 'Volt Code.lnk'), (Join-Path $programs 'Volt Code.lnk'),
            (Join-Path $programs 'Volt AI On-Off.lnk'),
            (Join-Path $startup 'Start Volt AI.vbs') -Force -ErrorAction SilentlyContinue
Remove-Item 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\VoltAI' -Recurse -Force -ErrorAction SilentlyContinue
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
[Environment]::SetEnvironmentVariable('Path', (($userPath -split ';') | Where-Object { $_ -and $_ -ne $root }) -join ';', 'User')
Remove-Item $root -Recurse -Force -ErrorAction SilentlyContinue

[System.Windows.MessageBox]::Show('Volt AI has been removed. Your chats and settings are still saved.',
    'Uninstall Volt AI') | Out-Null
