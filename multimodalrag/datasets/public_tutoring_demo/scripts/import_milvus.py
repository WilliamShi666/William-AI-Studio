#!/usr/bin/env python3
"""Import Roys Legion public demo documents through the Milvus API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib import request


def post_json(url: str, payload: dict[str, Any], token: str | None) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = request.Request(url, data=body, headers=headers, method="POST")
    with request.urlopen(req, timeout=120) as response:
        response_body = response.read().decode("utf-8")
    return json.loads(response_body) if response_body else {}


def load_output_json(dataset_dir: Path, document: dict[str, Any]) -> dict[str, Any]:
    output_json_path = document.get("output_json")
    if not output_json_path:
        raise ValueError(f"Document {document['file_id']} is missing output_json")
    path = dataset_dir / output_json_path
    if not path.is_file():
        raise FileNotFoundError(path)
    output_json = json.loads(path.read_text(encoding="utf-8"))
    if not output_json.get("results"):
        raise ValueError(f"{path} does not contain results")
    return output_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--milvus-api-url", default="http://localhost:8000")
    parser.add_argument("--collection-name", required=True)
    parser.add_argument("--token", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    manifest_path = args.manifest.resolve()
    dataset_dir = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    upload_url = args.milvus_api_url.rstrip("/") + "/upload_json/v2"

    for document in manifest.get("documents", []):
        output_json = load_output_json(dataset_dir, document)
        payload = {
            "collection_name": args.collection_name,
            "file_id": document["file_id"],
            "output_json": output_json,
            "result_key": document.get("result_key"),
        }
        if args.dry_run:
            chunks = next(iter(output_json["results"].values())).get("chunks", [])
            result = {"dry_run": True, "chunks_count": len(chunks)}
        else:
            result = post_json(upload_url, payload, args.token)
        print(json.dumps({"file_id": document["file_id"], "result": result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
