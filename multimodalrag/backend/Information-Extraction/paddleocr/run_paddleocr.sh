#!/usr/bin/env bash
set -euo pipefail

# Optional: activate your PaddleOCR environment before running this script.
uvicorn api_paddleocr_vl_mineru:app --host "${PADDLEOCR_HOST:-127.0.0.1}" --port "${PADDLEOCR_PORT:-8802}"
