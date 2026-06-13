"""Canon AI — internal triage workbench (read-only state inspector).

A microscope for the engine, not a product shell. Browses entities, assertions,
scenes, and findings of a loaded world straight from Postgres, with every claim
and flag shown next to its citation. The only write it performs is seal/unseal of
a finding (the `seals` table). It never runs the pipeline and never generates
story content — see ui/README.md and the project's one rule.
"""
