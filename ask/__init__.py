"""Phase 0 Ask-the-Bible CLI package.

This package is intentionally isolated from the rest of the pipeline workstreams:
it reads the existing Postgres canon graph and returns only cited answers.
"""

from .engine import ask_question, build_plan, render_answer

__all__ = ["ask_question", "build_plan", "render_answer"]
