param(
    [string]$NodeExe = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

if (-not $NodeExe) {
    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    if (-not $nodeCommand) {
        throw "node.exe를 찾지 못했습니다. -NodeExe 로 직접 지정해 주세요."
    }
    $NodeExe = $nodeCommand.Source
}

if (-not (Test-Path -LiteralPath $NodeExe)) {
    throw "node.exe 경로가 없습니다: $NodeExe"
}

Write-Host "Installing PyInstaller..."
& py -3 -m pip install pyinstaller

$distDir = Join-Path $ProjectRoot "dist"
$buildDir = Join-Path $ProjectRoot "build"
$specFile = Join-Path $ProjectRoot "TetrisScan.spec"

if (Test-Path -LiteralPath $distDir) {
    Remove-Item -LiteralPath $distDir -Recurse -Force
}
if (Test-Path -LiteralPath $buildDir) {
    Remove-Item -LiteralPath $buildDir -Recurse -Force
}
if (Test-Path -LiteralPath $specFile) {
    Remove-Item -LiteralPath $specFile -Force
}

$pyInstallerArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", "TetrisScan",
    "--add-data", "config.json;.",
    "--add-data", "tools\hydra;tools\hydra",
    "--add-data", "tools\gomen.js;tools",
    "--add-data", "tools\gomen_bg.wasm;tools",
    "--add-data", "tools\gomen_solver.js;tools",
    "--add-data", "tools\legal-boards.leb128;tools",
    "--add-data", "tools\setup_finder\setup_data.json;tools\setup_finder",
    "--add-binary", "$NodeExe;tools",
    "main.py"
)

Write-Host "Building EXE..."
& py -3 @pyInstallerArgs

$exePath = Join-Path $distDir "TetrisScan.exe"
if (-not (Test-Path -LiteralPath $exePath)) {
    throw "빌드가 끝났지만 EXE를 찾지 못했습니다: $exePath"
}

if (Test-Path -LiteralPath $buildDir) {
    Remove-Item -LiteralPath $buildDir -Recurse -Force
}
if (Test-Path -LiteralPath $specFile) {
    Remove-Item -LiteralPath $specFile -Force
}

Write-Host ""
Write-Host "Build complete:"
Write-Host $exePath
