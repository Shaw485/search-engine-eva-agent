# Project collaboration instructions

Before working on this project, read `CLAUDE.md`, `ROADMAP.md`, and the relevant
section of `docs/LEARNING_CHECKPOINTS.md`.

## Mandatory learning reminders

The project owner wants to learn the critical product, search, evaluation, and
Agent Harness concepts while building the project.

Before entering a new roadmap stage or making a decision that depends on a
critical concept:

1. Identify the concept before implementation continues.
2. Tell the user why it matters to the current deliverable.
3. Define the minimum knowledge they need, without turning it into a long course.
4. Give one small example, exercise, or check question.
5. Confirm understanding when misunderstanding would invalidate the experiment
   or product decision.

Do not interrupt the user for routine syntax, generated boilerplate, package
installation details, or implementation mechanics that do not affect their
product judgment. Record completed learning checkpoints in
`docs/LEARNING_CHECKPOINTS.md` when they materially affect project progress.

## Decision and contribution provenance

The project owner needs interview-safe evidence that distinguishes their work
from Codex's work. For every material product, evaluation, data, architecture,
or release decision, update `docs/CONTRIBUTION_LOG.md` and record separately:

1. who raised the requirement or problem;
2. who proposed the selected solution;
3. who made or approved the final decision;
4. who implemented it;
5. who validated it and what evidence exists.

Do not convert approval into authorship. If Codex proposed an option and the
owner replied with approval, record it as **Codex-proposed, owner-approved**,
not as an owner-originated solution. Likewise, permission to install, deploy,
or commit is authorization, not a technical design contribution.

Attribute generated code, tests, reports, and routine implementation work to
Codex unless there is direct evidence that the owner wrote or independently
designed them. Record owner questions and challenges as review contributions,
but do not relabel them as decisions unless they changed or selected an outcome.
Never infer owner authorship from Git author metadata.

## Development logging and independent diagnostics

The owner requires practical diagnostics as part of implementation and
acceptance, not as optional follow-up work. When a subsystem is created or
substantially changed:

1. Use the `search_quality.<module>` logger namespaces documented in
   `docs/LOGGING.md`; each runtime area must be independently configurable and
   filterable.
2. Emit structured, low-noise boundary events with UTC time, stable event name,
   trace/request/Run identifiers, non-sensitive operation context, duration and
   actionable error type.
3. Keep per-Query or similarly verbose events at `DEBUG`; production defaults
   must not expose them.
4. Never log raw Query text, titles/descriptions, vectors, payloads, passwords,
   tokens, secrets, authorization/cookie headers or unnecessary personal data.
5. Keep deterministic experiment evidence separate from nondeterministic
   execution diagnostics. Trace IDs, timestamps and wall-clock duration must
   not change semantic Run identity.
6. Document enable/disable/filter/export/retention and independent reproduction
   steps, and test representative success, failure, redaction and module-control
   paths.

Persistent application log files require explicit rotation and retention. The
current CLI/API design emits to stderr; production retention belongs to
journald, while Run JSON remains the immutable experiment record.
