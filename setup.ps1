#Requires -Version 5.1
<#
.SYNOPSIS
    wbox-mcp installer for Windows.

.DESCRIPTION
    Remote install:
        irm https://raw.githubusercontent.com/quazardous/wbox-mcp/main/setup.ps1 | iex

    From a local clone (dev mode):
        .\setup.ps1 -DevMode

    Custom install dir:
        .\setup.ps1 -InstallDir C:\my\path

    Skip PATH modification:
        .\setup.ps1 -NoPathUpdate
#>

param(
    [string]$InstallDir = "",
    [switch]$DevMode,
    [switch]$NoPathUpdate,
    [switch]$Help
)

$ErrorActionPreference = "Stop"
$REPO = "https://github.com/quazardous/wbox-mcp.git"

if ($Help) {
    Write-Host @"
Usage: setup.ps1 [OPTIONS]

Options:
  -InstallDir DIR   Install to DIR (default: ~\.local\share\wbox-mcp)
  -DevMode          Use current repo directory (no clone, no pull)
  -NoPathUpdate     Don't add install dir to user PATH
  -Help             Show this help
"@
    exit 0
}

Write-Host "=== wbox-mcp setup (Windows) ==="
Write-Host ""

# ── Check prerequisites ──────────────────────────────────────────

$missing = @()
foreach ($cmd in @("python", "uv", "git")) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        $missing += $cmd
    }
}

if ($missing.Count -gt 0) {
    Write-Host "Missing required tools: $($missing -join ', ')" -ForegroundColor Yellow
    Write-Host ""

    $hasWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)

    # Auto-install what we can
    $stillMissing = @()

    if ($missing -contains "python") {
        if ($hasWinget) {
            Write-Host "  Installing Python via winget..." -ForegroundColor Cyan
            winget install --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements -e 2>$null
            # Refresh PATH for this session
            $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
            if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
                $stillMissing += "python"
            }
        } else {
            Write-Host "  python: install from https://www.python.org/downloads/"
            $stillMissing += "python"
        }
    }

    if ($missing -contains "uv") {
        Write-Host "  Installing uv..." -ForegroundColor Cyan
        try {
            irm https://astral.sh/uv/install.ps1 | iex
            # Refresh PATH
            $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
            if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
                $uvPath = Join-Path $env:USERPROFILE ".local\bin"
                $cargoPath = Join-Path $env:USERPROFILE ".cargo\bin"
                foreach ($p in @($uvPath, $cargoPath)) {
                    if (Test-Path (Join-Path $p "uv.exe")) {
                        $env:PATH = "$p;$env:PATH"
                        break
                    }
                }
            }
            if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
                $stillMissing += "uv"
            }
        } catch {
            Write-Host "  uv install failed: $_" -ForegroundColor Red
            $stillMissing += "uv"
        }
    }

    if ($missing -contains "git") {
        if ($hasWinget) {
            Write-Host "  Installing git via winget..." -ForegroundColor Cyan
            winget install --id Git.Git --accept-source-agreements --accept-package-agreements -e 2>$null
            $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
            if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
                $stillMissing += "git"
            }
        } else {
            Write-Host "  git: install from https://git-scm.com or: winget install Git.Git"
            $stillMissing += "git"
        }
    }

    if ($stillMissing.Count -gt 0) {
        Write-Host ""
        Write-Host "Could not install: $($stillMissing -join ', ')" -ForegroundColor Red
        Write-Host "Install them manually, then re-run this script."
        exit 1
    }

    Write-Host ""
}

$pythonVersion = (python --version 2>&1).ToString()
Write-Host "  python: $pythonVersion"
Write-Host "  uv:     $((uv --version 2>&1).ToString())"
Write-Host "  git:    $((git --version 2>&1).ToString())"
Write-Host ""

# ── Check Windows version ────────────────────────────────────────

$osVersion = [System.Environment]::OSVersion.Version
if ($osVersion.Major -lt 10) {
    Write-Host "WARNING: wbox-mcp Windows backend requires Windows 10+." -ForegroundColor Yellow
}

# ── Resolve install directory ────────────────────────────────────

$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { $PWD.Path }

if ($DevMode) {
    $InstallDir = $ScriptDir
    Write-Host "Dev mode: using local repo at $InstallDir"
} elseif ($InstallDir -eq "") {
    # Check if we're running from inside the repo
    $pyprojectPath = Join-Path $ScriptDir "pyproject.toml"
    if ((Test-Path $pyprojectPath) -and (Select-String -Path $pyprojectPath -Pattern "wbox-mcp" -Quiet)) {
        $InstallDir = $ScriptDir
    } else {
        $InstallDir = Join-Path $env:USERPROFILE ".local\share\wbox-mcp"
    }
}

# ── Clone or update ──────────────────────────────────────────────

if ($DevMode) {
    Write-Host "Skipping git operations (dev mode)."
} elseif (Test-Path (Join-Path $InstallDir ".git")) {
    Write-Host "Updating existing install in $InstallDir..."
    git -C $InstallDir pull --ff-only
    if ($LASTEXITCODE -ne 0) {
        Write-Host "git pull failed. You may need to resolve conflicts manually." -ForegroundColor Yellow
    }
} else {
    Write-Host "Cloning wbox-mcp to $InstallDir..."
    $parentDir = Split-Path $InstallDir -Parent
    if (-not (Test-Path $parentDir)) {
        New-Item -ItemType Directory -Path $parentDir -Force | Out-Null
    }
    git clone $REPO $InstallDir
    if ($LASTEXITCODE -ne 0) {
        Write-Host "git clone failed." -ForegroundColor Red
        exit 1
    }
}

# ── Install Python package ───────────────────────────────────────

Write-Host ""
Write-Host "Installing package..."
Push-Location $InstallDir

try {
    # Create venv
    uv venv --python python .venv 2>$null

    # Activate and install
    $venvPython = Join-Path $InstallDir ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        Write-Host "Error: venv creation failed." -ForegroundColor Red
        exit 1
    }

    uv pip install -e . --python $venvPython
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Error: package install failed." -ForegroundColor Red
        exit 1
    }
} finally {
    Pop-Location
}

# ── Create shims in ~/.local/bin ─────────────────────────────────

$binDir = Join-Path $env:USERPROFILE ".local\bin"
if (-not (Test-Path $binDir)) {
    New-Item -ItemType Directory -Path $binDir -Force | Out-Null
}

$venvScripts = Join-Path $InstallDir ".venv\Scripts"

foreach ($name in @("wboxr", "wbox-mcp")) {
    $shimPath = Join-Path $binDir "$name.cmd"
    $target = Join-Path $venvScripts "$name.exe"

    if (Test-Path $target) {
        # Create a simple CMD shim
        Set-Content -Path $shimPath -Value "@echo off`n`"$target`" %*" -Encoding ASCII
        Write-Host "  Created shim: $shimPath -> $target"
    } else {
        # Fallback: use python -m
        $venvPython = Join-Path $venvScripts "python.exe"
        Set-Content -Path $shimPath -Value "@echo off`n`"$venvPython`" -m wbox.cli.$($name.Replace('-','_').Replace('wboxr','registry').Replace('wbox_mcp','server')) %*" -Encoding ASCII
        Write-Host "  Created shim: $shimPath (via python -m)"
    }
}

# ── Update PATH ──────────────────────────────────────────────────

if (-not $NoPathUpdate) {
    $userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if ($userPath -notlike "*$binDir*") {
        [Environment]::SetEnvironmentVariable("PATH", "$binDir;$userPath", "User")
        Write-Host ""
        Write-Host "  Added $binDir to user PATH." -ForegroundColor Green
        Write-Host "  Restart your terminal for PATH changes to take effect."
    }
}

# Check current session PATH
$inPath = $env:PATH -like "*$binDir*"

# ── Summary ──────────────────────────────────────────────────────

Write-Host ""
Write-Host "Done! Installed from: $InstallDir" -ForegroundColor Green
if ($DevMode) {
    Write-Host "  (dev mode - edits to source take effect immediately)"
}
Write-Host ""
Write-Host "  wboxr     - $(Join-Path $binDir 'wboxr.cmd')"
Write-Host "  wbox-mcp  - $(Join-Path $binDir 'wbox-mcp.cmd')"
Write-Host ""

if (-not $inPath) {
    Write-Host "NOTE: Restart your terminal, then:" -ForegroundColor Yellow
} else {
    Write-Host "Quick start:"
}

Write-Host "  mkdir my-app-mcp; cd my-app-mcp"
Write-Host "  wboxr init"
Write-Host ""
Write-Host "To update later:"
if ($DevMode) {
    Write-Host "  git pull; .\setup.ps1 -DevMode"
} else {
    Write-Host "  & `"$(Join-Path $InstallDir 'setup.ps1')`""
}
