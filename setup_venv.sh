#!/usr/bin/env bash
# Sets up a virtual environment using the Python version in .python-version
# and installs dependencies from requirements.txt.
# Requires: uv (https://github.com/astral-sh/uv)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v uv &>/dev/null; then
    echo "Error: 'uv' is not installed."
    echo "Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "Or see: https://github.com/astral-sh/uv"
    exit 1
fi

PYTHON_VERSION="$(cat .python-version)"
echo "Python version: $PYTHON_VERSION"

echo "Creating virtual environment..."
uv venv --python "$PYTHON_VERSION" .venv

echo "Installing dependencies from requirements.txt..."
uv pip install --python .venv/bin/python -r requirements.txt

echo ""
echo "Done. Activate with:"
echo "  source .venv/bin/activate"
