# Design System — Canon · "The Continuity Desk"

## Product Context
- **What this is:** Continuity/canon verification engine for serialized fiction — reads
  scripts, builds a cited fact graph, flags contradictions. Never generates story.
- **Who it's for:** Professional and aspiring serialized-fiction writers; AI-wary,
  post-2023-WGA. They pay humans $75–150/script for coverage today.
- **Space:** Writing tools (Final Draft, Highland, Scrivener; Sudowrite/NovelCrafter on
  the AI side). Out-of-category reference: Linear (instrument discipline).
- **Project type:** Web app (writer tool) + marketing landing. $20/mo, first script free.
- **The memorable thing (north star):** a writer's first reaction is **"It caught that."**
  Uncanny, cited competence. Every design decision serves it.

## Aesthetic Direction
- **Direction:** Evidence-editorial — "The Continuity Desk." A script supervisor's desk:
  unbleached paper, typewriter ink, pencil marks. Forensic, literary, quietly obsessive.
- **Decoration level:** Intentional — paper tone, hairline rules, pencil/stamp semantics.
  ZERO gradients, zero glow, zero decorative shadows, zero dark-SaaS chrome.
- **Mood:** The tool of the one crew member whose job is noticing. The machine's voice is
  a red pencil, never a chat bubble.
- **Rationale:** every AI writing tool wears warm-paper to hide that it generates; Canon
  is the one tool where the costume is true. Ratified by two independent designers
  converging blind (consultation, 2026-07-03).

## Typography (all free; self-hosted woff2 in ui/static/fonts/)
- **Display/Hero:** Fraunces (600/700, tight, ink-black) — the voice of canon itself.
- **Body/UI:** Instrument Sans (400/500/600) — deliberately anonymous; the interface
  self-erases because the writer is the only voice that matters.
- **Evidence:** Courier Prime — EVERY quote, citation, counter, script line, stat.
  Commissioned by screenwriters; the writer's native face. **A citation in any other
  face is a bug.**
- **Data/tables:** Courier Prime.
- **Loading:** self-hosted woff2 (privacy: no Google requests from a tool holding
  unproduced scripts), `font-display: swap`; fallbacks Georgia / Helvetica Neue /
  Courier New.
- **Scale:** display clamp(40→64px) marketing hero · 28px page titles · 16px body ·
  14px evidence mono · 12px meta/kickers (Courier, letterspaced, uppercase).

## Color
- **Approach:** Restrained — one accent + semantic pencils. Severity is pencil pressure,
  not traffic lights.
- **Neutrals:** bg #F2EFE7 (unbleached paper) · surface #FBFAF4 (fresh page) ·
  text #181510 (typewriter ink) · muted #6F675A (graphite) · hairline #DDD7C9.
- **Accent #C43B22 (red grease pencil): ONLY where Canon caught something.** Never
  decorative, never nav, never buttons. The system's sharpest law.
- **Semantic:** critical #A81F11 (pressed hard) · warning #B06E10 (china marker) ·
  note #41708F (non-photo blue — the pencil that doesn't print; dashed borders) ·
  sealed/stet #5C4632 (wax stamp) · success = plain ink.
- **Product language:** sealing = **STET** ("let it stand"), one-time tooltip. Confirm
  queue keeps Confirm/Reject.
- **Dark mode:** NONE at launch, by decision. Post-launch dark is a redesign, never
  inverted paper.

## Spacing
- **Base unit:** 4px. **Density:** comfortable marketing, compact report/queue (a writer
  triaging 70 findings gets density as respect).
- **Scale:** 2xs(2) xs(4) sm(8) md(16) lg(24) xl(32) 2xl(48) 3xl(64).

## Layout
- **Approach:** Hybrid. Marketing = poster-editorial (the document IS the hero; no
  browser-frame screenshots anywhere). App = grid-disciplined (hard left margin like a
  binder; index cards; keyboard-first).
- **Max content width:** 1200px marketing; app views keep current widths.
- **Border radius:** 0–2px. Documents are not rounded. No pills, no bubbles.
- **Buttons:** rectangular; primary ink fill, secondary ink outline, stet wax outline.
  Red is never a button color.

## Motion
- **Approach:** minimal-functional; enter ease-out, exit ease-in; micro 100ms, short
  150–250ms. **Nothing bounces, ever.**
- **The one signature moment:** landing/ingest theater — a thin red rule reads down the
  page, the Courier fact-counter ticks, one red circle blooms around the first catch.
  The only expressive motion in the product.

## Voice & copy (design-adjacent law)
- Findings: summary → evidence → citations; coordinator register, no hedging
  (docs/readers-report.md phrasing contract).
- Marketing leads with the refusal: "This tool will never write a word of your story."
- Footer covenant everywhere: "Canon never writes story · every claim cites its source."

## Decisions Log
| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-07-03 | Initial system created | /design-consultation: 6-site research + blind second proposal converged; user ratified |
| 2026-07-03 | Red = caught-only | The accent must mean one thing for "It caught that." to land |
| 2026-07-03 | Light mode only at launch | Paper is the brand; dark is a later redesign |
| 2026-07-03 | STET as seal language | Professional-marks vocabulary; literate-insider signal |
