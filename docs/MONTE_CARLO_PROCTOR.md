# The Monte Carlo proctor · analysis before optimization

Status: analysis, September 2026, with uses 1 and 2 built the same day (`orchestrator/proctor.py`:
`simulate_routing`, `cached_fragility`, `forecast_budget`, `forecast_vendors`, `should_defer`; the
society tick defers cycles on the forecast; the routing expander shows both reports; the outcome log
carries a per-send `fragility`). It exists because the operator asked for the pink-wave math to be
pushed further, and for the out-of-the-box uses to be analyzed before anything is optimized.

## What exists today, stated plainly

- `router.generate_one_over_f_noise` produces one reproducible 1/f^alpha realization; the alpha
  recovery and the Euler-Maruyama stencil are tested against the research formulas.
- `router.project_seth_routing_entropy` integrates the Project Seth SDE on **one** realization per
  endpoint, seeded by the endpoint name, driven by the observed failure rate and latency, and scores
  the entropy gained over the undriven baseline. That term is a tenth of a penalty that can move an
  endpoint's utility by at most 0.30.
- `pinkwave` walks a 1/f series one step per use and nudges four features inside fixed bounds
  (routing jitter ≤ 0.15 of the penalty, Heavy draft temperature ≤ +0.4, recall share 10 % to 25 %,
  digest recall 600 to 1,200 characters).
- The outcome log (`route_log`: runner-up endpoint, pink-wave state, thumbs, locked artifacts) and the
  *Chaos on vs off* table are built and are the measuring instrument for everything below.

There is **no Monte Carlo anywhere**: one realization, no statistics over realizations. "The Monte
Carlo proctor is not being used properly" is accurate in the strict sense that it does not exist.
`docs/IMPLEMENTATION_PLAN.md` names "treating pink-noise weighting as validated optimization" as an
anti-goal; that stays true. A Monte Carlo layer is legitimate when it reports *statistics that a
single run cannot*: how often a decision flips, how wide the outcome spread is, which outliers
appear. It is not legitimate as a claim that the noise itself knows something about the vendors.

## What "higher chaos unveils outliers" can mean here

A proctor runs N realizations at a chaos level higher than production, watches what changes, and
reports outliers. The decision stays with the deterministic controller and its bounds. Concretely the
proctor answers three questions per feature: *how fragile is the deterministic choice*, *what is the
spread of outcomes*, and *which rare cases deserve a look*.

## Candidate use cases, ranked

| # | Use case | What the proctor computes | Cost | Value | Verdict |
|---|---|---|---|---|---|
| 1 | **Routing fragility** | For each request, N = 2,048 realizations of the penalty with the jitter amplified 1× to 8×; per endpoint, its win rate. Fragility = 1 − win rate of the deterministic winner. Outlier = an endpoint that wins under amplification but never at production gain. | CPU only, ~50 ms for 8,192 paths | Tells you *when* the wave can matter at all. Today's endpoint table is not fragile (gaps 0.15 to 0.5), so this will mostly report "robust", which is itself a result. Becomes decisive once telemetry penalties or a local model bring endpoints close. | **Built.** `simulate_routing`; `cached_fragility` (512 paths, once a minute per task type) fills the outcome log's `fragility` column and the chaos table's `mean_fragility`. |
| 2 | **Budget forecast with bursty demand** | Model the day's token demand as 1/f-correlated (bursts cluster, as real usage does), N = 256 paths from the persisted counters, per vendor: P50 and P10 (early tail) of the hour the daily cap is hit. | CPU only | Real operational value on free tiers: the society tick can schedule cycles into forecast headroom and the sidebar can warn "Gemini likely capped by 18:00 UTC". Outliers = paths that cap early; those are the bursts to smooth. | **Built.** `forecast_budget`, `forecast_vendors`, `should_defer`; the tick skips company and academy cycles (and logs why) when every keyed vendor is out of headroom or likely to cap within the hour. |
| 3 | **Timeline forecast for waves** | Per work, simulate cycle outcomes (advance / stall / return) with rates taken from the cycle log, N = 512 paths; per milestone, the probability of landing by its due date; outliers = works whose P90 misses the wave. | CPU only | Turns "timeline drift" from a static due-date check into a probability the board can act on. Needs a few weeks of cycle history to be meaningful. | Build when the studio has run for two weeks. |
| 4 | **Heavy Mode candidate spread** | K = 3 drafts at temperatures spread by the wave; pick by critique agreement; outliers = drafts the critique rates far above the rest. | **3× draft tokens** | Genuine quality upside, but it spends the one resource this project protects. Only defensible in an explicit "explore" send, never by default. | Optional toggle, off by default; measure with thumbs before keeping. |
| 5 | **Memory serendipity** | One extra recalled line per prompt drawn from low-rank but novel matches, chosen by the wave; the outcome log says whether prompts with a serendipity line score better. | Free | Cheap experiment with a clean measurement. Small expected effect. | Build after 1 and 2, as a flagged experiment. |
| 6 | **Deterministic tie-breaking everywhere** | Replace insertion-order tie-breaks (fallback routing, `wake_for_seat`, examiner choice) with the wave. | Free | Already done for routing; the rest are cosmetic. | Low priority. |
| 7 | **Sampling parameters per vendor** | Expose `top_p` and provider `seed` in `build_cortex_request` so the wave can also shape sampling, not only temperature. | Free | Enables 4 and finer temperature schedules; no value alone. | Do with 4. |
| 8 | **Patch-variant sampling in the repair loop** | N patch attempts at spread temperatures, keep the one that passes tests. | N× tokens | Real upside on hard fixes; expensive; self-hosted only (tests run there). | Later, self-hosted, bounded N ≤ 3. |

Rejected: Monte Carlo over spatial layouts (the solver is deterministic by design and the docs say
so), Monte Carlo inside the quota ledger (caps are hard constraints and must stay exact), and any use
that lets the noise override a capacity row or a key.

## How each one is judged

Every candidate ships with its measurement or does not ship:

1. It writes what it computed into the outcome log (a `fragility`, a `forecast_hour`, a `serendipity`
   flag) next to the decision it influenced.
2. The *Chaos on vs off* table, or a sibling table for that feature, compares good rate, down rate,
   failures, truncations, and latency between the feature on and off over at least a week of use.
3. The bounds stay: a proctor result may *narrow* a choice or *warn*; it never widens a hard limit.

## Shape of the proctor (1 and 2 built; 3 proposed)

```
orchestrator/proctor.py
  simulate_routing(task_type, estimated_tokens, ledger, paths=2048, amplifications=(1, 2, 4, 8)) -> RoutingReport
      win_rate and decision entropy (bits) per amplification, fragility, outliers; argmax over the selector's own rows
  forecast_budget(scope, vendor, n=256, alpha=1.0) -> BudgetReport
      p50_cap_hour, p90_cap_hour, paths that cap early
  forecast_timeline(scope, company_id, n=512) -> TimelineReport
      per milestone: p_on_time; per work: p90 finish day; outliers
```

Each report is a dataclass with a `summary()` line for the routing log and the Ops view. Reports are
computed on demand (a button, or once per tick), never per send, so the CPU cost is invisible.

## Adjacent request noted

Manga-style illustration sub-agents for the books are a separate capability (image generation through
a BYOK endpoint, most likely Hugging Face Inference with a text-to-image model, stored as PNG
artifacts and attached to a work's manuscript). It does not depend on the proctor and is listed here
only so it is not lost.
