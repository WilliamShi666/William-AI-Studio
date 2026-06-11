#!/usr/bin/env python3
"""
PPT Generator - Generate PPT slide images using Google Gemini API.

This script generates PPT slide images based on a slide plan and style template,
then creates an HTML viewer for playback.
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv


# =============================================================================
# Constants
# =============================================================================

DEFAULT_RESOLUTION = "2K"
DEFAULT_TEMPLATE_PATH = "templates/viewer.html"
OUTPUT_BASE_DIR = "outputs"

# Style template markers
TEMPLATE_START_MARKER = "## "
TEMPLATE_END_MARKER = "## "


# =============================================================================
# Environment Configuration
# =============================================================================

def find_and_load_env() -> bool:
    """
    Find and load .env file from multiple locations.

    Search priority:
    1. Current script directory
    2. Parent directories up to project root (containing .git or .env)
    3. Claude Code skill standard location (~/.claude/skills/ppt-generator/)

    Returns:
        True if .env file was found and loaded, False otherwise.
    """
    current_dir = Path(__file__).parent
    env_locations = [
        current_dir / ".env",
        *[parent / ".env" for parent in current_dir.parents],
        Path.home() / ".claude" / "skills" / "ppt-generator" / ".env",
    ]

    for env_path in env_locations:
        if env_path.exists():
            load_dotenv(env_path, override=True)
            print(f"Loaded environment from: {env_path}")
            return True

        # Stop at project root if .git exists
        if env_path.parent != current_dir and (env_path.parent / ".git").exists():
            break

    # Fallback: try default loading from system environment
    load_dotenv(override=True)
    print("Warning: No .env file found, using system environment variables")
    return False


# =============================================================================
# Style Template
# =============================================================================

def load_style_template(style_path: str) -> str:
    """
    Load and parse style template file.

    Args:
        style_path: Path to the style template markdown file.

    Returns:
        Extracted base prompt template string.
    """
    with open(style_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Extract base prompt template section
    start_marker = "## "
    end_marker = "## "

    start_idx = content.find(start_marker)
    end_idx = content.find(end_marker, start_idx + len(start_marker))

    if start_idx == -1 or end_idx == -1:
        print("Warning: Could not parse style template, using full content")
        return content

    return content[start_idx + len(start_marker):end_idx].strip()


# =============================================================================
# Prompt Generation
# =============================================================================

def generate_prompt(
    style_template: str,
    page_type: str,
    content_text: str,
    slide_number: int,
    total_slides: int,
) -> str:
    """
    Generate a prompt for a single slide.

    Args:
        style_template: Base style template text.
        page_type: Type of page (cover, data, content).
        content_text: Text content for the slide.
        slide_number: Current slide number (1-indexed).
        total_slides: Total number of slides.

    Returns:
        Complete prompt string for image generation.
    """
    prompt_parts = [style_template, "\n\n"]

    # Determine page type based on slide position or explicit type
    is_cover = page_type == "cover" or slide_number == 1
    is_data = page_type == "data" or slide_number == total_slides

    if is_cover:
        prompt_parts.append(
            f"""Please generate a cover page based on visual balance aesthetics.
Place a large complex 3D glass object in the center, overlaid with bold text:

{content_text}

Background with extended aurora waves."""
        )
    elif is_data:
        prompt_parts.append(
            f"""Please generate a data/summary page using split-screen design.
Left side: typeset the following text.
Right side: floating large glowing 3D data visualization:

{content_text}"""
        )
    else:
        prompt_parts.append(
            f"""Please generate a content page using Bento grid layout.
Organize the following content in modular rounded rectangle containers.
Container material must be frosted glass with blur effect:

{content_text}"""
        )

    return "".join(prompt_parts)


# =============================================================================
# Image Generation
# =============================================================================

def get_gemini_client():
    """
    Initialize Gemini client via openai-proxy.org (native Gemini protocol).

    Returns:
        Configured genai.Client instance.

    Raises:
        SystemExit: If google-genai is not installed or API key is missing.
    """
    try:
        from google import genai
    except ImportError:
        print("Error: google-genai library not installed")
        print("Please run: pip install google-genai")
        sys.exit(1)

    api_key = os.environ.get("NANO_BANANA_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: NANO_BANANA_API_KEY or GEMINI_API_KEY environment variable not set")
        sys.exit(1)

    base_url = os.environ.get("NANO_BANANA_BASE_URL", "https://api.openai-proxy.org/google")

    return genai.Client(
        api_key=api_key,
        vertexai=True,            # 强制 REST 协议，避免默认 gRPC 报错
        http_options={"base_url": base_url},
    )


def generate_slide_gemini(
    prompt: str,
    slide_number: int,
    output_dir: str,
    resolution: str = DEFAULT_RESOLUTION,
) -> Optional[str]:
    """Generate a single slide image using Google Gemini (Nano Banana proxy)."""
    from google.genai import types

    try:
        client = get_gemini_client()
        response = client.models.generate_content(
            model=os.environ.get("NANO_BANANA_MODEL", "gemini-3.1-flash-image-preview"),
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(
                    aspect_ratio="16:9",
                    image_size=resolution,
                ),
            ),
        )

        for part in response.parts:
            if part.inline_data is not None:
                image = part.as_image()
                image_path = os.path.join(
                    output_dir, "images", f"slide-{slide_number:02d}.png"
                )
                image.save(image_path)
                return image_path

        print(f"  Slide {slide_number} failed: No image data received")
        return None

    except Exception as e:
        print(f"  Slide {slide_number} failed: {e}")
        return None


def generate_slide_qwen(
    prompt: str,
    slide_number: int,
    output_dir: str,
) -> Optional[str]:
    """Generate a single slide image using Alibaba Cloud Qwen image model (通义千问).

    Retries up to 3 times with exponential backoff on failure.
    """
    import time

    try:
        import dashscope
        from dashscope import MultiModalConversation
        import urllib.request
    except ImportError:
        print("Error: dashscope not installed. Run: pip install dashscope")
        return None

    slot = os.environ.get("QWEN_API_KEY_SLOT", os.environ.get("DASHSCOPE_API_KEY_SLOT", "1"))
    api_key = (
        os.environ.get(f"QWEN_API_KEY_{slot}")
        or os.environ.get("QWEN_API_KEY")
        or os.environ.get("DASHSCOPE_API_KEY")
    )
    if not api_key:
        print("Error: QWEN_API_KEY or DASHSCOPE_API_KEY not set")
        return None

    dashscope.base_http_api_url = os.environ.get(
        "QWEN_BASE_URL", "https://dashscope.aliyuncs.com/api/v1"
    )
    model = os.environ.get("QWEN_MODEL", "qwen-image-2.0")
    messages = [{"role": "user", "content": [{"text": prompt}]}]

    for attempt in range(1, 4):
        try:
            response = MultiModalConversation.call(
                api_key=api_key,
                model=model,
                messages=messages,
                result_format="message",
                stream=False,
                n=1,
            )

            if response.output is None:
                raise RuntimeError(f"output=None (rate limited), code={getattr(response, 'code', '?')}")

            content = response.output.choices[0].message.content
            image_url = next((item["image"] for item in content if "image" in item), None)

            if not image_url:
                raise RuntimeError("No image URL in response")

            image_path = os.path.join(output_dir, "images", f"slide-{slide_number:02d}.png")
            urllib.request.urlretrieve(image_url, image_path)
            return image_path

        except Exception as e:
            wait = 2 ** attempt
            if attempt < 3:
                print(f"  Slide {slide_number} attempt {attempt} failed: {e}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"  Slide {slide_number} failed after 3 attempts: {e}")

    return None


def generate_slide(
    prompt: str,
    slide_number: int,
    output_dir: str,
    resolution: str = DEFAULT_RESOLUTION,
) -> Optional[str]:
    """
    Generate a single PPT slide image. Dispatches to the backend set by IMAGE_BACKEND.

    Backends:
        qwen   — Alibaba Cloud qwen-image-2.0 (通义千问, default)
        gemini — Google Gemini via Nano Banana proxy

    Args:
        prompt: The generation prompt.
        slide_number: Slide number for filename.
        output_dir: Output directory path.
        resolution: Image resolution for Gemini backend (2K or 4K).

    Returns:
        Path to saved image, or None if generation failed.
    """
    print(f"Generating slide {slide_number}...")
    backend = os.environ.get("IMAGE_BACKEND", "qwen").lower()

    if backend == "qwen":
        return generate_slide_qwen(prompt, slide_number, output_dir)
    else:
        return generate_slide_gemini(prompt, slide_number, output_dir, resolution)


# =============================================================================
# Output Generation
# =============================================================================

def generate_viewer_html(
    output_dir: str,
    slide_count: int,
    template_path: str,
) -> str:
    """
    Generate HTML viewer for slides playback.

    Args:
        output_dir: Output directory path.
        slide_count: Total number of slides.
        template_path: Path to HTML template.

    Returns:
        Path to generated HTML file.
    """
    with open(template_path, "r", encoding="utf-8") as f:
        html_template = f.read()

    # Generate image list
    slides_list = [f"'images/slide-{i:02d}.png'" for i in range(1, slide_count + 1)]

    # Replace placeholder
    html_content = html_template.replace(
        "/* IMAGE_LIST_PLACEHOLDER */",
        ",\n            ".join(slides_list),
    )

    html_path = os.path.join(output_dir, "index.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"  Viewer HTML generated: {html_path}")
    return html_path


def export_pdf(output_dir: str, slide_count: int, title: str) -> Optional[str]:
    """
    Export all slide images into a single PDF file.

    Args:
        output_dir: Output directory path containing images/.
        slide_count: Total number of slides (used only for reference).
        title: Presentation title used for the filename.

    Returns:
        Path to generated PDF, or None if export failed.
    """
    import glob as _glob
    try:
        from PIL import Image
    except ImportError:
        print("Error: pillow not installed. Run: pip install pillow")
        return None

    images_dir = os.path.join(output_dir, "images")
    # Use glob to find actual files regardless of starting slide number
    found = sorted(_glob.glob(os.path.join(images_dir, "slide-*.png")))
    pages: List[Any] = []
    for img_path in found:
        pages.append(Image.open(img_path).convert("RGB"))

    if not pages:
        print("  PDF export failed: no images found")
        return None

    safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in title)
    pdf_path = os.path.join(output_dir, f"{safe_title}.pdf")
    pages[0].save(pdf_path, save_all=True, append_images=pages[1:])
    print(f"  PDF exported: {pdf_path}")
    return pdf_path


def export_pptx(output_dir: str, slide_count: int, title: str) -> Optional[str]:
    """
    Export all slide images into a PowerPoint (.pptx) file.

    Args:
        output_dir: Output directory path containing images/.
        slide_count: Total number of slides (used only for reference).
        title: Presentation title used for the filename.

    Returns:
        Path to generated PPTX, or None if export failed.
    """
    import glob as _glob
    try:
        from pptx import Presentation
        from pptx.util import Inches
    except ImportError:
        print("Error: python-pptx not installed. Run: pip install python-pptx")
        return None

    images_dir = os.path.join(output_dir, "images")
    # Use glob to find actual files regardless of starting slide number
    found = sorted(_glob.glob(os.path.join(images_dir, "slide-*.png")))

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank_layout = prs.slide_layouts[6]

    for img_path in found:
        slide = prs.slides.add_slide(blank_layout)
        slide.shapes.add_picture(
            img_path, 0, 0, prs.slide_width, prs.slide_height
        )

    safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in title)
    pptx_path = os.path.join(output_dir, f"{safe_title}.pptx")
    prs.save(pptx_path)
    print(f"  PPTX exported: {pptx_path}")
    return pptx_path


def save_prompts(output_dir: str, prompts_data: Dict[str, Any]) -> str:
    """
    Save all prompts to JSON file.

    Args:
        output_dir: Output directory path.
        prompts_data: Dictionary containing all prompts and metadata.

    Returns:
        Path to saved JSON file.
    """
    prompts_path = os.path.join(output_dir, "prompts.json")
    with open(prompts_path, "w", encoding="utf-8") as f:
        json.dump(prompts_data, f, ensure_ascii=False, indent=2)
    print(f"  Prompts saved: {prompts_path}")
    return prompts_path


# =============================================================================
# Main Entry Point
# =============================================================================

def create_argument_parser() -> argparse.ArgumentParser:
    """Create and configure argument parser."""
    parser = argparse.ArgumentParser(
        description="PPT Generator - Generate PPT images using Gemini API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example usage:
  python generate_ppt.py --plan slides_plan.json --style styles/gradient-glass.md --resolution 2K

Environment variables:
  GEMINI_API_KEY: Google AI API key (required)
""",
    )

    parser.add_argument(
        "--plan",
        help="Path to slides plan JSON file (required unless --merge is used)",
    )
    parser.add_argument(
        "--style",
        help="Path to style template file (required unless --merge is used)",
    )
    parser.add_argument(
        "--resolution",
        choices=["2K", "4K"],
        default=DEFAULT_RESOLUTION,
        help=f"Image resolution (default: {DEFAULT_RESOLUTION})",
    )
    parser.add_argument(
        "--output",
        help="Output directory path (default: outputs/TIMESTAMP)",
    )
    parser.add_argument(
        "--template",
        default=DEFAULT_TEMPLATE_PATH,
        help=f"HTML template path (default: {DEFAULT_TEMPLATE_PATH})",
    )
    parser.add_argument(
        "--format",
        default="all",
        help="Output format(s): html, pdf, pptx, all, or comma-separated e.g. pdf,pptx (default: all)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Number of parallel workers (default: 0 = number of slides)",
    )
    parser.add_argument(
        "--merge",
        nargs="+",
        metavar="BATCH_DIR",
        help="Merge mode: collect images from BATCH_DIR.../images/ into a single PPTX+PDF. "
             "Example: --merge batch1 batch2 batch3 batch4 --output merged --title 'My Deck'",
    )
    parser.add_argument(
        "--title",
        help="Presentation title (used for --merge mode output filenames)",
    )

    return parser


def merge_batches(batch_dirs: list, output_dir: str, title: str) -> None:
    """Merge images from multiple batch output directories into one PPTX + PDF."""
    import glob as _glob
    import shutil

    images_out = os.path.join(output_dir, "images")
    os.makedirs(images_out, exist_ok=True)

    # Collect all slide images from each batch, in order
    for batch_dir in batch_dirs:
        imgs_dir = os.path.join(batch_dir, "images")
        found = sorted(_glob.glob(os.path.join(imgs_dir, "slide-*.png")))
        if not found:
            print(f"  Warning: no slide-*.png in {imgs_dir}")
        for src in found:
            dst = os.path.join(images_out, os.path.basename(src))
            shutil.copy2(src, dst)
            print(f"  Copied {os.path.basename(src)} from {os.path.basename(batch_dir)}")

    total = len(sorted(_glob.glob(os.path.join(images_out, "slide-*.png"))))
    print(f"\nTotal slides merged: {total}")
    print("\nExporting PDF...")
    export_pdf(output_dir, total, title)
    print("Exporting PPTX...")
    export_pptx(output_dir, total, title)
    print("\nMerge complete!")
    print(f"Output: {output_dir}/")


def main() -> None:
    """Main entry point for PPT generation."""
    # Load environment variables
    find_and_load_env()

    # Parse arguments
    parser = create_argument_parser()
    args = parser.parse_args()

    # Merge mode: combine batch directories into one PPTX + PDF
    if args.merge:
        output_dir = args.output or "merged"
        title = args.title or "Presentation"
        print(f"Merge mode: {len(args.merge)} batch directories → {output_dir}")
        merge_batches(args.merge, output_dir, title)
        return

    # Normal generation mode requires --plan and --style
    if not args.plan:
        parser.error("--plan is required unless --merge is used")
    if not args.style:
        parser.error("--style is required unless --merge is used")

    # Load slides plan
    with open(args.plan, "r", encoding="utf-8") as f:
        slides_plan = json.load(f)

    # Load style template
    style_template = load_style_template(args.style)

    # Create output directory
    if args.output:
        output_dir = args.output
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = f"{OUTPUT_BASE_DIR}/{timestamp}"

    os.makedirs(os.path.join(output_dir, "images"), exist_ok=True)

    # Print configuration
    slides = slides_plan["slides"]
    total_slides = len(slides)

    print("=" * 60)
    print("PPT Generator Started")
    print("=" * 60)
    print(f"Style: {args.style}")
    print(f"Resolution: {args.resolution}")
    print(f"Slides: {total_slides}")
    print(f"Output: {output_dir}")
    print("=" * 60)
    print()

    # Initialize prompts data
    print_lock = Lock()
    prompts_data: Dict[str, Any] = {
        "metadata": {
            "title": slides_plan.get("title", "Untitled Presentation"),
            "total_slides": total_slides,
            "resolution": args.resolution,
            "style": args.style,
            "generated_at": datetime.now().isoformat(),
        },
        "slides": [],
    }

    def process_slide(slide_info: Dict[str, Any]) -> Dict[str, Any]:
        slide_number = slide_info["slide_number"]
        page_type = slide_info.get("page_type", "content")
        content_text = slide_info["content"]

        # Use prompt_override if provided, otherwise build from template
        prompt = slide_info.get("prompt_override") or generate_prompt(
            style_template, page_type, content_text, slide_number, total_slides
        )

        image_path = generate_slide(prompt, slide_number, output_dir, args.resolution)

        with print_lock:
            status = "✅" if image_path else "❌"
            print(f"  {status} Slide {slide_number} {'saved' if image_path else 'FAILED'}")

        return {
            "slide_number": slide_number,
            "page_type": page_type,
            "content": content_text,
            "prompt": prompt,
            "image_path": image_path,
        }

    # Parallel generation
    # Qwen API: non-pro model allows 120 RPM, 3 workers is safe
    backend = os.environ.get("IMAGE_BACKEND", "qwen").lower()
    default_workers = 3 if backend == "qwen" else total_slides
    max_workers = args.workers if args.workers > 0 else default_workers
    print(f"Launching {max_workers} parallel workers for {total_slides} slides...")
    print()

    results: Dict[int, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_slide, s): s["slide_number"] for s in slides}
        for future in as_completed(futures):
            result = future.result()
            results[result["slide_number"]] = result

    # Reassemble in slide order
    for i in range(1, total_slides + 1):
        if i in results:
            prompts_data["slides"].append(results[i])

    # Save prompts
    save_prompts(output_dir, prompts_data)

    # Export requested formats (supports comma-separated e.g. "pdf,pptx")
    title = slides_plan.get("title", "Presentation")
    requested = {f.strip() for f in args.format.split(",")}
    fmt_all = "all" in requested
    outputs: Dict[str, Optional[str]] = {}

    if fmt_all or "html" in requested:
        outputs["html"] = generate_viewer_html(output_dir, total_slides, args.template)

    if fmt_all or "pdf" in requested:
        outputs["pdf"] = export_pdf(output_dir, total_slides, title)

    if fmt_all or "pptx" in requested:
        outputs["pptx"] = export_pptx(output_dir, total_slides, title)

    # Print completion summary
    print()
    print("=" * 60)
    print("Generation Complete!")
    print("=" * 60)
    print(f"Output directory: {output_dir}")
    for fmt_name, path in outputs.items():
        if path:
            print(f"{fmt_name.upper()}: {path}")
    print()


if __name__ == "__main__":
    main()
