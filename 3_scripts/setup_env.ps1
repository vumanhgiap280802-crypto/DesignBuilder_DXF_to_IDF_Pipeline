# setup_env.ps1 - Setup Python environment for DesignBuilder_DXF_to_IDF_Pipeline (Windows PowerShell)
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts/setup_env.ps1
#
# Or without changing execution policy:
#   powershell -NoProfile -ExecutionPolicy Bypass -Command "& '.\scripts\setup_env.ps1'"

param(
    [switch]$SkipPip = $false,
    [switch]$SkipPackages = $false,
    [switch]$Help = $false
)

if ($Help) {
    Write-Host "
Setup Python environment for DesignBuilder_DXF_to_IDF_Pipeline

Usage:
    powershell -ExecutionPolicy Bypass -File scripts/setup_env.ps1

Options:
    -SkipPip        Skip pip upgrade
    -SkipPackages   Skip package installation
    -Help           Show this help message

Examples:
    # Full setup
    powershell -ExecutionPolicy Bypass -File scripts/setup_env.ps1
    
    # Skip pip upgrade
    powershell -ExecutionPolicy Bypass -File scripts/setup_env.ps1 -SkipPip
"
    exit 0
}

# Get script directory and project root
$ScriptDir = Split-Path -Resolve $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$VenvPath = Join-Path $ProjectRoot ".venv"
$RequirementsFile = Join-Path $ProjectRoot "requirements.txt"
$LogsDir = Join-Path $ProjectRoot "logs" "runs"

# Create logs directory
if (-not (Test-Path $LogsDir)) {
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
}

# Colors for output
$Green = 'Green'
$Red = 'Red'
$Yellow = 'Yellow'
$Cyan = 'Cyan'

function Write-Status {
    param([string]$Message, [string]$Status = "INFO")
    
    $color = $Cyan
    $prefix = "[INFO]"
    
    switch ($Status) {
        "SUCCESS" { $color = $Green; $prefix = "[✓]" }
        "ERROR" { $color = $Red; $prefix = "[✗]" }
        "WARNING" { $color = $Yellow; $prefix = "[!]" }
    }
    
    Write-Host "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $prefix $Message" -ForegroundColor $color
}

Write-Host ""
Write-Host "╔────────────────────────────────────────────────────────────╗"
Write-Host "║ DesignBuilder_DXF_to_IDF_Pipeline - Python Setup (Windows) ║"
Write-Host "╚────────────────────────────────────────────────────────────╝"
Write-Host ""

# 1. Check Python
Write-Status "═══════════════════════════════════════════════════════════"
Write-Status "1. CHECKING PYTHON"
Write-Status "═══════════════════════════════════════════════════════════"

$PythonPath = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $PythonPath) {
    $PythonPath = (Get-Command py -ErrorAction SilentlyContinue).Source
}

if ($PythonPath) {
    $PythonVersion = & python --version 2>&1
    Write-Status "Python found: $PythonPath" STATUS "SUCCESS"
    Write-Status "Version: $PythonVersion" STATUS "SUCCESS"
} else {
    Write-Status "Python not found in PATH" STATUS "ERROR"
    Write-Status "Please install Python 3.10+ from python.org" STATUS "WARNING"
    exit 1
}

# 2. Check/Create .venv
Write-Host ""
Write-Status "═══════════════════════════════════════════════════════════"
Write-Status "2. CHECKING VIRTUAL ENVIRONMENT"
Write-Status "═══════════════════════════════════════════════════════════"

if (Test-Path $VenvPath) {
    Write-Status ".venv found at $VenvPath" STATUS "SUCCESS"
    $VenvPythonPath = Join-Path $VenvPath "Scripts" "python.exe"
    if (Test-Path $VenvPythonPath) {
        Write-Status "Python executable: $VenvPythonPath" STATUS "SUCCESS"
    }
} else {
    Write-Status ".venv not found, creating..." STATUS "INFO"
    & python -m venv $VenvPath
    
    if (Test-Path $VenvPath) {
        Write-Status ".venv created successfully" STATUS "SUCCESS"
    } else {
        Write-Status "Failed to create .venv" STATUS "ERROR"
        exit 1
    }
}

# 3. Upgrade pip
Write-Host ""
Write-Status "═══════════════════════════════════════════════════════════"
Write-Status "3. UPGRADING PIP"
Write-Status "═══════════════════════════════════════════════════════════"

$VenvPythonPath = Join-Path $VenvPath "Scripts" "python.exe"

if (-not $SkipPip) {
    Write-Status "Upgrading pip, setuptools, wheel..."
    & $VenvPythonPath -m pip install --upgrade pip setuptools wheel 2>&1 | Out-Null
    
    if ($LASTEXITCODE -eq 0) {
        Write-Status "pip upgraded successfully" STATUS "SUCCESS"
    } else {
        Write-Status "Failed to upgrade pip (this may not be critical)" STATUS "WARNING"
    }
} else {
    Write-Status "Skipped (--SkipPip flag)" STATUS "INFO"
}

# 4. Check requirements.txt
Write-Host ""
Write-Status "═══════════════════════════════════════════════════════════"
Write-Status "4. CHECKING REQUIREMENTS.TXT"
Write-Status "═══════════════════════════════════════════════════════════"

if (Test-Path $RequirementsFile) {
    Write-Status "requirements.txt found" STATUS "SUCCESS"
    $PackageCount = @((Get-Content $RequirementsFile | Where-Object {$_ -match '^[a-zA-Z]'}).Count)
    Write-Status "Packages to install: $PackageCount" STATUS "INFO"
} else {
    Write-Status "requirements.txt not found" STATUS "ERROR"
    exit 1
}

# 5. Install packages
Write-Host ""
Write-Status "═══════════════════════════════════════════════════════════"
Write-Status "5. INSTALLING PACKAGES"
Write-Status "═══════════════════════════════════════════════════════════"

if (-not $SkipPackages) {
    Write-Status "Installing from requirements.txt (this may take a few minutes)..."
    & $VenvPythonPath -m pip install -r $RequirementsFile
    
    if ($LASTEXITCODE -eq 0) {
        Write-Status "All packages installed successfully" STATUS "SUCCESS"
    } else {
        Write-Status "Package installation failed" STATUS "ERROR"
        exit 1
    }
} else {
    Write-Status "Skipped (--SkipPackages flag)" STATUS "INFO"
}

# 6. Verify installation
Write-Host ""
Write-Status "═══════════════════════════════════════════════════════════"
Write-Status "6. VERIFYING INSTALLATION"
Write-Status "═══════════════════════════════════════════════════════════"

$TestPackages = @("pandas", "yaml", "ezdxf", "eppy", "pydantic")

foreach ($pkg in $TestPackages) {
    $ImportTest = & $VenvPythonPath -c "import $pkg; print('OK')" 2>&1
    if ($ImportTest -contains "OK") {
        Write-Status "✓ $pkg" STATUS "SUCCESS"
    } else {
        Write-Status "✗ $pkg (FAILED)" STATUS "ERROR"
    }
}

# 7. Instructions
Write-Host ""
Write-Status "═══════════════════════════════════════════════════════════"
Write-Status "SETUP COMPLETE"
Write-Status "═══════════════════════════════════════════════════════════"

Write-Host ""
Write-Host "Next steps:" -ForegroundColor $Cyan
Write-Host ""
Write-Host "1. Activate virtual environment:" -ForegroundColor White
Write-Host "   .\.venv\Scripts\Activate.ps1" -ForegroundColor $Cyan
Write-Host ""
Write-Host "2. Or use CMD instead:" -ForegroundColor White
Write-Host "   .venv\Scripts\activate.bat" -ForegroundColor $Cyan
Write-Host ""
Write-Host "3. Run the Python setup and validation script:" -ForegroundColor White
Write-Host "   python scripts\check_and_setup_python_env.py" -ForegroundColor $Cyan
Write-Host ""
Write-Host "4. Start the data processing pipeline:" -ForegroundColor White
Write-Host "   python scripts\run_pipeline.py --help" -ForegroundColor $Cyan
Write-Host ""
Write-Status "═══════════════════════════════════════════════════════════"
Write-Host ""
