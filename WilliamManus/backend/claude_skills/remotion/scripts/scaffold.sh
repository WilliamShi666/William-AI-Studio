#!/usr/bin/env bash
# scaffold.sh — Create a new Remotion project from the basic template
#
# Usage: bash scaffold.sh <project-name>
#
# Creates /workspace/<project-name>/ with a working Remotion project.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
TEMPLATE_DIR="$SKILL_DIR/templates/basic"
WORKSPACE="/workspace"

# Source Remotion Chrome path if not already set
if [ -z "${REMOTION_CHROME_EXECUTABLE:-}" ] && [ -f /etc/profile.d/remotion-chrome.sh ]; then
  source /etc/profile.d/remotion-chrome.sh
fi

if [ $# -lt 1 ]; then
  echo "Usage: bash scaffold.sh <project-name>"
  echo "Creates /workspace/<project-name>/ with a Remotion project template."
  exit 1
fi

PROJECT_NAME="$1"
PROJECT_DIR="$WORKSPACE/$PROJECT_NAME"

if [ -d "$PROJECT_DIR" ]; then
  echo "Error: Directory $PROJECT_DIR already exists."
  echo "Choose a different project name or remove the existing directory."
  exit 1
fi

echo "Creating Remotion project: $PROJECT_DIR"

# Copy template
cp -r "$TEMPLATE_DIR" "$PROJECT_DIR"

# Create public directory for static assets
mkdir -p "$PROJECT_DIR/public"

# Install dependencies (resolve from global npm cache when available)
echo "Installing dependencies..."
cd "$PROJECT_DIR"
npm install --prefer-offline 2>&1 | tail -5

# Verify installation
if [ -d "$PROJECT_DIR/node_modules/remotion" ]; then
  echo ""
  echo "Project created successfully at: $PROJECT_DIR"
  echo ""
  echo "Project structure:"
  find "$PROJECT_DIR" -maxdepth 3 -not -path "*/node_modules/*" -not -path "*/.git/*" | sort
  echo ""
  echo "Next steps:"
  echo "  1. Edit src/Root.tsx to register your compositions"
  echo "  2. Create component files in src/"
  echo "  3. Place assets in public/"
  echo "  4. Render: bash render.sh $PROJECT_NAME MyComposition /workspace/output.mp4"
else
  echo "Warning: npm install may have failed. Check node_modules."
  echo "You can retry with: cd $PROJECT_DIR && npm install"
fi
