#!/usr/bin/env bash
# FORGE CLI Installer — https://vibe2prod.com
# Usage: curl -fsSL https://get.vibe2prod.com | bash
set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BOLD}FORGE CLI Installer${NC}"
echo "===================="
echo ""

# Check Python 3.10+
check_python() {
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            version=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
            major=$(echo "$version" | cut -d. -f1)
            minor=$(echo "$version" | cut -d. -f2)
            if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON=$(check_python) || {
    echo -e "${RED}Error: Python 3.10+ not found.${NC}"
    echo "Install Python: https://www.python.org/downloads/"
    exit 1
}

echo -e "${GREEN}✓${NC} Found Python: $($PYTHON --version)"

# Install via pip or pipx
if command -v pipx &>/dev/null; then
    echo "Installing via pipx..."
    pipx install vibe2prod
elif command -v pip3 &>/dev/null; then
    echo "Installing via pip3..."
    pip3 install --user vibe2prod
elif command -v pip &>/dev/null; then
    echo "Installing via pip..."
    pip install --user vibe2prod
else
    echo -e "${RED}Error: pip not found.${NC}"
    exit 1
fi

echo -e "${GREEN}✓${NC} vibe2prod installed"

# Run setup
if [ -t 0 ]; then
    echo ""
    vibe2prod setup
else
    echo ""
    echo -e "${YELLOW}Non-interactive environment detected.${NC}"
    echo "Run 'vibe2prod setup' to configure."
fi
