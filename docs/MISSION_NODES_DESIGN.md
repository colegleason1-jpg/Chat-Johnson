# Design · Mission nodes and the chat → Task Finder handoff

Status: design only (operator asked for the plan; nothing here is built). Builds on the shipped
Task Finder (writing missions, sized sections, assembled deliverable), the session-only GitHub
push, the hub connector, and the Deploy Kit generator.

## 1. What a mission becomes

Today a mission is a statement plus an ordered list of typed workstreams that each call the
routed model. A **node** generalizes a workstream:

| Field | Meaning |
|---|---|
| `id`, `title` | as today |
| `executor` | `model` (today's workstream), `connector` (a built-in action), `sub_mission` (a nested mission run to completion, its deliverable returned as this node's output) |
| `task_type` | router task type for `model` nodes (`reasoning`, `chat`, `code_patch`, …) |
| `config` | executor parameters: for `model` the instruction and an optional output budget override; for `connector` the action name and its arguments; for `sub_mission` the statement and its own nodes |
| `inputs` | which earlier nodes' outputs are injected verbatim (default: everything before it, through the chat as today) |
| `output` | `chat` (a message in the mission chat, today's behaviour), `artifact` (locked under `missions/`), or both |
| `on_failure` | `stop` (default), `skip`, `retry_once` |

Connectors available on day one, all already implemented as functions:
- `deploy_kit.generate` (spec → files, validated) and `deploy_kit.lock`
- `github.fetch` (owner/repo, ref → sandbox path), `github.push` (files → branch + PR), `github.revert`
- `repository.run` (goal + sandbox → diff)
- `vault.export_thread`, `vault.save_artifact`
- later, as the roadmap connectors land: document scraper, cross-thread search, external APIs declared by the operator (URL, method, headers from session secrets, JSON body template).

## 2. Execution

Sequential in node order (a DAG with one edge per `inputs` entry; cycles rejected at edit time).
Per node: free-tier pacing with `cortex_wait_seconds` (model nodes), the request lock, the output
budget, the finish-reason notice; failures follow `on_failure`. A `sub_mission` node runs its own
nodes in the same thread with a prefixed title (`[Sub 2.1] …`) and returns the assembled
deliverable. Every node's output is appended to the chat exactly as today, so migration, digests,
and transcript export keep working unchanged.

Storage: `threads.mission` keeps the statement; a new `mission_nodes` table (thread_id, position,
JSON of the node) keeps the graph so a chat can be reopened with its nodes intact and re-run.

## 3. Editing nodes in the plan panel

The plan panel keeps the `st.data_editor` grid (title, executor, task type, instruction) and adds a
per-row **Configure** popover: executor-specific fields (connector picker with its argument form,
sub-mission statement and section count, output target, failure policy). Deterministic templates
seed the nodes exactly as `task_plan` does today; the operator adds, removes, or reorders rows.
Validation before launch is offline: unknown connector, missing argument, cycle, or a push node
while the push slot is disarmed all block the Launch button with the reason shown.

## 4. Chat → Task Finder handoff

Goal: develop an idea in Normal Chat or Chat Bot, then land it in Task Finder as a ready mission.

1. The capability card teaches the model one output format: when the operator asks to "turn this
   into a mission", "plan this as a mission", or "send this to Task Finder", the answer ends with a
   fenced block:
   ```mission
   statement: <one line>
   nodes:
     - title: …
       executor: model | connector | sub_mission
       task_type: reasoning
       instruction: …
   ```
2. `orchestrator/missions.py` gains `parse_mission_block(text)` (YAML via the bundled parser,
   strict schema, unknown executors rejected) and the chat renders a **Send to Task Finder** button
   under any answer that contains a valid block. Pressing it creates a Task Finder chat (or uses the
   current one), stores the nodes, and opens the plan panel with them prefilled; nothing runs
   until Launch.
3. The reverse path, **Ask the chat to refine this mission**, sends the current node list back to
   the chat as context so the operator can iterate in prose and re-send.

Nothing in the handoff costs a provider call beyond the chat answer the operator already asked for.

## 5. Phases and effort

| Phase | Scope | Size |
|---|---|---|
| N1 | Node model, `mission_nodes` table, executor `model` only (today's behaviour on the new model), configure popover with output target and failure policy | 1 batch |
| N2 | Connector executor with the day-one connectors, offline validation, push-node gating | 1 batch |
| N3 | Sub-mission executor, nested titles, deliverable return | 1 batch |
| N4 | Chat handoff: capability-card format, parser, Send to Task Finder, refine loop | 1 batch |

Open decisions for the operator: whether connector nodes may call operator-declared external APIs
(needs a session-only secret slot per API), and whether sub-missions may nest more than one level.
