#!/bin/bash

# setup_env.sh - Setup Python environment for DesignBuilder_DXF_to_IDF_Pipeline (Linux/macOS)
#
# Usage:
#   bash scripts/setup_env.sh
#   chmod +x scripts/setup_env.sh && ./scripts/setup_env.sh

set -e  # Exit on error

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Get script directory and project root
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VENV_PATH="$PROJECT_ROOT/.venv"
REQUIREMENTS_FILE="$PROJECT_ROOT/requirements.txt"
LOGS_DIR="$PROJECT_ROOT/logs/runs"

# Create logs directory
mkdir -p "$LOGS_DIR"

# Helper functions
print_status() {
    local message=$1
    local status=${2:-INFO}
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    
    case "$status" in
        SUCCESS)
            echo -e "${timestamp} ${GREEN}[✓]${NC} ${message}"
            ;;
        ERROR)
            echo -e "${timestamp} ${RED}[✗]${NC} ${message}"
            ;;
        WARNING)
            echo -e "${timestamp} ${YELLOW}[!]${NC} ${message}"
            ;;
        *)
            echo -e "${timestamp} ${CYAN}[>]${NC} ${message}"
            ;;
    esac
}

print_header() {
    echo ""
    print_status "=========================================================="
    print_status "$1"
    print_status "=========================================================="
}

# Main setup
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║ DesignBuilder_DXF_to_IDF_Pipeline - Python Setup (Linux) ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# 1. Check Python
print_header "1. CHECKING PYTHON"

if command -v python3 &> /dev/null; then
    PYTHON_PATH=$(command -v python3)
    PYTHON_VERSION=$(python3 --version)
    print_status "Python found: $PYTHON_PATH" SUCCESS
    print_status "Version: $PYTHON_VERSION" SUCCESS
elif command -v python &> /dev/null; then
    PYTHON_PATH=$(command -v python)
    PYTHON_VERSION=$(python --version)
    print_status "Python found: $PYTHON_PATH" SUCCESS
    print_status "Version: $PYTHON_VERSION" SUCCESS
else
    print_status "Python not found in PATH" ERROR
    print_status "Please install Python 3.10+ (apt install python3 / brew install python)" WARNING
    exit 1
fi

# 2. Check/Create .venv
print_header "2. CHECKING VIRTUAL ENVIRONMENT"

if [ -d "$VENV_PATH" ]; then
    print_status ".venv found at $VENV_PATH" SUCCESS
    VENV_PYTHON="$VENV_PATH/bin/python"
    if [ -f "$VENV_PYTHON" ]; then
        print_status "Python executable: $VENV_PYTHON" SUCCESS
    fi
else
    print_status ".venv not found, creating..." INFO
    # Use python3 if available, otherwise python
    if command -v python3 &> /dev/null; then
        python3 -m venv "$VENV_PATH"
    else
        python -m venv "$VENV_PATH"
    fi
    
    if [ -d "$VENV_PATH" ]; then
        print_status ".venv created successfully" SUCCESS
    else
        print_status "Failed to create .venv" ERROR
        exit 1
    fi
fi

# 3. Upgrade pip
print_header "3. UPGRADING PIP"

VENV_PYTHON="$VENV_PATH/bin/python"

if [ -z "$SKIP_PIP" ]; then
    print_status "Upgrading pip, setuptools, wheel..."
    if $VENV_PYTHON -m pip install --upgrade pip setuptools wheel &> /dev/null; then
        print_status "pip upgraded successfully" SUCCESS
    else
        print_status "Failed to upgrade pip (may not be critical)" WARNING
    fi
else
    print_status "Skipped (SKIP_PIP=1)" INFO
fi

# 4. Check requirements.txt
print_header "4. CHECKING REQUIREMENTS.TXT"

if [ -f "$REQUIREMENTS_FILE" ]; then
    print_status "requirements.txt found" SUCCESS
    PACKAGE_COUNT=$(grep -c '^[a-zA-Z]' "$REQUIREMENTS_FILE" || echo "0")
    print_status "Packages to install: $PACKAGE_COUNT" INFO
else
    print_status "requirements.txt not found" ERROR
    exit 1
fi

# 5. Install packages
print_header "5. INSTALLING PACKAGES"

if [ -z "$SKIP_PACKAGES" ]; then
    print_status "Installing from requirements.txt (this may take a few minutes)..."
    if $VENV_PYTHON -m pip install -r "$REQUIREMENTS_FILE"; then
        print_status "All packages installed successfully" SUCCESS
    else
        print_status "Package installation failed" ERROR
        exit 1
    fi
else
    print_status "Skipped (SKIP_PACKAGES=1)" INFO
fi

# 6. Verify installation
print_header "6. VERIFYING INSTALLATION"

TEST_PACKAGES=("pandas" "yaml" "ezdxf" "eppy" "pydantic")

for pkg in "${TEST_PACKAGES[@]}"; do
    if $VENV_PYTHON -c "import $pkg" 2>/dev/null; then
        print_status "✓ $pkg" SUCCESS
    else
        print_status "✗ $pkg (FAILED)" ERROR
    fi
done

# 7. Instructions
print_header "SETUP COMPLETE"

echo ""
echo -e "${CYAN}Next steps:${NC}"
echo ""
echo -e "${CYAN}1. Activate virtual environment:${NC}"
echo "   source .venv/bin/activate"
echo ""
echo -e "${CYAN}2. Run the Python setup and validation script:${NC}"
echo "   python scripts/check_and_setup_python_env.py"
echo ""
echo -e "${CYAN}3. Start the data processing pipeline:${NC}"
echo "   python scripts/run_pipeline.py --help"
echo ""
print_status "=========================================================="
echo ""
