#!/usr/bin/env python3
"""Validate a Roys Legion public demo dataset before release/import."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\((images/[^)]+)\)")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
    return rows


def validate_document(dataset_dir: Path, document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    file_id = document.get("file_id", "<missing>")
    for key in ("filename", "extraction_dir", "output_json", "chunk_file", "checksums"):
        if key not in document:
            errors.append(f"{file_id}: missing {key}")

    source_pdf = document.get("source_pdf")
    if source_pdf:
        pdf_path = dataset_dir / source_pdf
        if not pdf_path.is_file():
            errors.append(f"{file_id}: missing source_pdf {source_pdf}")
        elif document.get("checksums", {}).get("source_pdf_sha256") and sha256_file(pdf_path) != document["checksums"]["source_pdf_sha256"]:
            errors.append(f"{file_id}: source_pdf checksum mismatch")

    chunk_path = dataset_dir / document.get("chunk_file", "")
    if not chunk_path.is_file():
        errors.append(f"{file_id}: missing chunk_file {document.get('chunk_file')}")
        return errors

    chunks = read_jsonl(chunk_path)
    if not chunks:
        errors.append(f"{file_id}: chunk_file has no chunks")
    if document.get("chunk_count") is not None and len(chunks) != document["chunk_count"]:
        errors.append(f"{file_id}: chunk_count mismatch manifest={document['chunk_count']} actual={len(chunks)}")
    if document.get("checksums", {}).get("chunk_file_sha256") and sha256_file(chunk_path) != document["checksums"]["chunk_file_sha256"]:
        errors.append(f"{file_id}: chunk_file checksum mismatch")

    output_json_path = dataset_dir / document.get("output_json", "")
    if not output_json_path.is_file():
        errors.append(f"{file_id}: missing output_json {document.get('output_json')}")
    elif document.get("checksums", {}).get("output_json_sha256") and sha256_file(output_json_path) != document["checksums"]["output_json_sha256"]:
        errors.append(f"{file_id}: output_json checksum mismatch")

    image_dir = dataset_dir / document.get("image_dir", "")
    for index, chunk in enumerate(chunks):
        text = chunk.get("chunk_text") or chunk.get("text") or chunk.get("content")
        if not text:
            errors.append(f"{file_id}: chunk {index} has no text")
            continue
        for match in IMAGE_PATTERN.findall(text):
            image_path = image_dir / Path(match).name
            if not image_path.is_file():
                errors.append(f"{file_id}: chunk {index} references missing image {match}")

    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()

    manifest_path = args.manifest.resolve()
    dataset_dir = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    documents = manifest.get("documents", [])
    if not documents:
        errors.append("manifest has no documents")
    for document in documents:
        errors.extend(validate_document(dataset_dir, document))

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        raise SystemExit(1)

    print(f"Validated {len(documents)} documents from {manifest_path}")


if __name__ == "__main__":
    main()
