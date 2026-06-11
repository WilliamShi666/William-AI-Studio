#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import os
from pathlib import Path
from typing import Iterable, List, Tuple

from pypdf import PdfReader


def utc_now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def is_pdf(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() == ".pdf"


def iter_pdfs(input_path: Path, recursive: bool, glob_pat: str) -> Iterable[Path]:
    if input_path.is_file():
        if is_pdf(input_path):
            yield input_path
        return

    if not input_path.is_dir():
        return

    if recursive:
        for root, _, files in os.walk(input_path):
            for f in files:
                if fnmatch.fnmatch(f, glob_pat) and f.lower().endswith(".pdf"):
                    yield Path(root) / f
    else:
        for f in input_path.iterdir():
            if f.is_file() and fnmatch.fnmatch(f.name, glob_pat) and f.suffix.lower() == ".pdf":
                yield f


def extract_text_pypdf(pdf_path: Path) -> Tuple[str, int]:
    """
    Returns (text, pages)
    """
    reader = PdfReader(str(pdf_path))
    pages = len(reader.pages)
    chunks: List[str] = []
    for i, page in enumerate(reader.pages):
        try:
            t = page.extract_text() or ""
        except Exception:
            t = ""
        t = normalize_text(t)
        if t.strip():
            chunks.append(f"\n\n<!-- page:{i+1} -->\n\n{t}".strip())
    text = "\n\n".join(chunks).strip() + "\n"
    return text, pages


def normalize_text(s: str) -> str:
    # Keep it conservative: preserve newlines; normalize Windows newlines
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # Remove excessive trailing spaces per line
    s = "\n".join(line.rstrip() for line in s.splitlines())
    # Collapse 3+ blank lines to 2
    while "\n\n\n" in s:
        s = s.replace("\n\n\n", "\n\n")
    return s


def quality_warnings(text: str, pages: int) -> List[str]:
    warnings: List[str] = []
    chars = len(text.strip())
    if chars == 0:
        warnings.append("No extractable text found. PDF might be scanned/image-only (OCR needed).")
        return warnings

    # Text density heuristic
    density = chars / max(pages, 1)
    if density < 800:
        warnings.append(f"Low text density ({int(density)} chars/page). Might be heavily formatted or partially scanned.")
    if "�" in text:
        warnings.append("Found replacement characters (�). Encoding/extraction quality may be degraded.")
    return warnings


def md_frontmatter(source_pdf: Path, pages: int, extracted_chars: int, method: str) -> str:
    # Simple YAML; keep stable keys
    return (
        "---\n"
        f"source_pdf: \"{str(source_pdf)}\"\n"
        f"pages: {pages}\n"
        f"extracted_chars: {extracted_chars}\n"
        f"extraction_method: \"{method}\"\n"
        f"timestamp_utc: \"{utc_now_iso()}\"\n"
        "---\n"
    )


def build_markdown(source_pdf: Path, pages: int, text: str, method: str) -> str:
    extracted_chars = len(text.strip())
    fm = md_frontmatter(source_pdf, pages, extracted_chars, method)

    body = text.strip()
    if not body:
        body = "_(No extractable text was found in this PDF. It may be scanned; OCR is required.)_"

    warns = quality_warnings(text, pages)
    report_lines = [
        "\n\n---\n",
        "## Extraction report\n",
        f"- Pages: **{pages}**\n",
        f"- Extracted chars: **{extracted_chars}**\n",
        f"- Method: **{method}**\n",
    ]
    if warns:
        report_lines.append("- Warnings:\n")
        for w in warns:
            report_lines.append(f"  - {w}\n")
    else:
        report_lines.append("- Warnings: _(none)_\n")

    return fm + "\n" + body + "".join(report_lines)


def ensure_out_path(input_pdf: Path, out: Path) -> Path:
    """
    If out is a directory, place <pdf_stem>.md inside it.
    If out is a file path ending with .md, use it (single input only).
    """
    if out.suffix.lower() == ".md":
        return out
    out.mkdir(parents=True, exist_ok=True)
    return out / (input_pdf.stem + ".md")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Convert resume PDFs to Markdown (.md). Supports single PDF or directory batch."
    )
    ap.add_argument("--input", required=True, help="PDF file path OR directory containing PDFs")
    ap.add_argument("--output", required=True, help="Output .md file path OR output directory")
    ap.add_argument("--recursive", action="store_true", help="Recursively scan input directory")
    ap.add_argument("--glob", default="*.pdf", help="Glob pattern when input is a directory (default: *.pdf)")
    args = ap.parse_args()

    in_path = Path(args.input).expanduser().resolve()
    out_path = Path(args.output).expanduser().resolve()

    pdfs = list(iter_pdfs(in_path, args.recursive, args.glob))
    if not pdfs:
        print(f"[pdf_to_md] No PDFs found at: {in_path}")
        return 2

    # If output is a single .md file but we have multiple inputs, refuse
    if out_path.suffix.lower() == ".md" and len(pdfs) > 1:
        print("[pdf_to_md] --output is a file, but multiple PDFs were found. Use an output directory instead.")
        return 2

    ok = 0
    for pdf in pdfs:
        try:
            text, pages = extract_text_pypdf(pdf)
            md = build_markdown(pdf, pages, text, method="pypdf.extract_text")
            target = ensure_out_path(pdf, out_path)
            write_text(target, md)
            print(f"[pdf_to_md] OK: {pdf} -> {target} (pages={pages}, chars={len(text.strip())})")
            ok += 1
        except Exception as e:
            print(f"[pdf_to_md] FAIL: {pdf} ({type(e).__name__}: {e})")

    if ok == 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())