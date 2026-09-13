---
name: token-optimization
description: Dual-processor token optimization and Heavy Mode batching protocol for the Chat Johnson repository. Use this skill for every code change, refactor, bug fix, feature build, audit, or review in this repo, and whenever the user mentions token budget, token burn, context size, heavy mode, batching, compression, texturization, or asks for fewer, denser, or complete-in-one-pass edits. Apply it even when the user does not name it: it is the house style for how this repository is read, edited, and reported on.
---

# Token optimization · targeted reads, batched rewrites, compact context

The operator runs this project on free-tier budgets and pays for every token twice: once when
the model reads and once when it writes. Recursive re-reading of the tree, dribbles of tiny
edits that each need a re-read, and verbose narration are the three leaks. This skill closes
them by trading instant response for one dense, calculated pass per turn, without giving up
any depth of the logic or the architecture.

## 1. Targeted reads, never sweeps

Why: the repository map already tells you where everything lives. Re-discovering it burns the
input budget and adds nothing.

- Start from `references/repo-map.md`. It names each module, its responsibility, its public
  symbols, and the dependency ripple (what else must change when a file changes). Read only the
  files that the change actually touches, and only the regions you will modify: use line-range
  reads (`sed -n`, `Read` with offset/limit) or a targeted `grep -n` for the symbol, not whole
  files, and never `find`/`ls -R`/`cat` over the tree.
- A broad sweep is allowed only when the user explicitly asks for one ("audit everything",
  "map the repo") or the map is provably stale (a symbol it names is missing). When the map is
  stale, refresh it with `scripts/refresh_repo_map.py` and commit the map with the change.
- Before writing, map the full ripple internally: every caller, every test, every doc line
  that names the thing you are changing. Read those specific spots once. The goal is that no
  file is read twice in a turn.

## 2. Heavy Mode batching: one complete rewrite per file

Why: each incremental edit costs a re-read to verify and a re-run to check. Ten small patches
to one file cost far more than one correct rewrite of it.

- Plan the entire change set first, in your head or in a short private outline: which files,
  which functions, which signatures ripple where, which tests change. Resolve conflicts
  between those plans before any write. This is the "extra processing cycles": spend them on
  reasoning and self-correction, not on exploratory tool calls.
- Then write each affected file exactly once, complete and untruncated. For a module, emit the
  whole module. For a very large file (roughly 800+ lines) where a whole rewrite would risk
  truncation, rewrite by whole function or whole section boundaries, still in a single pass per
  file, never by scattered one-line edits.
- No placeholders, no `TODO`, no `pass` stubs, no "rest unchanged" comments. If a function is
  in the output, it is finished.
- One verification pass after the batch, not after each edit: `ruff check`, `py_compile`,
  `pytest -q`. Fix what fails in a second complete pass over only the failing files.
- Commit once per coherent batch with a message that states what changed and why. Push the
  batch; the operator has lost work to crashes before, so a pushed batch is the unit of
  progress.

## 3. Structural context redaction and texturization

Why: the input baseline is the largest recurring cost. Everything carried forward that does
not change the next decision is waste.

- Do not paste tool output, file dumps, or docstrings back into your reasoning or your reply.
  Keep a thin state vector: files touched, decisions made, open questions, next action.
- Strip decorative comments and historical narration from what you write into files; keep
  the comments that explain a non-obvious why (a vendor quirk, a policy ceiling, a security
  boundary). The code should read as dense and intentional.
- Replies to the user: lead with the outcome, then what changed (files, one line each), then
  what was verified and what was not. No narration of the process, no restating the request,
  no closing offers. A report that fits in 250 words is the target unless the user asked for
  a plan or an audit.
- Compress long conversational history the same way the app does: summarize decisions,
  constraints, and open items; drop the rest.

## 4. Token-budget pacing protocol (per turn)

| Budget line | Target | Rationale |
|---|---|---|
| Files read | ≤ 3 targeted regions per change, from the map | Reading is the recurring cost |
| Writes | 1 complete write per affected file | No patch dribble |
| Verification runs | 1 lint + 1 test run per batch (2 on failure) | Verification is fixed cost; batch it |
| Model calls in the app | Never add a provider call to planning or UI paths without an explicit toggle | Free tiers are metered per request |
| Reply length | ≤ 250 words for a change; longer only for plans, audits, or when asked | Output tokens cost too |

Pacing means: think longer, act fewer times. When you notice yourself about to read a file a
second time, or to make a third small edit to the same file, stop, finish the plan, and do the
single complete pass instead.

## 5. Guardrails that override the budget

- Correctness beats compactness: a complete rewrite must pass the same tests as the code it
  replaces, plus tests for the new behaviour. If you cannot verify, say so plainly.
- Secrets never enter files, logs, prompts, or replies; redaction is not a token to save.
- Do not shrink a change the user asked for to fit the budget; scope is the user's call.
  Deliver it in one batch, or say exactly what is left and why.

## Workflow summary

1. Read `references/repo-map.md`. Name the files and functions the change touches.
2. Read only those regions. Map the ripple (callers, tests, docs).
3. Plan the whole change set; resolve conflicts before writing.
4. Write each file once, complete. Update tests and the map in the same batch.
5. One verification pass. One commit. Push.
6. Report: outcome, files, verified / not verified. Done.
