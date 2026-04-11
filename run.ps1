param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$RepoUrl,

    [Parameter(Position = 1)]
    [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
}

if (-not $pythonCommand) {
    Write-Error "Python 실행 파일을 찾을 수 없습니다. Python을 설치한 뒤 다시 시도하세요."
    exit 1
}

try {
    if ($pythonCommand.Name -eq "py") {
        & $pythonCommand.Source -3 app/main.py $RepoUrl --branch $Branch
    } else {
        & $pythonCommand.Source app/main.py $RepoUrl --branch $Branch
    }

    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
