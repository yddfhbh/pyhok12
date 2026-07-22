$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
Set-Location $ProjectRoot

$requiredPaths = @(
    (Join-Path $PSScriptRoot "browser-source\tetrio-cdp-source.mjs"),
    (Join-Path $PSScriptRoot "browser-source\chromium-launch.mjs"),
    (Join-Path $PSScriptRoot "browser-source\ddd-ws-observer.mjs"),
    (Join-Path $PSScriptRoot "browser-source\vs-ws-bridge.mjs"),
    (Join-Path $PSScriptRoot "node_modules"),
    (Join-Path $PSScriptRoot "package.json"),
    (Join-Path $PSScriptRoot "tools\node.exe")
)

foreach ($requiredPath in $requiredPaths) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required build input is missing: $requiredPath"
    }
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
    "--add-data", "$PSScriptRoot\config.json;.",
    "--add-data", "$PSScriptRoot\browser-source;browser-source",
    "--add-data", "$PSScriptRoot\node_modules;node_modules",
    "--add-data", "$PSScriptRoot\package.json;.",
    "--add-data", "$PSScriptRoot\tools\hydra;tools\hydra",
    "--add-data", "$PSScriptRoot\tools\gomen.js;tools",
    "--add-data", "$PSScriptRoot\tools\gomen_bg.wasm;tools",
    "--add-data", "$PSScriptRoot\tools\gomen_solver.js;tools",
    "--add-data", "$PSScriptRoot\tools\legal-boards.leb128;tools",
    "--add-data", "$PSScriptRoot\tools\setup_finder\setup_data.json;tools\setup_finder",
    "--add-binary", "$PSScriptRoot\tools\node.exe;tools",
    "$PSScriptRoot\main.py"
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
