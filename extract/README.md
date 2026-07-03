# P0-EXTRACT

Standalone extraction workstream for Canon AI Phase 0.

This directory is JSON-in / JSON-out. It does not parse scripts, write to
Postgres, create migrations, or build UI.

## Input Scene JSON

Accepted shapes:

```json
{
  "world": "greyharbor",
  "scenes": [
    {
      "scene_id": "E101/sc1",
      "work_title": "E101 - The Ledger",
      "scene_index": 1,
      "slug": "INT. HARBORMASTER'S OFFICE - DAY",
      "story_position": 1,
      "is_flashback": false,
      "raw_text": "INT. HARBORMASTER'S OFFICE - DAY\n\n..."
    }
  ]
}
```

`slug`, `story_position`, `is_flashback`, and `raw_text` are required. `scene_id`
is strongly preferred because it is the citation handle before database IDs
exist.

## Commands

Dry-run prompts, with no API call:

```sh
python -m extract extract extract/examples/greyharbor_ep101_ep102_scenes.json --dry-run --limit 1
```

Run LLM extraction:

```sh
python -m extract extract extract/examples/greyharbor_ep101_ep102_scenes.json \
  --world greyharbor \
  --out out/candidates.json
```

Resolve entities from candidate assertions:

```sh
python -m extract resolve out/candidates.json \
  --aliases extract/examples/greyharbor_aliases.json \
  --state out/extract-state.json \
  --out out/resolved.json
```

Apply the confidence gate:

```sh
python -m extract gate \
  --state out/extract-state.json \
  --out out/resolved-gated.json \
  --eval-out out/assertions.json
```

Review human queues:

```sh
python -m extract confirm-entities --state out/extract-state.json --out out/resolved.json
python -m extract confirm-assertions --state out/extract-state.json --out out/resolved.json
```

Human-only merge:

```sh
python -m extract merge --state out/extract-state.json --keep "Marcus Hale" --drop "MARCUS" --reason "same cast-listed character"
```

## Status Rules

- candidate extraction drops any assertion without a verbatim scene quote.
- predicates are a closed set from `docs/extraction.md`.
- `confidence >= 0.85` becomes `draft`.
- lower-confidence assertions become `needs_confirmation` queue items.
- accepted/edited queue items become `canon`; rejected items are excluded from
  eval output.
- entity disambiguation may assign one referring expression to one existing
  entity, but it never merges two existing entities. Merges only happen through
  the human CLI.
