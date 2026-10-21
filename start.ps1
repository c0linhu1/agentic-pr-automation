# Starts Vader and the Slack bot, each in its own window. Close a window to stop that program.
# Usage:  .\start.ps1        (Ollama must already be running)
$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"

Start-Process powershell -WorkingDirectory $root -ArgumentList "-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle='Vader'; & '$py' -m vader.service"
Start-Sleep -Seconds 3   # let Vader start listening before the bot connects
Start-Process powershell -WorkingDirectory $root -ArgumentList "-NoExit", "-Command", "`$host.UI.RawUI.WindowTitle='Vader Slack bot'; & '$py' -m slack_bot.bot"
