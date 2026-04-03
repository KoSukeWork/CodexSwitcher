[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$ErrorActionPreference = "Stop"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv was not found. Please install uv first."
}

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $repoRoot
try {
    Write-Host "[1/3] Sync dependencies via uv"
    uv sync --locked --group build
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency sync failed with exit code: $LASTEXITCODE"
    }

    Write-Host "[2/3] Build ico"
    @'
from pathlib import Path
try:
    from PIL import Image
except Exception as exc:
    raise SystemExit(f"Pillow is unavailable: {exc}")
src = Path("icon_app.png")
dst = Path("icon_app.ico")
if src.exists():
    img = Image.open(src)
    img.save(dst, sizes=[(256,256), (128,128), (64,64), (48,48), (32,32), (16,16)])
'@ | uv run --locked python -
    if ($LASTEXITCODE -ne 0) {
        throw "ICO generation failed with exit code: $LASTEXITCODE"
    }

    Write-Host "[3/3] Package with PyInstaller"
    uv run --locked --group build pyinstaller --clean --noconfirm codex_switcher.spec
    if ($LASTEXITCODE -ne 0) {
      throw "Packaging failed with exit code: $LASTEXITCODE"
    }

    Write-Host "Done: dist/CodexSwitcher.exe"
}
finally {
    Pop-Location
}
