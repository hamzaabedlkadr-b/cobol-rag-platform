$ErrorActionPreference = 'Stop'
$Python = if (Get-Command py -ErrorAction SilentlyContinue) { 'py' } else { 'python' }
& $Python "$PSScriptRoot/scripts/rag_machine.py" start @args
exit $LASTEXITCODE
