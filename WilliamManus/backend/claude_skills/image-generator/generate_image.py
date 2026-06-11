#!/usr/bin/env python3
"""
Image Generator - Generate a single image using DashScope qwen-image-2.0 API.

Uses dashscope.MultiModalConversation.call() — synchronous, no polling needed.
Supports optional reference images for image editing / style transfer.
"""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

import dashscope
from dashscope import MultiModalConversation


# =============================================================================
# Constants
# =============================================================================

dashscope.base_http_api_url = "https://dashscope.aliyuncs.com/api/v1"

DEFAULT_MODEL = "qwen-image-2.0"
DEFAULT_SIZE = "1024*1024"
DEFAULT_OUTPUT_DIR = "outputs"

VALID_SIZES = ["1024*1024", "1280*720", "720*1280", "1024*768", "768*1024"]


# =============================================================================
# Environment
# =============================================================================

def find_and_load_env() -> bool:
    """
    Find and load .env file from multiple locations.

    Search priority:
    1. Script directory
    2. Parent directories up to project root (.git found)
    3. ~/.claude/skills/image-generator/.env
    4. System environment variables

    Returns:
        True if .env file was found and loaded.
    """
    script_dir = Path(__file__).parent
    candidates = [
        script_dir / ".env",
        *[parent / ".env" for parent in script_dir.parents],
        Path.home() / ".claude" / "skills" / "image-generator" / ".env",
    ]

    for env_path in candidates:
        if env_path.exists():
            load_dotenv(env_path, override=True)
            print(f"Loaded environment from: {env_path}")
            return True

        # Stop searching when we hit the project root
        if env_path.parent != script_dir and (env_path.parent / ".git").exists():
            break

    load_dotenv(override=True)
    print("Warning: No .env file found, using system environment variables")
    return False


def get_api_key() -> str:
    """Get DashScope API key from environment.

    Supports dual-key setup for multi-agent rate limit distribution:
      DASHSCOPE_API_KEY_SLOT=1  → uses DASHSCOPE_API_KEY_1
      DASHSCOPE_API_KEY_SLOT=2  → uses DASHSCOPE_API_KEY_2
    Falls back to DASHSCOPE_API_KEY for backward compatibility.
    """
    slot = os.environ.get("DASHSCOPE_API_KEY_SLOT", "1")
    api_key = (
        os.environ.get(f"DASHSCOPE_API_KEY_{slot}")
        or os.environ.get("DASHSCOPE_API_KEY")
    )
    if not api_key:
        print(f"Error: DASHSCOPE_API_KEY_{slot} (or DASHSCOPE_API_KEY) not set")
        print("Please configure it in your .env file")
        print("Apply at: https://dashscope.aliyuncs.com/")
        sys.exit(1)
    return api_key


# =============================================================================
# API Client
# =============================================================================

def build_messages(prompt: str, reference_images: list[str]) -> list[dict]:
    """
    Build the multimodal messages payload.

    Args:
        prompt: Text prompt for image generation.
        reference_images: Optional list of reference image URLs or local paths.

    Returns:
        Messages list for MultiModalConversation.call().
    """
    content: list[dict] = []

    for img in reference_images:
        if img.startswith("http://") or img.startswith("https://"):
            content.append({"image": img})
        else:
            # Local file — convert to file:// URI
            abs_path = Path(img).resolve()
            if not abs_path.exists():
                print(f"Error: Reference image not found: {img}")
                sys.exit(1)
            content.append({"image": abs_path.as_uri()})

    content.append({"text": prompt})

    return [{"role": "user", "content": content}]


def call_generate(api_key: str, prompt: str, model: str, reference_images: list[str]) -> list[str]:
    """
    Call qwen-image-2.0 via MultiModalConversation and return image URLs.

    Args:
        api_key: DashScope API key.
        prompt: Text prompt.
        model: Model name.
        reference_images: Optional reference image URLs or paths.

    Returns:
        List of generated image URLs.

    Raises:
        SystemExit: On API error.
    """
    messages = build_messages(prompt, reference_images)

    try:
        response = MultiModalConversation.call(
            api_key=api_key,
            model=model,
            messages=messages,
            result_format="message",
            stream=False,
        )
    except Exception as e:
        print(f"Error: API call failed: {e}")
        sys.exit(1)

    # Check for error codes
    if hasattr(response, "code") and response.code:
        print(f"Error: API returned error code: {response.code}")
        print(f"Message: {getattr(response, 'message', 'No details')}")
        sys.exit(1)

    # Extract image URLs from response choices
    urls: list[str] = []
    try:
        choices = response.output.choices
        for choice in choices:
            for item in choice.message.content:
                # item may be dict or object
                url = item.get("image") if isinstance(item, dict) else getattr(item, "image", None)
                if url:
                    urls.append(url)
    except (AttributeError, TypeError, KeyError) as e:
        print(f"Error: Could not parse API response: {e}")
        print(f"Response: {response}")
        sys.exit(1)

    if not urls:
        print(f"Error: No image URLs found in response: {response}")
        sys.exit(1)

    return urls


def download_image(url: str, output_dir: str, index: int = 0) -> str:
    """
    Download image from URL and save to output directory.

    Args:
        url: Image URL.
        output_dir: Directory to save the image.
        index: Index suffix when generating multiple images.

    Returns:
        Absolute path to saved image file.

    Raises:
        SystemExit: On download failure.
    """
    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{index}" if index > 0 else ""
    filename = f"image_{timestamp}{suffix}.png"
    output_path = os.path.join(output_dir, filename)

    print(f"Downloading image{suffix}...")

    try:
        response = requests.get(url, timeout=120, stream=True)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Error: Failed to download image: {e}")
        sys.exit(1)

    with open(output_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    return os.path.abspath(output_path)


# =============================================================================
# CLI
# =============================================================================

def create_argument_parser() -> argparse.ArgumentParser:
    """Create and configure argument parser."""
    parser = argparse.ArgumentParser(
        description="Image Generator - Generate images using DashScope qwen-image-2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example usage:
  python generate_image.py --prompt "a futuristic city at night, neon lights, cyberpunk style"
  python generate_image.py --prompt "mountain lake at sunset" --size 1280*720
  python generate_image.py --prompt "portrait of a fox" --size 720*1280 --output my_images/
  python generate_image.py --prompt "dress her in the black skirt" --ref photo.jpg ref2.jpg

Size options:
  1024*1024  — 1:1  square (default)
  1280*720   — 16:9 landscape
  720*1280   — 9:16 portrait
  1024*768   — 4:3  landscape
  768*1024   — 3:4  portrait

Environment variables:
  DASHSCOPE_API_KEY  DashScope API key (required)
  QWEN_IMAGE_MODEL   Model name (default: qwen-image-2.0)
""",
    )

    parser.add_argument(
        "--prompt",
        required=True,
        help="Image description / generation prompt",
    )
    parser.add_argument(
        "--size",
        default=DEFAULT_SIZE,
        choices=VALID_SIZES,
        help=f"Image size (default: {DEFAULT_SIZE})",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"Model name (default: env QWEN_IMAGE_MODEL or {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--ref",
        nargs="*",
        default=[],
        metavar="IMAGE",
        help="Optional reference image URLs or local paths (for editing/style transfer)",
    )

    return parser


def main() -> None:
    """Main entry point."""
    find_and_load_env()

    parser = create_argument_parser()
    args = parser.parse_args()

    api_key = get_api_key()
    model = args.model or os.environ.get("QWEN_IMAGE_MODEL", DEFAULT_MODEL)

    print()
    print("=" * 60)
    print("Image Generator Started")
    print("=" * 60)
    print(f"Model:  {model}")
    print(f"Output: {args.output}")
    print(f"Prompt: {args.prompt[:80]}{'...' if len(args.prompt) > 80 else ''}")
    if args.ref:
        print(f"Refs:   {args.ref}")
    print("=" * 60)
    print()

    # Generate
    print("Calling qwen-image-2.0...")
    image_urls = call_generate(api_key, args.prompt, model, args.ref or [])
    print(f"Received {len(image_urls)} image(s)")
    print()

    # Download and save
    saved_paths = []
    for i, url in enumerate(image_urls):
        path = download_image(url, args.output, index=i)
        saved_paths.append(path)

    print()
    print("=" * 60)
    print("Generation Complete!")
    print("=" * 60)
    for path in saved_paths:
        print(f"File: {path}")
    print()


if __name__ == "__main__":
    main()
