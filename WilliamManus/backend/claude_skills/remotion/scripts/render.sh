#!/usr/bin/env bash
# render.sh — Render a Remotion composition to video
#
# Usage: bash render.sh <project-dir> <composition-id> <output-path> [extra-flags...]
#
# Examples:
#   bash render.sh my-video MyComposition /workspace/output.mp4
#   bash render.sh my-video MyComposition /workspace/output.gif --codec=gif --every-nth-frame=2
#   bash render.sh my-video MyComposition /workspace/output.webm --codec=vp8

set -euo pipefail

WORKSPACE="/workspace"

# Source Remotion Chrome path if not already set
if [ -z "${REMOTION_CHROME_EXECUTABLE:-}" ] && [ -f /etc/profile.d/remotion-chrome.sh ]; then
  source /etc/profile.d/remotion-chrome.sh
fi

if [ $# -lt 3 ]; then
  echo "Usage: bash render.sh <project-dir> <composition-id> <output-path> [extra-flags...]"
  echo ""
  echo "Arguments:"
  echo "  project-dir     Project directory name (under /workspace)"
  echo "  composition-id  Composition ID to render (as defined in Root.tsx)"
  echo "  output-path     Output file path (e.g., /workspace/output.mp4)"
  echo "  extra-flags     Additional flags passed to 'npx remotion render'"
  echo ""
  echo "Examples:"
  echo "  bash render.sh my-video MyComp /workspace/output.mp4"
  echo "  bash render.sh my-video MyComp /workspace/out.gif --codec=gif"
  exit 1
fi

PROJECT_DIR_NAME="$1"
COMPOSITION_ID="$2"
OUTPUT_PATH="$3"
shift 3
EXTRA_FLAGS=("$@")

# Resolve project directory
if [[ "$PROJECT_DIR_NAME" = /* ]]; then
  PROJECT_DIR="$PROJECT_DIR_NAME"
else
  PROJECT_DIR="$WORKSPACE/$PROJECT_DIR_NAME"
fi

if [ ! -d "$PROJECT_DIR" ]; then
  echo "Error: Project directory not found: $PROJECT_DIR"
  exit 1
fi

if [ ! -f "$PROJECT_DIR/src/index.ts" ]; then
  echo "Error: src/index.ts not found in $PROJECT_DIR"
  echo "Is this a valid Remotion project?"
  exit 1
fi

# Ensure output directory exists
OUTPUT_DIR="$(dirname "$OUTPUT_PATH")"
mkdir -p "$OUTPUT_DIR"

echo "Rendering composition '$COMPOSITION_ID' from $PROJECT_DIR"
echo "Output: $OUTPUT_PATH"

cd "$PROJECT_DIR"

# Build render command
RENDER_CMD=(npx remotion render src/index.ts "$COMPOSITION_ID" "$OUTPUT_PATH" --log=error)

# Append extra flags
if [ ${#EXTRA_FLAGS[@]} -gt 0 ]; then
  RENDER_CMD+=("${EXTRA_FLAGS[@]}")
fi

echo "Command: ${RENDER_CMD[*]}"
echo ""

# Execute render
"${RENDER_CMD[@]}"

# Verify output
if [ -f "$OUTPUT_PATH" ]; then
  FILE_SIZE=$(du -h "$OUTPUT_PATH" | cut -f1)
  echo ""
  echo "Render complete!"
  echo "  Output: $OUTPUT_PATH"
  echo "  Size:   $FILE_SIZE"
else
  echo ""
  echo "Error: Output file was not created: $OUTPUT_PATH"
  echo "Check the render logs above for errors."
  exit 1
fi
