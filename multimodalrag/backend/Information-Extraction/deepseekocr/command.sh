#!/usr/bin/env bash
set -euo pipefail

# Run the API server. Override DEEPSEEK_OCR_MODEL_PATH for your local model checkout.
python api_server_mineru_format.py \
  --model-path "${DEEPSEEK_OCR_MODEL_PATH:-./models/deepseek-ocr}" \
  --gpu-id "${DEEPSEEK_OCR_GPU_ID:-0}" \
  --port "${DEEPSEEK_OCR_PORT:-8705}"
