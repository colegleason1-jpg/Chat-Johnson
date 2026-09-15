# Supply-chain engine audit: using `scrcae` to plan Chat Johnson's token supply

Date: 2026-09-15. Engine audited: `colegleason1-jpg/supply-chain-resilience-engine` at commit
`ed03d4b` (`engine/` package `scrcae` 0.1.0). Audited read-only from a clone; nothing in that
repository was changed.

## 1. Verdict in one paragraph

The engine is a real, tested capital-allocation optimizer with a correlated Monte Carlo and an
honest infeasibility diagnosis. It is not a scheduler and not a flow model: it has one scalar
budget, no per-supplier capacity, and no time axis. So it fits the **daily token treasury**
(which activities to fund today, with a safety stock for the chat, and by how much the day is
short when it cannot all be funded), and it fits **tail-risk estimation** of what the society
will actually deliver when vendors fail together. It does not fit per-minute pacing or
per-request routing; Cortex 2's rows and the Batch P pacing stay the enforcement there. Used for
what it is, it removes a class of failure Chat Johnson has today: background jobs starting work
the day's tokens cannot finish, and the chat paying for it.

## 2. What was verified, not assumed

| Check | Result |
| --- | --- |
| Engine test suite (`engine/`, Python 3.12 venv) | 197 passed (known-answer, parity, Hypothesis property tests) |
| App test count (not run; Streamlit client) | 376 test functions |
| Solver | CBC through PuLP, bundled binary, no service; `pulp.listSolvers(onlyAvailable=True)` → `PULP_CBC_CMD` |
| One solve, 5 nodes, concave response, tangent approximation | 15–26 ms |
| Budget sweep, 11 solves | 67 ms |
| Correlated Monte Carlo, 3 nodes × 20,000 draws | 19 ms |
| Infeasible request | `status=infeasible`, diagnosis `exceeds_budget`, `capital_required=673,333`, plain explanation with the shortfall |
| Constraint re-verification | `constraint_report.violations` recomputed from the returned allocation, 0 on every solve |
| Audit record | content hashes over network, objective, response model, parameters, seed (`input_hash`, `engine_version`, …) |
| Python | `requires-python >= 3.12`; Chat Johnson's Cloud runtime is 3.12 (`runtime.txt`); its CI matrix also runs 3.11, so the engine must be an optional extra |
| Dependencies | `numpy>=1.26`, `pulp>=2.8`; both install cleanly through pip; no Streamlit import inside `scrcae` |

Probe scripts: scratchpad `sce_probe.py`, `sce_probe2.py` (activities as nodes, tokens as capital).

## 3. The engine's model, in Chat Johnson's words

| Engine object | Meaning in the engine | What it maps to here |
| --- | --- | --- |
| `Intervention` (node) | a candidate investment with `cost`, `risk_reduction_pts`, `lead_time_saved_days`, `carbon_tons`, min/max funding scale | an activity for the day: chat safety stock, an academy cycle, a company cycle, queued missions, proctor/learner upkeep; `cost` = tokens at full scale, `risk_reduction_pts` = goal points it delivers, `lead_time_saved_days` = operator waiting removed |
| funding scale `x` in [min, max], binary `y` | fund partially, or not at all, never below its minimum economic scale | run a cycle at reduced size (fewer work items) or skip it; the chat reserve has `min = max = 1` |
| `Dependency(dependent, prerequisite)` | `x_dep ≤ x_prereq` | a company cycle may never be funded beyond the chat reserve; a final-edit item beyond its draft |
| `Bundle(discount, required_nodes)` | a rebate when every required node is fully funded | a same-vendor cache or batching saving when two activities share an endpoint |
| `budget` | one scalar net capital cap | the day's remaining tokens summed over keyed vendors (daily caps minus use) |
| `required_risk_reduction_pts` + `MinimizeCapitalObjective` | reach a target for the least capital | "deliver these goal points today for the fewest tokens": the natural mode here |
| `MonetaryNPVObjective` + `PriceBook` | maximise value in currency; prices must be sourced | usable only once a token has a stated value per goal point; the ADR is right that unsourced prices are assumptions |
| `AllocationConcaveResponse(0.85)` | diminishing returns to funding, tangent-approximated in the MILP | more items per cycle yield less per token; the exponent is uncalibrated in both repos |
| `SimulationRequest` + correlation | delivered value under correlated lognormal shocks | vendors failing together (one upstream, one region): the tail of what the society delivers |
| `attainable_frontier`, `diagnosis` | structural ceiling vs budget shortfall | "the academy cannot run today at any budget" vs "it needs 523,333 more tokens" |
| `sweep_budget` → `saturation_budget` | the lowest budget that buys everything worth buying | the daily share that the treasury slider should sit at |

## 4. Fit, use by use

### 4.1 Daily token treasury (fits; build this)

Today `economy.py` splits a daily share by fixed ratio and the tick defers on a bursty forecast.
Neither answers "which activities, at what size, so the chat never starves". The engine does,
in one solve per tick:

- Nodes: the activities above, costs estimated from the same per-item token estimates the
  society already uses; the chat reserve is a node with `min_funding_scale = 1.0`.
- Budget: remaining daily tokens across keyed vendors from the persisted ledger counters.
- Mode: `MinimizeCapitalObjective` with a goal-points floor per company plan; when infeasible,
  the diagnosis says exactly what is short, and the tick defers the cheapest-to-drop activity
  instead of all of them.
- The sweep's saturation budget becomes the recommended daily share shown next to the slider.
- Every plan carries the engine's audit record; the Bible's claim register can cite the hash.

### 4.2 Tail risk of delivery (fits; build after 4.1)

The proctor already simulates routing fragility. The engine adds the other half: given the
plan, how much of the day's value survives correlated vendor failure. Inputs exist: the
learner's outcome rates and 429 counts per endpoint give the shock size; `dynamics.py`'s
pairwise coupling gives the correlation matrix (`repair_to_correlation` keeps it valid). Output:
P50/P90 delivered value and the expected shortfall, next to the plan.

### 4.3 Per-request routing and per-minute pacing (does not fit; keep Cortex 2)

The engine has no time axis and no per-supplier capacity row. A Heavy send is three calls
within one minute against two different per-vendor ceilings; that is a scheduling problem,
solved today by Cortex 2's feasibility rows plus the Batch P pacing. Forcing it into the engine
would mean one solve per pass with a synthetic budget, and it would still not see the minute
window. Not worth it.

### 4.4 Calibration (later)

`calibrate_elasticity` fits an elasticity from delivery history and refuses when the history is
too thin. The analogue here is fitting each activity's goal points per token from the route log
and the verdicts, instead of typing them. The engine's refusal rule ("cannot support a fit")
is the right behaviour and matches the learner's Beta priors philosophy.

## 5. Gaps that decide the integration shape

1. **One scalar budget.** Per-vendor daily caps cannot be expressed as constraints; the plan
   allocates the pooled day and Cortex 2 enforces vendor feasibility call by call. A small,
   optional extension to the engine, a resource family `sum(usage_r,n · x_n) ≤ cap_r`, would make
   it a true multi-supplier planner. That is a change in the engine repository, proposed, not
   made here.
2. **No time.** Windows, rolling minutes, and refills are outside the model. The tick re-plans
   every interval, which is the right cadence for a day-level plan.
3. **Estimated costs.** Token demand per activity is an estimate; a re-plan after each cycle
   reconciles it. Same discipline the learner uses for speed.
4. **Uncalibrated exponent and unsourced prices.** Use the target-risk mode, which needs
   neither; leave NPV mode until goal points have a stated value.
5. **Python 3.12 only.** Ship as an optional extra (`requirements-supply.txt`, pinned to a
   commit via a git URL with `#subdirectory=engine`), import-guarded like pyspi, so CI on 3.11
   and hosts without it keep working and the planner reports "engine not installed".
6. **CBC is a subprocess.** 15–70 ms per plan is fine per tick; never call it per chat send.

## 6. Proposed batch (Q): the treasury planner

- `requirements-supply.txt`: `scrcae @ git+https://github.com/colegleason1-jpg/supply-chain-resilience-engine@<sha>#subdirectory=engine`.
- `orchestrator/treasury_plan.py`: `available()`, `build_network(scope, day)` from activities
  and the society's per-item estimates, `plan_day(scope, budget, goal_points)` → allocation,
  diagnosis, saturation budget, audit hash; `tail_risk(plan, coupling)` via the Monte Carlo;
  deterministic fallback (today's ratio split) when the engine is absent.
- `society/tick.py`: consult the plan before queueing; defer per activity with the shortfall
  in the cycle log; the chat reserve is never funded below full.
- `app.py`: Society settings panel "Plan of the day": allocation table, why each activity was
  sized as it was, the shortfall sentence when short, recommended daily share, P50/P90 delivered.
- Vault: `treasury_plans(day, scope, plan_json, audit_hash)` so a day's plan is reviewable.
- Tests: known-answer plan on a fixed network; infeasible day defers the right activity; engine
  absent → fallback and the panel says so; the chat reserve is never reduced; audit hash stable.
- Docs: README module row, RUNBOOK section, Bible claim register entry with the engine version.

Estimated size: one batch, no change to hard limits, no change to routing.
