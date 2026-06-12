#!/usr/bin/env python3
"""Rights-hygiene guard: blocks non-original script text from entering the repo.

Canon's standing rule (PLAN.md): only rights-clean material, ever. Fixtures are
original work written for this repo; everything else must contain no screenplay
text. This guard enforces the mechanical half of that promise:

  RULE 1  Screenplay/document container formats (.fdx, .pdf, .docx, ...) are
          blocked everywhere — if it can't be diffed in review, it can carry
          script text into a public repo unseen.
  RULE 2  .fountain files are allowed only under fixtures/, and each must carry
          the "Original fixture material" credit line in its title page.
  RULE 3  Screenplay-formatted text (3+ column-0 sluglines, or a Final Draft
          XML marker) is blocked in any file outside fixtures/.
  RULE 4  .env files, .DS_Store, and Anthropic API key strings never commit.

Modes:
  --staged   check files staged for commit (pre-commit hook)
  --tree     check all tracked files in the working tree (CI)
  --history  content-scan every commit reachable from HEAD (CI backstop;
             catches text that was added then deleted within a push)

Stdlib only. Exit 0 = clean, exit 1 = violations (each printed with a fix hint).
"""

import re
import subprocess
import sys
from pathlib import PurePosixPath

BLOCKED_EXTENSIONS = {
    ".fdx", ".fdr", ".pdf", ".doc", ".docx", ".rtf",
    ".celtx", ".highland", ".fadein",
}
FIXTURES_PREFIX = "fixtures/"
CREDIT_MARKER = "original fixture material"
SLUGLINE = r"^(INT|EXT|EST|I/E|INT/EXT)[ ./]"
SLUGLINE_THRESHOLD = 3  # a real script has many; a doc citing one example passes
FDX_MARKER = "<" + "FinalDraft"  # concatenated so this file never matches itself
SECRET = r"sk-ant-[A-Za-z0-9_-]{8,}"


def run_git(*args):
    out = subprocess.run(
        ["git", *args], capture_output=True, text=True, errors="replace"
    )
    return out.returncode, out.stdout


def check_name(path):
    """RULE 1, 2 (location), 4 (junk names). Returns list of violations."""
    p = PurePosixPath(path)
    problems = []
    if p.suffix.lower() in BLOCKED_EXTENSIONS:
        problems.append(
            f"{path}: blocked format ({p.suffix}) — script/document containers "
            "can't be reviewed as diffs. Convert original material to .fountain "
            "under fixtures/; never commit third-party scripts in any format."
        )
    if p.suffix.lower() == ".fountain" and not path.startswith(FIXTURES_PREFIX):
        problems.append(
            f"{path}: .fountain outside fixtures/ — original test material "
            "belongs in fixtures/<world>/; real/working scripts must never "
            "be committed (use an ignored scratch dir, e.g. out/)."
        )
    if p.name == ".DS_Store":
        problems.append(f"{path}: Finder junk — remove (it is gitignored).")
    if p.name == ".env" or p.name.startswith(".env."):
        problems.append(f"{path}: env file — keys never commit (gitignored).")
    return problems


def check_content(path, text):
    """RULE 2 (attestation), 3, 4 (secrets). Returns list of violations."""
    problems = []
    if re.search(SECRET, text):
        problems.append(
            f"{path}: contains an Anthropic API key string — revoke the key "
            "and remove it before committing."
        )
    if path.startswith(FIXTURES_PREFIX):
        if path.endswith(".fountain") and CREDIT_MARKER not in text.lower():
            problems.append(
                f"{path}: fixture lacks the originality attestation. Add a "
                'title-page line like: Credit: Original fixture material '
                "written for the Canon AI test harness."
            )
        return problems
    slugs = sum(
        1 for line in text.splitlines() if re.match(SLUGLINE, line)
    )
    what = (
        f"screenplay-formatted text ({slugs} sluglines)"
        if slugs >= SLUGLINE_THRESHOLD
        else "Final Draft XML" if FDX_MARKER in text else None
    )
    if what:
        problems.append(
            f"{path}: {what} outside fixtures/ — if this is original "
            "fixture material move it there with a credit line; if it is "
            "third-party script text it must not enter the repo at all."
        )
    return problems


def is_binary(text):
    return "\x00" in text[:8192]


def check_staged():
    _, out = run_git(
        "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"
    )
    problems = []
    for path in filter(None, out.split("\0")):
        problems += check_name(path)
        _, blob = run_git("show", f":{path}")
        if not is_binary(blob):
            problems += check_content(path, blob)
    return problems


def check_tree():
    _, out = run_git("ls-files", "-z")
    problems = []
    for path in filter(None, out.split("\0")):
        problems += check_name(path)
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if not is_binary(text):
            problems += check_content(path, text)
    return problems


def check_history():
    """Content-scan every commit reachable from HEAD (needs full clone depth).

    Same thresholds as the tree scan, so a doc that passes pre-commit can
    never fail here. A hit means rewrite history — deleting the file in a
    later commit does not unpublish it.
    """
    _, out = run_git("rev-list", "HEAD")
    revs = out.split()
    problems = []
    rewrite = " — REWRITE history to remove; a later delete does not unpublish."
    for i in range(0, len(revs), 500):
        chunk = revs[i : i + 500]
        # screenplay text outside fixtures/, with the slugline threshold
        _, hits = run_git(
            "grep", "-cE", SLUGLINE, *chunk, "--", ":!fixtures"
        )
        for line in filter(None, hits.splitlines()):
            rev_path, _, count = line.rpartition(":")
            if int(count) >= SLUGLINE_THRESHOLD:
                problems.append(f"{rev_path}: screenplay text in history{rewrite}")
        for pattern, label in ((FDX_MARKER, "Final Draft XML"), (SECRET, "API key")):
            _, hits = run_git("grep", "-lE", pattern, *chunk)
            problems += [
                f"{line}: {label} in history{rewrite}"
                for line in filter(None, hits.splitlines())
            ]
    _, adds = run_git(
        "log", "--all", "--name-only", "--diff-filter=A", "--pretty=format:"
    )
    for path in sorted(set(filter(None, adds.splitlines()))):
        p = PurePosixPath(path)
        if p.suffix.lower() in BLOCKED_EXTENSIONS or (
            p.suffix.lower() == ".fountain"
            and not path.startswith(FIXTURES_PREFIX)
        ):
            problems.append(f"{path}: script container in history{rewrite}")
    return sorted(set(problems))


def main():
    modes = {
        "--staged": check_staged,
        "--tree": check_tree,
        "--history": check_history,
    }
    args = sys.argv[1:] or ["--tree"]
    unknown = [a for a in args if a not in modes]
    if unknown:
        sys.exit(f"rights_guard: unknown mode {unknown} (use {list(modes)})")
    problems = [p for a in args for p in modes[a]()]
    if problems:
        print("RIGHTS GUARD — commit blocked:\n", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}\n", file=sys.stderr)
        print(
            "Canon's rule: only rights-clean, original material enters this "
            "repo (PLAN.md standing rules).",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"rights guard: clean ({', '.join(a.lstrip('-') for a in args)})")


if __name__ == "__main__":
    main()
