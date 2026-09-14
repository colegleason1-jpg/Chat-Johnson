---
name: company-reporting
description: How seats report inside the companies: the board hears facts, decisions, and asks; never padding.
keywords: board report cycle company ceo executive assistant scorecard rocks l10 issues todo agenda backlog seat department
---
- A report to the board names what moved (items, stages), what waits for the board, the open issues with their resolutions, and the one question that needs a decision; under 200 words.
- Level 10 minutes use the line formats the cycle parses: HEADLINE, ISSUE | RESOLUTION, TODO seat_key: task, QUESTION.
- Ratings and assignments follow the exact `#id: n` and `#id -> seat_key` forms; anything else is treated as no answer and the deterministic fallback decides.
- Escalate under the skip-level rule with a line `ESCALATE: up :: message` or `ESCALATE: down :: message`; say what you need decided, not what you feel about it.
