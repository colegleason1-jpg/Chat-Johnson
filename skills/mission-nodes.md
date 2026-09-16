---
name: mission-nodes
description: How to hand a plan to Task Finder as mission nodes the app can run.
keywords: mission nodes task finder workstreams connector sub-mission executor
---
- When the operator asks to plan something as a mission or send it to Task Finder, end the answer with exactly one fenced block tagged `mission`:
  ```
  statement: one line saying what the mission delivers
  nodes:
    - title: Research the topic
      executor: model
      task_type: reasoning
      instruction: what this node must produce
    - title: Lock the brief
      executor: connector
      config: {connector: vault.save_artifact, name: brief.md}
  ```
- Executors: `model` (a routed model call), `solver` (the spatial layout solver), `webqa` (URL check), `connector` (deploy_kit.generate, github.fetch, github.push, github.revert, repository.run, vault.export_thread, vault.save_artifact, webqa.check, mcp.call), `sub_mission` (config: statement, sections).
- Optional per node: `inputs` (earlier node numbers this node should see), `output` (chat | artifact | both), `on_failure` (stop | skip | retry_once). Keep it to twelve nodes; a push node needs the GitHub slot armed.
