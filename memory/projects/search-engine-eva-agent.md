# Search Engine EVA Agent

**Status:** Active  
**Roadmap position:** Stage 1 technical gate passed; owner checkpoint pending
**Primary dataset:** Amazon Shopping Queries ESCI  
**Repository:** https://github.com/Shaw485/search-engine-eva-agent

## Product intent

Build a reproducible e-commerce search evaluation and diagnosis system. The
system compares lexical, vector, hybrid, and Cross-Encoder ranking, calculates
deterministic metrics, classifies bad cases, and lets a controlled Agent run
evidence-backed experiments with Trace and Replay.

## Owner learning preference

The owner explicitly requested prompt reminders whenever a critical concept must
be understood personally during the project.

The collaborator must:

- remind before the relevant implementation or decision;
- focus on minimum useful understanding rather than broad theory;
- connect the concept to the current project artifact;
- include a concrete example or small exercise;
- verify understanding when a mistake would invalidate results;
- avoid interrupting for boilerplate and routine syntax.

The canonical learning plan and completion log live in
`docs/LEARNING_CHECKPOINTS.md`.

## Current engineering state

- Stage 0 mandatory local path accepted on 2026-08-25.
- Local BM25 and exact cosine search implement the shared backend contract.
- OpenSearch 3.8.0 support is implemented but live Docker verification is pending.
- Stage 1 validated the pinned official ESCI sources and built deterministic
  English-US train/dev/frozen-test data plus a 20-Query smoke view on 2026-08-26.
- Stage 1 aggregate evidence lives in `docs/STAGE_1_REPORT.md` and
  `data/manifests/esci-stage1.json`; large raw and processed data stay ignored.
- The Stage 1 owner learning checkpoint remains pending until the Query leakage
  and incomplete-judgment question in `docs/LEARNING_CHECKPOINTS.md` is answered.
- Do not begin Stage 2 metrics, Web, semantic models, or Agent orchestration
  before that owner checkpoint is complete.
