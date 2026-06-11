"""Idempotently create Langfuse Score Configs for every score in PHASE1_SCORE_REGISTRY.

Run once per environment (local-dev / staging / production):

    cd WilliamManus/backend
    uv run python scripts/bootstrap_langfuse_score_configs.py

First run prints "+" for each created config; subsequent runs print "=" for each
existing config (idempotent by name — does not update existing configs).

IMPORTANT — Managed Evaluator name alignment:
  If you enable Langfuse's built-in Managed Evaluators (Toxicity, Hallucination),
  they write scores under their own fixed names (typically "toxicity" / "hallucination"),
  NOT under "toxic_output_detected" / "groundedness" from this registry.
  Either rename the evaluator's output score in the Langfuse UI to match the registry name,
  or add an alias mapping in services/langfuse.py::filter_registered_scores.
"""

import sys
import os

# Allow running from backend/ root without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langfuse.api.resources.score_configs.types import CreateScoreConfigRequest
from services.langfuse import enabled, langfuse, PHASE1_SCORE_REGISTRY


def main() -> None:
    if not enabled:
        raise SystemExit(
            "Langfuse is not configured (missing LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY). "
            "Set them in your .env and retry."
        )

    print(f"Fetching existing score configs from Langfuse …")
    existing_page = langfuse.api.score_configs.get(limit=100)
    existing: dict[str, str] = {cfg.name: cfg.id for cfg in existing_page.data}
    print(f"  {len(existing)} existing config(s) found.\n")

    created = 0
    skipped = 0
    for score_name, spec in PHASE1_SCORE_REGISTRY.items():
        if score_name in existing:
            print(f"  = {score_name}  (id={existing[score_name]})")
            skipped += 1
            continue
        try:
            cfg = langfuse.api.score_configs.create(
                request=CreateScoreConfigRequest(
                    name=score_name,
                    data_type="NUMERIC",
                    min_value=float(spec["min"]),
                    max_value=float(spec["max"]),
                    description=f"Phase 1 / v4 metric: {score_name}",
                )
            )
            print(f"  + {score_name}  (id={cfg.id})")
            created += 1
        except Exception as exc:
            print(f"  ! {score_name}  ERROR: {exc}", file=sys.stderr)

    print(f"\nDone. Created {created}, skipped {skipped} (already existed).")
    if created == 0 and skipped == len(PHASE1_SCORE_REGISTRY):
        print("All configs are already in sync — nothing to do.")


if __name__ == "__main__":
    main()
