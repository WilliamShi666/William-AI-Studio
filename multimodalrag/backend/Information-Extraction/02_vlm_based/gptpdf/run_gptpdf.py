import os
from pathlib import Path

from gptpdf import parse_pdf

pdf_path = os.getenv("GPTPDF_INPUT_PDF", "examples/input.pdf")
output_dir = os.getenv("GPTPDF_OUTPUT_DIR", "output/gptpdf")

API_KEY = os.getenv("GPTPDF_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
MODEL_NAME = os.getenv("GPTPDF_MODEL_NAME", "qwen3-vl-plus")
MODEL_URL = os.getenv("GPTPDF_MODEL_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

if not API_KEY:
    raise RuntimeError("Set GPTPDF_API_KEY or DASHSCOPE_API_KEY before running this script.")

Path(output_dir).mkdir(parents=True, exist_ok=True)

content, image_paths = parse_pdf(
    pdf_path=pdf_path,
    output_dir=output_dir,
    api_key=API_KEY,
    base_url=MODEL_URL,
    model=MODEL_NAME,
    gpt_worker=1,
    # 删掉这三行 ↓↓↓
    # prompt=None,
    # rect_prompt=None,
    # role_prompt=None,
)

print('Markdown length:', len(content))
print('Extracted images:', image_paths)
# output_dir/output.md 会同时写入最终的 markdown
