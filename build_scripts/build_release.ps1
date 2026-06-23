[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version,

    [string]$Python = 'python',

    [switch]$SkipValidation
)

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$distRoot = Join-Path $projectRoot 'dist'
$buildRoot = Join-Path $projectRoot 'build'
$releaseRoot = Join-Path $distRoot 'release'
$packageName = "tickets_hunter_v$Version"
$packageRoot = Join-Path $releaseRoot $packageName
$zipPath = Join-Path $releaseRoot "${packageName}_windows_x64.zip"
$checksumPath = "$zipPath.sha256"

function Assert-ProjectPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $absolute = [System.IO.Path]::GetFullPath($Path)
    if (-not $absolute.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the project: $absolute"
    }
    return $absolute
}

function Remove-ProjectItem {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (Test-Path -LiteralPath $Path) {
        $absolute = Assert-ProjectPath $Path
        Remove-Item -LiteralPath $absolute -Recurse -Force
    }
}

function Invoke-Python {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE`: $($Arguments -join ' ')"
    }
}

function New-ReleaseArchive {
    param(
        [Parameter(Mandatory = $true)][string]$SourceDirectory,
        [Parameter(Mandatory = $true)][string]$DestinationPath,
        [int]$MaxAttempts = 6
    )

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            if (Test-Path -LiteralPath $DestinationPath) {
                Remove-Item -LiteralPath $DestinationPath -Force
            }
            Compress-Archive `
                -Path (Join-Path $SourceDirectory '*') `
                -DestinationPath $DestinationPath `
                -CompressionLevel Optimal `
                -Force `
                -ErrorAction Stop
            return
        }
        catch {
            if ($attempt -eq $MaxAttempts) {
                throw
            }
            $delaySeconds = [Math]::Min($attempt * 3, 15)
            Write-Warning "Archive attempt $attempt failed; retrying in $delaySeconds seconds. $($_.Exception.Message)"
            Start-Sleep -Seconds $delaySeconds
        }
    }
}

Set-Location $projectRoot

if (-not $SkipValidation) {
    Invoke-Python '-W' 'error::SyntaxWarning' '-m' 'compileall' '-q' 'src' 'tests'
    & ruff check src tests
    if ($LASTEXITCODE -ne 0) {
        throw "Ruff validation failed with exit code $LASTEXITCODE"
    }
    Invoke-Python '-m' 'pytest'
}

Remove-ProjectItem $buildRoot
Remove-ProjectItem $distRoot
New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null

Invoke-Python '-m' 'PyInstaller' '--clean' '--noconfirm' 'build_scripts\nodriver_tixcraft.spec'
Remove-ProjectItem $buildRoot
Invoke-Python '-m' 'PyInstaller' '--clean' '--noconfirm' 'build_scripts\settings.spec'

New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $packageRoot '_internal') -Force | Out-Null

Copy-Item -LiteralPath (Join-Path $distRoot 'nodriver_tixcraft\nodriver_tixcraft.exe') -Destination $packageRoot
Copy-Item -LiteralPath (Join-Path $distRoot 'settings\settings.exe') -Destination $packageRoot

Copy-Item -Path (Join-Path $distRoot 'nodriver_tixcraft\_internal\*') `
    -Destination (Join-Path $packageRoot '_internal') -Recurse -Force
Copy-Item -Path (Join-Path $distRoot 'settings\_internal\*') `
    -Destination (Join-Path $packageRoot '_internal') -Recurse -Force

Copy-Item -LiteralPath (Join-Path $projectRoot 'src\assets') -Destination $packageRoot -Recurse
Copy-Item -LiteralPath (Join-Path $projectRoot 'src\www') -Destination $packageRoot -Recurse
Copy-Item -LiteralPath (Join-Path $projectRoot 'CHANGELOG.md') -Destination (Join-Path $packageRoot 'CHANGELOG.md')
Copy-Item -LiteralPath (Join-Path $projectRoot 'ENTERPRISE_REFACTOR_REPORT.md') `
    -Destination (Join-Path $packageRoot 'ENTERPRISE_REFACTOR_REPORT.md')
Copy-Item -LiteralPath (Join-Path $projectRoot 'LEGAL_NOTICE.md') -Destination (Join-Path $packageRoot 'LEGAL_NOTICE.md')
Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination (Join-Path $packageRoot 'LICENSE')
Copy-Item -LiteralPath (Join-Path $projectRoot 'build_scripts\README_Release.txt') `
    -Destination (Join-Path $packageRoot 'README_Release.txt')

Invoke-Python `
    'build_scripts\generate_default_settings.py' `
    (Join-Path $packageRoot 'settings.json')

$requiredPaths = @(
    'nodriver_tixcraft.exe',
    'settings.exe',
    '_internal',
    'assets',
    'www',
    'settings.json',
    'README_Release.txt',
    'CHANGELOG.md',
    'ENTERPRISE_REFACTOR_REPORT.md'
)
foreach ($relativePath in $requiredPaths) {
    if (-not (Test-Path -LiteralPath (Join-Path $packageRoot $relativePath))) {
        throw "Release package is missing required path: $relativePath"
    }
}

New-ReleaseArchive -SourceDirectory $packageRoot -DestinationPath $zipPath
$hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText(
    $checksumPath,
    "$hash  $([System.IO.Path]::GetFileName($zipPath))$([Environment]::NewLine)",
    [System.Text.Encoding]::ASCII
)

Write-Host ''
Write-Host 'Release package created successfully:'
Write-Host "  Folder: $packageRoot"
Write-Host "  ZIP:    $zipPath"
Write-Host "  SHA256: $hash"
