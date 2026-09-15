# Supply-chain engine audit: using `scrcae` to plan Chat Johnson's token supply

Date: 2026-09-15. Engine audited: `colegleason1-jpg/supply-chain-resilience-engine` at commit
`ed03d4b` (`engine/` package `scrcae` 0.1.0, the Streamlit client under `app/`). Audited
read-only from a clone; nothing in that repository was changed. Every feature the engine
exposes was exercised on a Chat Johnson-shaped problem (activities as nodes, tokens as capital);
the probe scripts are in the session scratchpad (`sce_probe*.py`).

## 1. Verdict in one paragraph

The engine is a real, tested capital-allocation optimizer (mixed-integer, CBC through PuLP)
with a correlated Monte Carlo, an honest infeasibility diagnosis, a budget sweep, a calibration
estimator that refuses thin evidence, and a content-hashed audit record. It is not a scheduler
and not a flow model: it has one scalar budget, no per-supplier capacity row, and no time axis.
So it fits every place in Chat Johnson where a fixed pool of tokens must be split across
competing work under uncertainty: the **daily treasury**, a **release wave**, and a **mission
before Launch**. It also fits **tail-risk estimation** of what the society delivers when vendors
fail together, and its calibration fits **per-vendor stress from the route log**. It does not
fit per-minute pacing or per-request routing; Cortex 2's rows and the Batch P pacing stay the
enforcement there.

## 2. What was verified, not assumed

| Check | Result |
| --- | --- |
| Engine test suite (`engine/`, Python 3.12 venv) | 197 passed (known-answer, parity, Hypothesis property tests) |
| App tests (Streamlit client; counted, not run) | 376 test functions |
| Solver | CBC through PuLP, bundled binary, no service; `pulp.listSolvers(onlyAvailable=True)` → `PULP_CBC_CMD` |
| One solve, 5 nodes, concave response (13 tangent points) | 15–26 ms |
| Budget sweep, 11 solves | 67 ms |
| Correlated Monte Carlo, 3 nodes × 20,000 draws | 19 ms |
| Constraint re-verification | `constraint_report.violations` recomputed from the returned allocation, 0 on every solve |
| Audit record | content hashes over network, objective, response model, parameters, seed (`input_hash`, `output_hash`, `engine_version`) |
| Python | `requires-python >= 3.12`; Chat Johnson's Cloud runtime is 3.12 (`runtime.txt`); its CI matrix also runs 3.11, so the engine must be an optional extra |
| Dependencies | `numpy>=1.26`, `pulp>=2.8`; no Streamlit import inside `scrcae` |

## 3. Every feature, toggled on the Chat Johnson shape

Network used: chat safety stock (120k tokens, 30 value points, must be fully funded), academy
cycle (250k, 18 pts, min scale 0.3), company cycle (300k, 25 pts, min 0.3, never funded beyond
the chat reserve), missions (150k, 15 pts, min 0.2); baseline 90 points; budget 450k tokens.

| Feature | What it did on our shape | Use here |
| --- | --- | --- |
| **Target mode** (`MinimizeCapitalObjective` + `required_risk_reduction_pts`) | "55 points for the fewest tokens": 390k linear, 372k concave; funded chat 1.0, missions 1.0, company 0.34–0.40, academy 0 | The natural mode for every placement: a goal, the fewest tokens |
| **Budget mode** (`MaximizeRiskReductionObjective`, budget 450k) | 60.0 pts linear, 61.7 pts concave (concave spreads across academy 0.3 + company 0.35 instead of company 0.6) | "Spend today's share for the most value" when no goal is set |
| **Monetary NPV** (`MonetaryNPVObjective` + `PriceBook`) | Funds only what beats its price; with unsourced prices it funded the chat reserve alone | Only once a token has a stated value per goal point; the ADR's warning about unsourced prices applies |
| **Legacy weighted** (`LegacyWeightedObjective`) | Identical to budget mode on our shape (lead-time column near zero) | Parity only; not for new work, as the engine says |
| **Response: linear** | The honest baseline | Default for planning |
| **Response: parameter-power 0.85** (legacy) | Reweights nodes, no diminishing returns; made the 55-point target infeasible (coefficients shrink to `R^0.85`) | Do not use; kept in the engine for parity |
| **Response: allocation-concave 0.85** | Genuine diminishing returns; tangent + chord approximation; 15–26 ms | Use when items per cycle yield less per token, once the exponent is calibrated per activity |
| **Diagnosis** | Budget short: `exceeds_budget`, `capital_required=708,889`, remedy sentence; asking beyond the baseline: `exceeds_risk_cap`; structural ceiling separately named | The shortfall sentence the tick, the board, and Task Finder can show |
| **Attainable frontier** | Unbounded: 88 of 90 pts at 820k, limited by structure; at 300k: 46.5 pts, limited by budget | "Can this be finished today at any budget?" before starting |
| **Budget sweep** → `saturation_budget` | 11 solves in 67 ms; saturation 180k on the small shape | The recommended daily share next to the slider |
| **Dependencies** (`x_dep ≤ x_prereq`) | Company never funded beyond the chat reserve; bound as expected | Final edit ≤ manuscript; company ≤ chat reserve; mission step ≤ its inputs |
| **Bundles** (discount only at full funding) | Declined when the pair was not fully funded | A batching or cache saving when two activities share an endpoint |
| **Per-node macro multipliers** | Stressing the academy ×0.5 left an unfunded academy unfunded; the mechanism is a solve-time overlay, never written back | Per-vendor stress from the route log (section 5.4) |
| **Monte Carlo: lognormal shock, σ 0.12** | P50 on the deterministic answer (bias ±0.07 pts), P90 +6–9 pts depending on correlation | Tail of delivered value for a plan |
| **Monte Carlo: lognormal σ 0.35** | P90 +17–25 pts; bias −1.4 to −3.8 pts because delivered reduction clips at the baseline. Real property, reported by `reduction_bias_pts` | Read the bias; keep σ measured, not typed |
| **Monte Carlo: legacy truncated normal** | Optimistic centre, as the engine documents | Parity only |
| **Monte Carlo: deterministic shock** | Collapses onto the deterministic answer exactly | Test fixture |
| **Correlation: uniform ρ, explicit matrix, repair** | ρ 0.7 widened P90 by 3.3 pts over ρ 0; repair never triggered on valid matrices | Matrix from `dynamics.cached_coupling`, repaired to unit diagonal |
| **Calibration** (`calibrate_elasticity`) | 72 hourly observations with a planted elasticity 1.4: fitted 1.28, interval 1.13–1.55, R² 0.70, quality USABLE; 5 observations: refused as TOO_FEW_OBSERVATIONS with no number | Per-vendor failure elasticity to load from `route_log` bins |
| **Audit record** | `input_hash`/`output_hash` stable run to run | Cite in the Bible's claim register; store with each plan |
| App-side: pricing derivation, market/macro feeds, copilot, portfolios, auth | Sound, but about commodities and CFO inputs | Not imported; the copilot's "propose, never write" is already how Task Finder's handoff works |

## 4. The engine's model, in Chat Johnson's words

| Engine object | Meaning in the engine | What it maps to here |
| --- | --- | --- |
| `Intervention` (node) | a candidate investment with `cost`, `risk_reduction_pts`, `lead_time_saved_days`, min/max funding scale | a unit of work: an activity of the day, a work item of a wave, a mission step; `cost` = tokens at full scale, `risk_reduction_pts` = goal points delivered, `lead_time_saved_days` = operator waiting removed |
| funding scale `x` in [min, max], binary `y` | partial funding above a minimum economic scale, or none | run a cycle at reduced size, or skip it; the chat reserve has `min = max = 1` |
| `Dependency` | `x_dep ≤ x_prereq` | final edit ≤ manuscript; company ≤ chat reserve; step ≤ inputs |
| `Bundle` | a rebate when every required node is fully funded | a same-endpoint batching saving |
| `budget` | one scalar net capital cap | tokens available: the day across keyed vendors, or a wave's or mission's allowance |
| target mode | reach a floor for the least capital | "deliver these goal points for the fewest tokens" |
| `node_macro_multipliers` | per-node stress overlay at solve time | vendor stress from the calibrated load elasticity |
| `SimulationRequest` + correlation | delivered value under correlated lognormal shocks | vendors failing together |
| `attainable_frontier`, `diagnosis` | structural ceiling vs budget shortfall | "cannot at any budget" vs "needs N more tokens" |
| `sweep_budget` → `saturation_budget` | the lowest budget that buys everything worth buying | the daily share the slider should sit at |

## 5. Placements, in order of value

### 5.1 Daily treasury: plan of the day (fits; build first)

Today `economy.py` splits the day by fixed ratio and `tick.budget_deferral` defers every
cycle on a bursty forecast. Neither answers "which activities, at what size, so the chat never
starves". One target-mode solve per tick does: nodes are the activities with the society's own
per-item token estimates, the budget is the day's remaining tokens from the persisted counters,
the chat reserve is pinned at full funding, and an infeasible day comes back with the exact
shortfall so the tick drops the cheapest activity instead of all of them. The sweep's
saturation point becomes the recommended daily share. Every plan carries the audit hash.

### 5.2 Tail risk of the plan (fits; build with 5.1)

Given the plan, how much of the day's value survives correlated vendor failure. Shock size per
vendor from the learner's failure and 429 rates; correlation from `dynamics.cached_coupling`,
repaired to a valid correlation matrix by the engine. Output: P50/P90 delivered value and the
expected shortfall, next to the plan, with the bias line so a clipped tail is visible.

### 5.3 Release wave and mission feasibility (fits; second batch)

A wave's unpublished works and a mission's steps have the same shape as activities: tokens to
finish, goal points toward the gate, dependencies (final edit after manuscript, a step after its
inputs). Target mode says the fewest tokens to reach the wave gate; the frontier says whether
Launch can finish today at any budget; the diagnosis gives the board and Task Finder a shortfall
in tokens instead of a stalled queue. Task Finder's "Cost preview" line becomes a feasibility
verdict before Launch.

### 5.4 Vendor stress from evidence (fits; small)

`calibrate_elasticity` fits `failure_rate = f0 · (1 + e · max((load − anchor)/anchor, 0))`
per vendor from hourly `route_log` bins (load fraction as the price, failures and 429s as the
disruption rate), with a bootstrap interval, and refuses when the bins are too few. The fitted
elasticity becomes a per-node multiplier on the plan, so a vendor that degrades under load is
planned around before it fails. The routing rows are untouched: this shapes the plan, never the
hard limits.

### 5.5 Sound math to port, not import

- The Monte Carlo's percentile standard error (`_percentile_standard_error`) belongs in the
  proctor's reports so a 512-path fragility or a cap-hour forecast says how precise it is.
- `repair_to_correlation` (spectral clip plus diagonal renormalisation) is the right way to turn
  pairwise coupling into a usable matrix; the naive ridge it replaces silently widens tails.
- The audit record pattern (canonical JSON, SHA-256, versions) fits the Bible's claim register.

### 5.6 Where it does not belong

Per-request routing and per-minute pacing. A Heavy send is three calls inside one minute against
two per-vendor ceilings; that is scheduling, which the engine does not model, and Cortex 2 with
the Batch P pacing already does it. Also not the academy's producer rotation: fairness (fewest
tasks first) is a rule, not an optimum.

## 6. Gaps that decide the integration shape

1. **One scalar budget.** Closed on 2026-09-15: the engine's resource family (pull request 1,
   merged as 6f0fc8b) adds one verified row per capped supply, and the plan of the day models
   each keyed vendor as a resource with its remaining daily tokens. Cortex 2 still enforces every
   limit call by call.
2. **No time axis.** The tick re-plans every interval, the right cadence for a day-level plan.
3. **Estimated costs.** Token demand per activity is an estimate; the re-plan reconciles it.
4. **Uncalibrated exponent, unsourced prices.** Use target mode, which needs neither; move to
   the concave response per activity only when a fit supports it.
5. **Monte Carlo clip.** At large σ the baseline clip biases delivered value; show
   `reduction_bias_pts` and keep σ measured.
6. **Python 3.12 only.** Optional extra (`requirements-supply.txt`, pinned to a commit via a git
   URL with `#subdirectory=engine`), import-guarded like pyspi; CI on 3.11 and hosts without it
   keep working and the panel reports "engine not installed".
7. **CBC is a subprocess.** 15–70 ms per plan is fine per tick, per wave, per Launch; never per
   chat send.

## 7. A proposed, additive change to the engine (your repository, your call)

A resource-capacity family would make the engine a true multi-supplier planner and close gap 1:

- `Resource(name, capacity)` on `SupplyNetwork`, and an optional `usage: Mapping[str, float]`
  on `Intervention` (units of each resource consumed at full scale);
- one constraint row per resource, `sum(usage_r,n · x_n) ≤ capacity_r`, verified after the solve
  like the budget row and carried in the audit record;
- default empty, so every existing test, result, and hash is unchanged.

With it, vendors become resources with daily caps and activities carry a routing mix, and the
plan respects per-vendor supply directly. It is a self-contained change with its own tests; it
should land as a pull request on the engine repository, reviewed there, not as a fork in Chat
Johnson. Chat Johnson's adapter works with or without it.

## 8. Proposed batches

**Q1, treasury planner.** `requirements-supply.txt` (git URL, pinned commit);
`orchestrator/treasury_plan.py` with `available()`, `build_network(scope, day)`,
`plan_day(scope, budget, goal_points)` (allocation, diagnosis, saturation budget, audit hash),
`tail_risk(plan, coupling)`, `vendor_stress(scope)` from the calibrated elasticity, and a
deterministic fallback (today's ratio split) when the engine is absent; `society/tick.py`
consults the plan and defers per activity with the shortfall in the cycle log; the chat reserve
is never reduced; `app.py` Society settings panel "Plan of the day" (allocation, why, shortfall,
recommended share, P50/P90, engine version and hash); vault table `treasury_plans`; the proctor
gains percentile standard errors; tests, README, RUNBOOK, Bible claim register.

**Q2, wave and mission feasibility.** `release.wave_plan(scope, company_id)` and a Launch-time
`mission_plan` using the same adapter; board and Task Finder show the verdict and the shortfall.

**Engine PR (optional, separate).** The resource-capacity family of section 7.

No batch changes hard limits, keys, or routing.
