# Turn Volt AI off, or back on if it's off. Bound to Ctrl+Alt+V through the
# "Volt AI On-Off" Start menu shortcut. Open Volt Code terminals are left alone.

$root = Join-Path $env:LOCALAPPDATA 'VoltAI'

function Get-VoltProcesses {
    # The real Volt processes run from uv's Python, so match what they run.
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cmd.exe'" | Where-Object {
        ($_.CommandLine -match '-m (desktop_app|jarvis\.main)') -or
        ($_.CommandLine -and $_.CommandLine.Contains("$root\run.cmd"))
    }
}

function Show-Notice($title, $text) {
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    $note = New-Object System.Windows.Forms.NotifyIcon
    $icon = "$root\volt-ai.ico"
    $note.Icon = if (Test-Path $icon) { New-Object System.Drawing.Icon $icon } else { [System.Drawing.SystemIcons]::Information }
    $note.Visible = $true
    $note.ShowBalloonTip(4000, $title, $text, 'Info')
    Start-Sleep -Seconds 5
    $note.Dispose()
}

if (Get-VoltProcesses | Where-Object { $_.CommandLine -match '-m desktop_app' }) {
    Get-VoltProcesses | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    for ($i = 0; $i -lt 15 -and (Get-VoltProcesses); $i++) { Start-Sleep -Seconds 1 }
    # A deliberate stop is not a crash: don't offer a crash report next time.
    Remove-Item (Join-Path $env:LOCALAPPDATA 'Jarvis\.crash_marker') -Force -ErrorAction SilentlyContinue
    # Free the memory the AI model was using (Ollama itself keeps running).
    try {
        foreach ($m in (Invoke-RestMethod 'http://127.0.0.1:11434/api/ps' -TimeoutSec 3).models) {
            $body = @{ model = $m.name; keep_alive = 0 } | ConvertTo-Json
            Invoke-RestMethod 'http://127.0.0.1:11434/api/generate' -Method Post -Body $body -TimeoutSec 10 | Out-Null
        }
    } catch { }
    Show-Notice 'Volt AI is off' 'Press Ctrl+Alt+V to turn it back on.'
} else {
    Start-Process wscript.exe -ArgumentList "`"$root\Start Volt AI.vbs`" --open-chat"
    Show-Notice 'Volt AI is starting' 'The chat opens in a moment. Press Ctrl+Alt+V again to turn it off.'
}
