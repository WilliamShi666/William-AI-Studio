#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    meta_block = parts[1]
    metadata: dict = {}
    for line in meta_block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip('"').strip("'")
    return metadata


def discover_skills(source_dir: Path) -> list[Path]:
    skills = []
    for entry in sorted(source_dir.iterdir()):
        try:
            if entry.is_dir() and (entry / "SKILL.md").exists():
                skills.append(entry)
        except PermissionError:
            # Skip directories we can't access inside the sandbox.
            continue
    return skills


def prepare_destination(dest_dir: Path, force: bool) -> bool:
    if dest_dir.exists():
        if not force:
            return False
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    return True


def copy_skills(skills: list[Path], dest_dir: Path) -> None:
    for skill_dir in skills:
        target = dest_dir / skill_dir.name
        shutil.copytree(skill_dir, target, dirs_exist_ok=True)


def symlink_skills(skills: list[Path], dest_dir: Path) -> None:
    for skill_dir in skills:
        target = dest_dir / skill_dir.name
        if target.exists() or target.is_symlink():
            target.unlink()
        os.symlink(skill_dir, target)


def build_metadata(dest_dir: Path) -> dict:
    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "skills": [],
    }
    for skill_dir in discover_skills(dest_dir):
        skill_md = skill_dir / "SKILL.md"
        content = skill_md.read_text(encoding="utf-8", errors="ignore")
        front = parse_frontmatter(content)
        metadata["skills"].append(
            {
                "dir_name": skill_dir.name,
                "name": front.get("name", skill_dir.name),
                "description": front.get("description", ""),
                "path": str(skill_dir),
            }
        )
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare Claude Skills in a sandbox.")
    parser.add_argument(
        "--source",
        default=os.environ.get("CLAUDE_SKILLS_DIR", "/opt/claude_skills"),
        help="Source skills directory (default: CLAUDE_SKILLS_DIR or /opt/claude_skills)",
    )
    parser.add_argument(
        "--dest",
        default="/workspace/skills",
        help="Destination directory inside the sandbox (default: /workspace/skills)",
    )
    parser.add_argument(
        "--mode",
        choices=["copy", "symlink"],
        default="copy",
        help="How to place skills into /workspace (default: copy)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Remove destination before populating",
    )
    args = parser.parse_args()

    source_dir = Path(args.source)
    dest_dir = Path(args.dest)

    if not source_dir.exists():
        print(f"Source not found: {source_dir}", file=sys.stderr)
        return 2

    skills = discover_skills(source_dir)
    if not skills:
        print(f"No skills found under: {source_dir}", file=sys.stderr)
        return 3

    refreshed = prepare_destination(dest_dir, args.force)
    if refreshed:
        if args.mode == "copy":
            copy_skills(skills, dest_dir)
        else:
            symlink_skills(skills, dest_dir)
    else:
        print(f"Destination exists, skipping {args.mode}. Use --force to refresh.")

    metadata = build_metadata(dest_dir)
    metadata_path = dest_dir / "skills_metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    print(f"Skills ready at: {dest_dir}")
    print(f"Metadata written: {metadata_path}")
    print(f"Skill count: {len(metadata['skills'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
