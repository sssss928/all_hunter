[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version
)

$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$distRoot = Join-Path $projectRoot 'dist'
$sourceRoot = Join-Path $distRoot 'source'
$stageRoot = Join-Path $sourceRoot "myhunter-$Version-source"
$zipPath = Join-Path $sourceRoot "myhunter-$Version-source.zip"
$checksumPath = "$zipPath.sha256"

function Assert-SourcePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $absolute = [System.IO.Path]::GetFullPath($Path)
    $expected = [System.IO.Path]::GetFullPath($sourceRoot)
    if (-not $absolute.StartsWith(
        $expected + [IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to modify a path outside dist/source: $absolute"
    }
    return $absolute
}

New-Item -ItemType Directory -Path $sourceRoot -Force | Out-Null
foreach ($target in @($stageRoot, $zipPath, $checksumPath)) {
    if (Test-Path -LiteralPath $target) {
        $safeTarget = Assert-SourcePath $target
        Remove-Item -LiteralPath $safeTarget -Recurse -Force
    }
}
New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null

$directories = @('.github', 'build_scripts', 'docs', 'guide', 'src', 'tests')
foreach ($directory in $directories) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $directory) `
        -Destination $stageRoot -Recurse -Force
}

$files = @(
    '.env.example',
    '.gitignore',
    'CHANGELOG.md',
    'CONTRIBUTING.md',
    'ENTERPRISE_REFACTOR_REPORT.md',
    'LEGAL_NOTICE.md',
    'LICENSE',
    'pyproject.toml',
    'README.md',
    'requirement.txt',
    'settings.example.json'
)
foreach ($file in $files) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $file) `
        -Destination $stageRoot -Force
}

$generatedDirectories = Get-ChildItem -LiteralPath $stageRoot -Recurse -Directory |
    Where-Object { $_.Name -in @('__pycache__', '.pytest_cache', '.ruff_cache') }
foreach ($directory in $generatedDirectories) {
    $safeDirectory = Assert-SourcePath $directory.FullName
    Remove-Item -LiteralPath $safeDirectory -Recurse -Force
}
Get-ChildItem -LiteralPath $stageRoot -Recurse -File |
    Where-Object { $_.Extension -in @('.pyc', '.pyo') } |
    ForEach-Object {
        $safeFile = Assert-SourcePath $_.FullName
        Remove-Item -LiteralPath $safeFile -Force
    }

$sourceFiles = @(Get-ChildItem -LiteralPath $stageRoot -Recurse -File)
if ($sourceFiles.Count -lt 20) {
    throw "Source staging unexpectedly contains only $($sourceFiles.Count) files"
}
$requiredSourceFiles = @(
    'src\platforms\nolworld.py',
    'src\nodriver_tixcraft.py',
    '.github\workflows\ci.yml',
    '.github\workflows\release.yml',
    'settings.example.json',
    'ENTERPRISE_REFACTOR_REPORT.md'
)
foreach ($relativePath in $requiredSourceFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $stageRoot $relativePath))) {
        throw "Source staging is missing required file: $relativePath"
    }
}

Compress-Archive -Path (Join-Path $stageRoot '*') `
    -DestinationPath $zipPath -CompressionLevel Optimal -Force
$hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash  $(Split-Path -Leaf $zipPath)" |
    Set-Content -LiteralPath $checksumPath -Encoding ascii

Write-Host "Source package: $zipPath"
Write-Host "SHA256: $hash"
