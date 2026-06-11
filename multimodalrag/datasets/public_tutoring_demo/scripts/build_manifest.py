#!/usr/bin/env python3
"""Build a draft manifest for the Roys Legion public demo dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_manifest(dataset_dir: Path) -> dict[str, Any]:
    uploads_dir = dataset_dir / "uploads"
    extraction_dir = dataset_dir / "extraction_results"
    chunks_dir = dataset_dir / "chunks"

    documents: list[dict[str, Any]] = []
    for chunk_file in sorted(chunks_dir.glob("*.jsonl")):
        file_id = chunk_file.stem
        source_candidates = sorted(uploads_dir.glob(f"{file_id}.*"))
        source_pdf = source_candidates[0] if source_candidates else None
        document_extraction_dir = extraction_dir / file_id
        output_candidates = sorted(document_extraction_dir.glob("*_chunked_output.json"))
        output_json = output_candidates[0] if output_candidates else None

        checksums: dict[str, str] = {"chunk_file_sha256": sha256_file(chunk_file)}
        if source_pdf and source_pdf.is_file():
            checksums["source_pdf_sha256"] = sha256_file(source_pdf)
        if output_json and output_json.is_file():
            checksums["output_json_sha256"] = sha256_file(output_json)

        result_key = ""
        chunk_count = 0
        ocr_engine = "TBD"
        if output_json and output_json.is_file():
            output_data = json.loads(output_json.read_text(encoding="utf-8"))
            results = output_data.get("results", {})
            if results:
                result_key = next(iter(results))
                chunk_count = len(results[result_key].get("chunks", []))
            ocr_engine = str(output_data.get("backend", "TBD"))

        documents.append(
            {
                "file_id": file_id,
                "filename": source_pdf.name if source_pdf else f"{file_id}.pdf",
                "subject": "TBD",
                "course": "TBD",
                "source_pdf": str(source_pdf.relative_to(dataset_dir)) if source_pdf else "",
                "extraction_dir": str(document_extraction_dir.relative_to(dataset_dir)),
                "output_json": str(output_json.relative_to(dataset_dir)) if output_json else "",
                "result_key": result_key,
                "chunk_file": str(chunk_file.relative_to(dataset_dir)),
                "ocr_engine": ocr_engine,
                "image_dir": str((document_extraction_dir / "images").relative_to(dataset_dir)),
                "chunk_count": chunk_count,
                "provenance": {
                    "source": "TBD",
                    "public_release_approved": False,
                    "notes": "Review before release.",
                },
                "checksums": checksums,
            }
        )

    return {
        "dataset_name": "Roys Legion Demo Dataset",
        "version": "0.1.0-draft",
        "description": "Public demo dataset for Roys Legion multidisciplinary visual tutoring.",
        "license": "TBD",
        "documents": documents,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", default=Path(__file__).resolve().parents[1], type=Path)
    parser.add_argument("--output", default="manifest.json")
    args = parser.parse_args()

    dataset_dir = args.dataset_dir.resolve()
    manifest = build_manifest(dataset_dir)
    output_path = dataset_dir / args.output
    output_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
