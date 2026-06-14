"""Braintrust layer — the shareable dashboard over the vendor-neutral scores.

Each planted error / trap is a CASE; each system (Canon, a chatbot run) is an
EXPERIMENT in one Braintrust project, so they line up side-by-side with a per-case
green/red table — exactly the artifact a blog post wants. Custom scorers report
`recall` (on planted errors) and `trap_respect` (on traps) separately.

Pure visualization: every number comes from bench/scorer.evaluate(); nothing is
re-judged here. Requires BRAINTRUST_API_KEY in the environment.
"""

from __future__ import annotations

DEFAULT_PROJECT = "canon-vs-chatbot"


# --- custom scorers (Braintrust uses the function name as the score name) ---

def recall(input, output, expected):
    """1 if a planted error was flagged; not applicable (None) to traps."""
    if input.get("kind") != "planted":
        return None
    return 1.0 if output.get("flagged") else 0.0


def trap_respect(input, output, expected):
    """1 if a trap was correctly NOT flagged; not applicable (None) to planted errors."""
    if input.get("kind") != "trap":
        return None
    return 0.0 if output.get("flagged") else 1.0


def push(system_name: str, evaluation: dict, *, project: str = DEFAULT_PROJECT,
         extra_metadata: dict | None = None):
    """Log one system's per-case verdicts to Braintrust as an experiment."""
    import braintrust

    by_id = {c["id"]: c for c in evaluation["cases"]}
    data = [
        {
            "input": {"id": c["id"], "kind": c["kind"], "check": c["check"],
                      "where": c.get("where"), "must_mention": c.get("must_mention"),
                      "description": c.get("description")},
            "expected": c["expected"],
            "metadata": {"kind": c["kind"], "check": c["check"]},
        }
        for c in evaluation["cases"]
    ]

    def task(inp):
        return {"flagged": by_id[inp["id"]]["flagged"]}

    metadata = dict(evaluation["summary"])
    metadata["system"] = system_name
    if extra_metadata:
        metadata.update(extra_metadata)

    return braintrust.Eval(
        project,
        data=data,
        task=task,
        scores=[recall, trap_respect],
        experiment_name=system_name,
        metadata=metadata,
    )
