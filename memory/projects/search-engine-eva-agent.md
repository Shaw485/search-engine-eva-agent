# Search Engine EVA Agent

**Status:** Active  
**Roadmap position:** Stage 2 Search Evaluation Harness in progress; Stage 1
owner checkpoint pending
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

## Decision provenance preference

The owner explicitly requires interview-safe separation between owner work and
Codex work. For material decisions, record requirement origin, proposal source,
final decision maker, implementation owner, and validator separately in
`docs/CONTRIBUTION_LOG.md`.

An owner approval such as “可以” means the owner made the adoption decision; it
does not mean the owner originated the proposal. Installation, deployment, and
commit permission are authorizations rather than architecture contributions.
Generated code and tests remain attributed to Codex unless direct evidence says
otherwise.

The owner also explicitly requires per-module development logging and
independent diagnostics. New or substantially changed runtime paths must use
structured traceable events, safe redaction, production low-noise defaults and
documented enable/filter/retention procedures from `docs/LOGGING.md`.

## Current engineering state

- Stage 0 mandatory local path accepted on 2026-08-25.
- Local BM25 and exact cosine search implement the shared backend contract.
- OpenSearch 3.8.0 support is implemented but live Docker verification is pending.
- Stage 1 validated the pinned official ESCI sources and built deterministic
  English-US train/dev/frozen-test data plus a 20-Query smoke view on 2026-08-26.
- Stage 1 aggregate evidence lives in `docs/STAGE_1_REPORT.md` and
  `data/manifests/esci-stage1.json`; large raw and processed data stay ignored.
- Stage 2 now includes hand-verified nDCG, MRR, and Success metrics; the
  versioned `esci-primary-v1` relevance policy; a shared label-blind Ranker
  contract; and deterministic fixed-seed random, keyword-overlap and title-BM25
  comparators on the 20-Query smoke profile.
- Module-level structured diagnostics cover data, evaluation, ranking, backend
  and API boundaries. The shared formal evaluation entry point rejects the
  500-Query dev profile before file access while the Owner data-boundary
  checkpoint is pending.
- The Stage 2 smoke result is nDCG@5 0.659606, nDCG@10 0.719098, MRR@10
  0.851667, Success@1 0.75, and Success@5 1.0. It is a judged-candidate rerank,
  not the separate 482,105-product exploratory full-corpus search.
- The Stage 1 owner learning checkpoint remains pending until the Query leakage
  and incomplete-judgment question in `docs/LEARNING_CHECKPOINTS.md` is answered.
- Do not promote the smoke run to the full dev baseline, open the frozen test,
  or begin later ranking/Agent claims before the relevant owner checkpoints are
  complete.
