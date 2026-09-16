# Logic-gap audit · why a generated page never came out working (2026-09-16)

Evidence: the operator's exported chats 11–14 (Supabase `chat_exports`), one hour of Heavy Mode work on a
study-guide app; four code audits (answer path, prompt/context, missions/jobs on Cloud, operator flow); a
fake-stream reproduction of the streaming path. Nothing here is a guess: every finding names the line.

## 1. What actually happened, in order

1. The operator asked for a study app with 100 questions with the output budget at its default (2,048
   tokens). Heavy Mode gives the draft half of that (1,024) and the synthesis all of it. A page like that
   needs 6,000–15,000 tokens. **Every page answer in the thread ended mid-file with an unclosed fence**
   (stored sizes 545–2,534 estimated tokens). Groq's gpt-oss also spends part of the ceiling on hidden
   reasoning the app never sees, and Gemini's thinking is billed inside `maxOutputTokens`, so the visible
   page is smaller still.
2. The app showed a one-run warning ("stopped at the output budget … send 'continue'") under a wall of
   code and stored the answer with no mark. **The model was never told its answer was cut.** The next
   prompt replayed a page that simply stops.
3. The Heavy critique was told to check for "one complete self-contained html fence", saw a cut draft,
   said "incomplete", and the synthesis ("keep it concise") wrote a *smaller* page, which was cut again.
   That is the 100 → 25 → 3 → pure-CSS → Streamlit spiral the operator watched.
4. The thread had been auto-migrated from an earlier chat, and the inherited "vision digest" (about an
   unrelated microservice incident) is injected first into every prompt, taking a quarter of the context,
   and contains the sentence "Inline onclick attributes are blocked by CSP in sandboxed previews; scripts
   may be stripped entirely." **Every model kept blaming CSP because the app told it to.**
5. When the operator said "the buttons don't work", the previous page reached the model with its middle
   50–80 % removed (context clipping), placed in the system prompt under "never imitate its format". It
   could not patch what it could not see. The word "fix" also loaded the repository-patching skill, whose
   text forbids partial edits, so "fix one thing at a time" injected a rule demanding whole-file rewrites.
6. The canvas defaulted to Sanitized, which strips every script; nothing in the frame said so. In the
   second thread the page loaded Tailwind from a CDN (no anti-CDN rule is sent in Sanitized mode), so it
   arrived unstyled: "loads weird, text overlapping".
7. A "Fix the buttons on this preview" mission received no page at all (missions cannot read the canvas),
   its first step failed on a full free-tier window (missions have no headroom retry), and a failed first
   step writes nothing to the chat. The operator saw nothing and launched it twice.
8. Two Groq answers were garbage: a deliberation-only answer ("Wait, let's make sure…") and a fragment
   starting mid-attribute. gpt-oss splits output across reasoning and content channels; the app reads only
   the content channel and has no "does this look like an answer" check.

## 2. Findings by severity (file:line as of commit 4862809)

**Critical**
- C1 Heavy draft at half budget, critique judges the cut draft, synthesis shrinks: `router.py:1936,1946-1950,1889-1893`.
- C2 Truncation shown once, never stored, never fed back: `app.py:1613-1617`, `vault.py:642-682` (no finish column), `prompting.py:24-66`.
- C3 Inherited digest injected first, 25 % of the window, unrelated content: `vault.py:1024-1035,1053`.
- C4 Previous page clipped and moved to the system prompt: `vault.py:1054-1097`, `prompting.py:55-56,65`.

**High**
- H1 Heavy fallbacks return the 1,024-token draft with `finish=""`: `router.py:1962-1981,2064-2067`.
- H2 No reasoning/thinking control on Groq gpt-oss or Gemini: `router.py:1238,1254`.
- H3 `delta.reasoning` dropped, content leaks stored verbatim, no shape check: `router.py:1281-1293,1363-1364`.
- H4 PREVIEW RULES only in Run mode; no anti-CDN rule in Sanitized: `app.py:1644`.
- H5 Critique sees ≤ 485 chars of the page: `router.py:1870-1885`.
- H6 "fix" loads repository-patching (forbids excerpts): `skills.py:67-76`, `skills/repository-patching.md`.
- H7 Sanitized default, scripts stripped silently, canvas off-screen on arrival: `app.py:187,559,606,3327`.
- H8 Missions: no canvas page, no headroom retry, failed step posts nothing: `app.py:1966-1971`, `mission_runner.py:85-96,159-173`.
- H9 Missions cannot test a page; no preview executor: `missions.py:248`, `mission_runner.py:43-68`.

**Medium**
- M1 No output ceiling per endpoint; MILP blind to output size; Groq structurally unable to write a long page: `router.py:106-120,777-800`.
- M2 Budget slider hard cap 8,192, no auto-raise for pages: `app.py:3176`.
- M3 Partial page reaches the canvas and comes back as a "sandbox failure" with no truncation cause: `preview.py:146-168`, `sandbox_preview.py:362-397`.
- M4 A new chat cannot see the canvas page; "Fix the buttons" in a fresh thread has nothing: `app.py:1629-1654`.
- M5 Exports omit jobs, node failures and the route log: `vaultsync.py:226-260`.
- M6 Engineer-speak throughout the flow (see §4).

## 3. The plan (Batch U, four pushes)

**U1 · Finish the answer (core).** Persist `finish` on every message; a cut assistant turn carries a
"[CUT AT THE OUTPUT BUDGET…]" note into the next prompt and a caption in history. Automatic continuation:
when an answer ends at the budget inside a fence, the app asks for the rest verbatim from the tail,
stitches on overlap, up to three rounds, normal mode, then stores one message. Heavy fallbacks carry the
draft's finish; the draft gets the full budget for interface requests; the critique is told a cut is a cut;
"concise" is dropped for pages. Reasoning control: `max_completion_tokens` + low reasoning effort for
gpt-oss, a low thinking budget for Gemini on page/code tasks. Answer shape check: an answer that starts
mid-tag or is deliberation only is retried once, then continued. Output ceilings per endpoint, an MILP row
for output need, and an automatic budget raise for interface requests (cap 16,384; sidebar checkbox,
default on). Slider default 4,096 with a plain label.

**U2 · Tell the model the truth.** APP STATE block in every prompt: canvas mode, budget and ceiling, whether
the last answer was cut, the last sandbox report. CANVAS RULES (no CDN, no external anything) in both
modes. The live window is filled before the digest; the digest is capped at an eighth and never outranks
the current page. The page the operator refers to is sent as a dedicated CURRENT PAGE turn, never clipped
(patch mode when it exceeds 6,000 chars: SEARCH/REPLACE edits applied in Python, validated, re-rendered;
full rewrite only when the page fits the budget). The critique sees the page. "fix" no longer loads the
repository skill outside Repository Work. A request that mentions the page/preview/buttons in a chat
without a page pulls the canvas page in, with a notice.

**U3 · Show the operator the truth.** Run mode auto-selected when a page with scripts arrives; modes renamed
"Preview only (buttons off)" and "Run the page"; a status line on the canvas (mode · page size vs budget ·
last run · repairs used); a truncation box inside the answer with Continue / Raise the limit / Smaller page;
"Unexpected end of input" translated to "the answer was cut off"; plain-language captions everywhere the
audit quoted engineer-speak.

**U4 · Missions that can fail out loud and see the page.** A failed step posts a system message in the
thread; headroom retry in missions; the canvas page travels into a mission that refers to it; a `preview`
mission kind with `preview.validate` (static completeness checks) and `preview.repair` (bounded regenerate
loop) executors; Task Finder says plainly that missions cannot run the browser and offers "Send to Chat
Bot"; exports include job failures.

## 4. Plain-language replacements (applied in U3)

"Output token budget" → "Answer length limit (about 4 characters per unit; a working page needs 6,000+)".
"Sanitized / Run in sandbox" → "Preview only (buttons off) / Run the page". "Scripts are removed here" →
"Buttons and other interactive parts are switched off in this view." "stopped: the same error came back"
→ "Repair stopped: the rewrite broke in the same place; this usually means the answer is being cut off."
"Free-tier window is full; sending in N s" → "Your free allowance for this minute is used up; sending again
in N s." "Thread health agent migrated…" → "This chat got long, so it was summarised into a new chat."
Cost preview → "This will make up to N requests with your key."

---

# Part 2 · Connected systems (second-level audit, same day)

The first part found where the hour went wrong. This part explains why the same thing will keep
happening until the systems underneath change, and reorders the plan by payoff. The chain is now
traced end to end from your very first chat: chat 4 began as a microservice stress-test; the "give
me a preview link" turns are where Gemini first invented the CSP explanation (chat 4, messages 24
and 28); the study app was already being cut off there (one answer is 79 characters long).

## 5. How the wrong conclusion was manufactured and kept

- **Digest "decisions" are regex hits from any speaker.** `_DECISION_RE` matches must / never / always /
  rule / policy / constraint / require; `_OPEN_RE` matches "blocked"; every line of every row is scanned,
  assistant and sandbox text included (`vault.py:1960-1962, 2073-2084`). A model's sentence about CSP, a
  sandbox report line "Blocked (2): …", or `<input required>` in a page all become "Decisions and
  constraints". The model refiner (`app.py:366-378`) is told to keep every decision and constraint and can
  invent them; its text is placed first and locked as an immutable artifact with no expiry.
- **Migration fires on token load, and pages are big by design.** `HEALTH_TOKEN_LIMIT` is 18,000
  estimated tokens in the live window; one exchange with two repair rounds is ~12,750 tokens, so a
  page-building chat migrates about every third send and the successor is exempt for only 12 rows
  (`vault.py:1953-1969`). The one thing the token trigger evicts is the page. That produced chats 11 →
  12 → 13 → 14 in one hour.
- **Raising the budget shrinks memory** on the narrowest keyed endpoint (`prompt_context_chars`), so the
  natural response to cut pages clips the page harder next turn; with Gemini keyed the 24,000-char cap
  applies at every budget, and the digest + summaries + recall take up to 15,300 of it first.
- **Recall has no stopwords and a substring hit test** (`keyword_search.py:13-17`, `vault.py:411-421`):
  "the buttons don't work in the preview" recalls the CSP sentence from any chat because "t" and "in"
  match everything; digests have no thread id, so a chat recalls its own inherited digest a second
  time; nothing ages out but a 30-day half-life.
- **Prompt order puts memory above the page** and labels the page "for reference only; never imitate
  its format" while the persona demands "return the complete page" (`prompting.py:20-21, 55-66`).
- **Fix turns are stored as user rows carrying the whole page**, so they count toward migration load and
  repetition, crowd out the operator's original request, and feed the digest with sandbox report lines.

## 6. Why the same request takes a different path each time

- **Groq wins every plain chat or code turn by table utility** (chat 0.953 vs 0.762; code_patch 0.974 vs
  0.636) and is the endpoint structurally unable to write a page (8,000 TPM). It loses only when the
  prompt is long enough to be infeasible; so the route is decided by prompt length, never by the answer
  size the request needs (`router.py:780-803, 857-860`).
- **The ledger never reserves and never sees hidden reasoning** (`_estimate_tokens`, charges after the
  stream on chars/4): the pacer says "no wait", the vendor answers 429, retries are capped at 8 s even
  when Retry-After says 60, sibling models are tried, the endpoint is excluded, Gemini's 5 RPM window is
  already spent by the earlier passes, and the Heavy pipeline swallows the failure by returning the
  1,024-token draft with an empty finish (`router.py:1962-1985`). No warning fires. That is the whole
  "sometimes Gemini, mostly Groq, twice garbage" pattern, and it needs no background cycle to happen.
- **Learned speed is polluted**: route time is measured from before the 65-second free-tier sleep and
  the 20-second lock wait and covers all three Heavy passes, attributed to the synthesis endpoint
  (`app.py:1504-1604`, `learner.py:40-41, 95-101`). After ten Heavy sends both endpoints sit near the floor
  and routing becomes a coin toss broken by momentum and jitter.
- **Only thumbs close the learning loop** (`learner.py:47, 250`): a cut answer, a failed send, a sandbox
  error or an operator's words never move a prior; a down-vote alone can never flip chat away from Groq.
- **`classify()` flips the task type turn by turn** (design/why → reasoning → Gemini; plain text → chat →
  Groq; an error line → test_fix; `import` in a fix prompt → code_patch), so the same conversation
  changes endpoint every send.
- **Background cycles take the very windows the chat needs**: a company cycle is up to 31 calls, each
  under the request lock, in Heavy Mode if Heavy was on at launch; the chat waits 20 s then "sends
  anyway" unlocked and races the cycle for the same minute window. The plan-of-the-day chat reserve is a
  daily-token notion applied only at tick time; nothing reserves a minute window at send time.
- **Per-minute windows are per process**; the VM worker's use is invisible to the app.

## 7. Why the canvas shows the wrong page and the loop cannot converge

- `extract_preview_source` takes the first fence that looks like markup: a ```css stub, a 40-character
  snippet, a partial page that precedes a full one, or a JavaScript fence with `<div>` in a template
  literal all beat the real page. `last_markup_in_chat` takes the newest row with any markup, so a
  109-character fragment refills the canvas on reload. Any workspace overwrites the canvas.
- `hold_fences` and the fence regex close on the first inner ``` inside a page; the remainder renders as
  a stray fence line; an unclosed fence gets no Lock button.
- A page cut inside `<script>` throws "Unexpected end of input"; `fix_decision` has no completeness
  input, `fix_prompt` asserts breakage, each rewrite is cut on a different line so the same-error rule
  never fires: one cut page costs nine model calls in Heavy Mode, then "two rounds used".
- The automatic fix prompt is classified test_fix and loads the repository-patching and
  company-reporting skills: three conflicting output contracts on every repair.
- In the default Sanitized mode the app has no verdict at all; the model believes the buttons work.

## 8. Runtime facts that made it worse

- The keepalive `curl` returns Streamlit's shell page; the script never runs, so the health JSON is never
  produced and nothing ticks. Only the Playwright smoke drive runs the app.
- Every browser reload or a disconnect longer than two minutes resets Heavy Mode to off, the answer limit
  to 2,048, the canvas mode to Sanitized, and the pasted keys; nothing tells the operator.
- A container restart loses up to ten minutes of vault rows (no snapshot on shutdown), forgets the
  minute windows (first sends after boot can 429 with "no wait"), and marks jobs failed with no message.
- A failed send leaves a question with no answer in the thread and a route row with no message id; the
  export omits routes, jobs and failures, which is why this audit had to reconstruct them.

## 9. Revised plan, ordered by payoff

**U1 · Stop the spiral (one push).**
1. Completeness gate before the canvas and before any repair: fence closed, `</html>`, script tags
   closed, brace balance; an incomplete page goes to automatic continuation (verbatim from the tail,
   stitched on overlap, up to 3 rounds, normal mode, one stored message), never to a repair round.
2. `finish` persisted per message; a cut answer carries a "[CUT AT THE OUTPUT BUDGET…]" note into the
   next prompt; Heavy fallbacks carry the draft's finish; the critique is told a cut is a cut; "concise"
   dropped for pages; the draft gets the full budget for page requests.
3. Output-need routing: endpoints whose output ceiling or TPM cannot fit the answer are excluded for that
   send; the budget auto-raises for page requests (cap 16,384; sidebar checkbox, default on); slider
   default 4,096; `reasoning_effort: low` on Groq gpt-oss for page/code work.
4. Memory serves the page: the live window is filled before memory; the digest is capped at an eighth,
   labelled "background from an earlier conversation, may be unrelated, never a rule"; only operator
   sentences (never fix turns, never assistant or sandbox lines) can become decisions; the refiner may
   only restate; recall gets stopwords and whole-word hits and is off for page requests; digests get a
   thread id; migration is deferred while a page is on the canvas, on a fix round, or when the last
   answer was cut, and `HEALTH_TOKEN_LIMIT` rises to 60,000 with fix turns excluded from the load.
5. Canonical page store per thread (vault setting + session): the canvas, the fix loop, the next
   prompt and missions read one page; the last closed page fence wins, snippets and css stubs never
   do; only chat workspaces write it; the page and the canvas mode survive a reload.
6. The model is told the truth: an APP STATE block (canvas mode, budget and ceiling, whether the last
   answer was cut, last sandbox report) and CANVAS RULES (no CDN, no external anything) in both modes;
   the current page as a dedicated unclipped turn; no skills on fix turns; repository-patching only in
   Repository Work, company-reporting only in Company.

**U2 · Tell the operator the truth.** Run mode auto-selected when a page has scripts; modes renamed;
canvas status line; truncation box with Continue / Raise the limit / Smaller page; "Unexpected end of
input" translated; restart banner and "keys were pasted in an earlier session" notice; a failed send
writes a system row; route facts exported and shown per message ("why this answer came out like this");
health JSON reports process start, workers, jobs, ledger and snapshot age; the keepalive is replaced by
the smoke drive on a longer cadence or marked inert; plain-language captions throughout.

**U3 · Routing and quota that see reality.** Token reservation while a request is in flight; vendor usage
from the final stream chunk (reasoning included); Retry-After and rate-limit headers honoured; a daily
request limit for Gemini; learned speed measured per pass net of waits; cut and failed answers fold into
the priors; momentum scoped per workspace; exploration limited to endpoints that can fit the answer;
background cycles deferred for five minutes after a chat send and ten after a page, run in normal mode,
and the chat lock timeout raised to one window; the served model recorded.

**U4 · Missions and patch mode.** A failed step posts a message in the thread; headroom retry in
missions; the canvas page travels into a mission that refers to it; `preview.validate` and
`preview.repair` executors with the static completeness check; Task Finder says missions cannot run the
browser and offers "Send to Chat Bot"; SEARCH/REPLACE patch mode for pages over 6,000 characters so
"fix one thing at a time" is literally what happens.
