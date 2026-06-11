#!/usr/bin/env python3
"""Export a portable Roys Legion dataset from backend output directories."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


COPY_EXTRACTION_PATTERNS = (
    "*_metadata.json",
    "*_chunked_output.json",
    "*_extraction.md",
    "*_content_list.json",
    "*_middle_json.json",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def find_chunked_output(document_dir: Path) -> Path | None:
    candidates = sorted(document_dir.glob("*_chunked_output.json"))
    return candidates[0] if candidates else None


def find_metadata(document_dir: Path) -> Path | None:
    candidates = sorted(document_dir.glob("*_metadata.json"))
    return candidates[0] if candidates else None


def find_source_pdf(uploads_dir: Path, file_id: str, filename: str | None) -> Path | None:
    matches = sorted(uploads_dir.rglob(f"*{file_id}*.pdf"))
    if matches:
        return matches[0]
    if filename:
        matches = sorted(uploads_dir.rglob(filename))
        if matches:
            return matches[0]
    return None


def result_key_and_chunks(output_json: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    results = output_json.get("results")
    if not isinstance(results, dict) or not results:
        raise ValueError("chunked output has no results")
    result_key = next(iter(results))
    result = results[result_key]
    chunks = result.get("chunks")
    if not isinstance(chunks, list):
        raise ValueError(f"result {result_key!r} has no chunks list")
    return result_key, chunks


def normalize_chunk(
    *,
    file_id: str,
    filename: str,
    subject: str,
    course: str,
    index: int,
    chunk: dict[str, Any],
) -> dict[str, Any]:
    text = chunk.get("text") or chunk.get("chunk_text") or chunk.get("content")
    if not text:
        raise ValueError(f"chunk {index} for {file_id} has no text")
    metadata = {
        "file_id": file_id,
        "filename": filename,
        "subject": subject,
        "course": course,
        "chunk_index": index,
        "page_start": chunk.get("page_start"),
        "page_end": chunk.get("page_end"),
        "pages": chunk.get("pages", []),
        "continued": chunk.get("continued", False),
        "cross_page_bridge": chunk.get("cross_page_bridge", False),
        "is_table_like": chunk.get("is_table_like", False),
        "headers": chunk.get("headers", {}),
    }
    if isinstance(chunk.get("images"), list):
        metadata["images"] = chunk["images"]
    return {
        "chunk_text": text,
        "file_id": file_id,
        "filename": filename,
        "metadata": metadata,
    }


def copy_document_files(source_dir: Path, target_dir: Path) -> list[str]:
    target_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for pattern in COPY_EXTRACTION_PATTERNS:
        for source in sorted(source_dir.glob(pattern)):
            target = target_dir / source.name
            shutil.copy2(source, target)
            copied.append(target.name)
    image_source_dir = source_dir / "images"
    if image_source_dir.is_dir():
        image_target_dir = target_dir / "images"
        image_target_dir.mkdir(parents=True, exist_ok=True)
        for source in sorted(image_source_dir.iterdir()):
            if source.is_file():
                shutil.copy2(source, image_target_dir / source.name)
    return copied


def export_dataset(args: argparse.Namespace) -> dict[str, Any]:
    extraction_source = args.extraction_results_dir.resolve()
    uploads_source = args.uploads_dir.resolve()
    dataset_dir = args.dataset_dir.resolve()
    uploads_target = dataset_dir / "uploads"
    extraction_target = dataset_dir / "extraction_results"
    chunks_target = dataset_dir / "chunks"
    uploads_target.mkdir(parents=True, exist_ok=True)
    extraction_target.mkdir(parents=True, exist_ok=True)
    chunks_target.mkdir(parents=True, exist_ok=True)

    selected_ids = set(args.file_id or [])
    document_dirs = sorted(path for path in extraction_source.iterdir() if path.is_dir())
    documents: list[dict[str, Any]] = []

    for document_dir in document_dirs:
        file_id = document_dir.name
        if selected_ids and file_id not in selected_ids:
            continue
        if args.limit is not None and len(documents) >= args.limit:
            break

        chunked_output = find_chunked_output(document_dir)
        metadata_path = find_metadata(document_dir)
        if not chunked_output or not metadata_path:
            continue

        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        output_json = json.loads(chunked_output.read_text(encoding="utf-8"))
        result_key, chunks = result_key_and_chunks(output_json)
        filename = metadata.get("filename") or f"{result_key}.pdf"
        source_pdf = find_source_pdf(uploads_source, file_id, filename)
        if not source_pdf:
            if args.require_pdf:
                raise FileNotFoundError(f"No source PDF found for {file_id} ({filename})")
            source_pdf_relative = ""
        else:
            source_pdf_target = uploads_target / source_pdf.name
            shutil.copy2(source_pdf, source_pdf_target)
            source_pdf_relative = source_pdf_target.relative_to(dataset_dir).as_posix()

        document_target_dir = extraction_target / file_id
        copied_files = copy_document_files(document_dir, document_target_dir)
        chunk_file = chunks_target / f"{file_id}.jsonl"
        with chunk_file.open("w", encoding="utf-8") as handle:
            for index, chunk in enumerate(chunks):
                row = normalize_chunk(
                    file_id=file_id,
                    filename=filename,
                    subject=args.subject,
                    course=args.course,
                    index=index,
                    chunk=chunk,
                )
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

        output_json_relative = (
            document_target_dir / chunked_output.name
        ).relative_to(dataset_dir).as_posix()
        checksums = {
            "chunk_file_sha256": sha256_file(chunk_file),
            "output_json_sha256": sha256_file(document_target_dir / chunked_output.name),
        }
        if source_pdf:
            checksums["source_pdf_sha256"] = sha256_file(uploads_target / source_pdf.name)

        documents.append(
            {
                "file_id": file_id,
                "filename": filename,
                "subject": args.subject,
                "course": args.course,
                "source_pdf": source_pdf_relative,
                "extraction_dir": document_target_dir.relative_to(dataset_dir).as_posix(),
                "output_json": output_json_relative,
                "result_key": result_key,
                "chunk_file": chunk_file.relative_to(dataset_dir).as_posix(),
                "ocr_engine": metadata.get("extraction_mode", "unknown"),
                "image_dir": (document_target_dir / "images").relative_to(dataset_dir).as_posix(),
                "chunk_count": len(chunks),
                "copied_extraction_files": copied_files,
                "provenance": {
                    "source": args.provenance_source,
                    "public_release_approved": args.public_release_approved,
                    "notes": args.provenance_notes,
                },
                "checksums": checksums,
            }
        )

    return {
        "dataset_name": "Roys Legion Demo Dataset",
        "version": args.version,
        "description": "Public demo dataset for Roys Legion multidisciplinary visual tutoring.",
        "license": args.license,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "uploads_dir": str(uploads_source),
            "extraction_results_dir": str(extraction_source),
        },
        "documents": documents,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uploads-dir", required=True, type=Path)
    parser.add_argument("--extraction-results-dir", required=True, type=Path)
    parser.add_argument("--dataset-dir", default=Path(__file__).resolve().parents[1], type=Path)
    parser.add_argument("--output", default="manifest.json")
    parser.add_argument("--file-id", action="append")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--subject", default="TBD")
    parser.add_argument("--course", default="TBD")
    parser.add_argument("--version", default="0.1.0-draft")
    parser.add_argument("--license", default="Apache-2.0")
    parser.add_argument("--provenance-source", default="Owner-approved public tutoring materials")
    parser.add_argument("--provenance-notes", default="Review provenance before final release.")
    parser.add_argument("--public-release-approved", action="store_true")
    parser.add_argument("--require-pdf", action="store_true")
    args = parser.parse_args()

    manifest = export_dataset(args)
    output_path = args.dataset_dir.resolve() / args.output
    output_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {output_path}")
    print(f"Documents: {len(manifest['documents'])}")


if __name__ == "__main__":
    main()
