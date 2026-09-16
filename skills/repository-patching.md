---
name: repository-patching
description: Rules for producing file changes the pipeline can apply safely.
keywords: patch refactor repository repo diff pull request branch pytest sandbox unified
---
- Emit complete files in fenced blocks named with `file: path`; never an excerpt, never "... rest unchanged". The parser refuses blocks that elide code or hold a fraction of a module's definitions.
- Keep changes minimal and local; add or update a test next to the change when behaviour changes.
- The pipeline applies blocks in a sandbox, checks syntax, optionally runs the repository's tests (off by default for fetched repositories), and shows a reviewable diff; nothing reaches GitHub without the operator pressing Push.
- When a task needs more context than the tree excerpt shows, say which file to open instead of guessing its contents.
