# PID traffic-weight controller (shadow prototype)

**Status:** local, shadow-only prototype. It proposes a `region_a_percent`; nothing in it writes to the
gateway, and nothing touches AWS. All results below come from a toy simulator, not from the real system.

| File | What it is |
|---|---|
| `services/adaptivenet/controller/pid.py` | The controller. Pure arithmetic, imports only the standard library. |
| `experiments/pid_comparison.py` | Offline simulator: no action vs threshold rule vs PID vs ML-guided PID. |
| `experiments/results/pid-comparison/` | `summary.md`, `results.json`, `traces.json` from the last run (20 seeds). |
| `tests/test_pid_controller.py`, `tests/test_pid_simulation.py` | 70 offline tests. |

```bash
python experiments/pid_comparison.py --out experiments/results/pid-comparison
python -m pytest tests/test_pid_controller.py tests/test_pid_simulation.py -q
```

## 1. Why a PID at all

`controller/engine.py` is a bang-bang rule: after 3 consecutive "degraded" ticks it proposes 0% or 100%,
`routes.py` walks there in 25-point steps with a 120 s cooldown, and it never fails back. That is right for
a dead region. It is wrong for a region that is merely *slower or smaller*, because the only places it can
stop are multiples of 25 points from where it started, and it has no notion of "enough".

A PID controller treats the weight as a continuous actuator and stops when the measured error is zero. It
is the natural tool when **latency depends on how much traffic a region receives** (capacity loss, noisy
neighbour, partial scale-in), because then a stable split exists and the loop can find it.

## 2. Design

**Process variable.** Per region, `pressure = max(p95_ms / target_p95_ms, error_ratio / target_error_ratio)`
(1.0 = exactly on target; defaults 400 ms and 0.05, deliberately below the engine's 800 ms / 0.20 "degraded"
line). `excess = clip(pressure - 1 - deadband, 0, error_clip)`. The loop error is
`excess(region-b) - excess(region-a)`: positive means region-a deserves more traffic. If both regions are
over target by a similar amount (within 25%), the error is forced to 0, because moving traffic between two
equally bad regions cannot help.

**Controller form.** Velocity (incremental) PI, D optional and off by default:

```
delta  = gain_scale * ( kp*(e - e_prev) + ki*e*dt + kd*(e - 2e_prev + e_prev2)/dt )
weight = clamp( applied_weight + clamp(delta, +-max_step_pct), min_weight, max_weight )
```

| Requirement | How it is met |
|---|---|
| Output clamping | `min_weight..max_weight` (default 0..100), plus a slew limit of at most 25 points per sample, the same limit `decisions.review_proposal` enforces. Larger values are rejected in `PIDConfig`. |
| Anti-windup | There is no separate integrator. The integral *is* the weight, so clamping the weight clamps the integral, and every step is re-based on the weight that is **actually applied**. A shadow controller whose proposals are never applied therefore cannot wind up, and an operator override or a kill-switch-blocked write is tracked bumplessly. A side effect: the state survives a process restart, because it lives in the gateway, not in memory. |
| Slow sample interval | `sample_interval_s` (default 15 s). `step()` can be called on every fast tick; it decides once per interval on the **median** pressure of the ticks seen since the last decision, so one slow probe cannot move traffic. |
| Hold on null / stale | `region_pressure()` returns `None` (unknown) when status is not `ok`, age is missing or over 90 s, samples < 3, or a value is NaN/inf/negative. If p95 **or** the error ratio is null, the other signal is trusted only when it already proves a breach; "one signal missing, the other looks fine" is unknown. On unknown the controller returns `hold` with the current weight, integrates nothing, drops derivative and filter history, and caps `dt` so a 30-minute gap is one bounded step, not 30 minutes of banked integral. |
| Freshness gates | `observations_from_model_input()` maps `adaptivenet.model_input.v1` to observations and marks a region unknown unless `ready_for_inference` is true and its `quality.internal` is `fresh`. |
| Shadow only | `to_proposal()` wraps the result as `adaptivenet.proposal.v1`, so `review_proposal` judges it like any model output: `executed=false`, `effective_action=hold`. A test asserts the module imports nothing but `dataclasses`, `math`, `statistics`. |
| No surprise failback | Off by default, like the engine. Optional `failback_rate_pct_per_s` drifts toward `home_weight` only after both regions have been known-and-on-target for `failback_after_s`. |

**ML-guided variant.** `advisor_gain_scale(label_a, label_b)` turns a per-region diagnosis
(`healthy | network | compute | critical` with a confidence) into a gain multiplier in `[0, 2]`: both regions
impaired gives 0 (freeze), a path or hard failure on one region gives `1 + confidence`, `compute` gives 1
(scale-out is the real remedy), "model sees nothing wrong" gives 0.5. Invalid or low-confidence advice gives
1.0, the plain PID. The advisor can slow, freeze or speed up the loop; it cannot move the weight, widen the
clamp, raise the slew limit or make the loop act on unknown data.

## 3. Simulated comparison

Plant: per region `latency = 30 ms / (1 - rho)` with `rho = share * load / capacity`, 3 s timeout, shedding
above `rho = 1`; faults are path delay / path loss (user path only), service delay, capacity loss, stale
telemetry and null p95. Observations use the collector's shape and its 5-probe p95. The threshold arm is the
real `engine.Controller` driven exactly as `/controller/tick` drives it with automation on. The ML arm is a
dependency-free k-NN trained on the train split of the local, git-ignored `data/ticks.jsonl` (97.5% on a
held-out sample of the same synthetic data; confidence is a vote share, not calibrated). 20 seeds per cell.
"Bad request" = error or slower than 800 ms.

| Scenario | No action | Threshold | PID | ML-guided PID |
|---|---|---|---|---|
| **local_latency_30s** (the repo scenario: A +1500 ms for 30 s) | 10.0% bad | 8.7%, ends at A=75 | 9.6%, ends at A=84 | 9.2%, ends at A=75 |
| **sustained_latency** (same fault for 300 s) | 50.0% | 29.4%, ends at A=25 | 17.2%, ends at A=3 | 9.0%, ends at A=2 |
| **capacity_loss** (A keeps 40%; neither region can carry all) | 56.0% | 54.6%, 5 reversals | **2.8%**, settles at A=29 | 2.7%, settles at A=28 |
| **telemetry_gap** (A stale for 90 s mid-fault) | 50.0% | 31.9% | 26.6%, 0 moves while unknown | 15.4%, 0 moves while unknown |
| **null_p95** (healthy, A's p95 null for 200 s) | 0 moves | 0 moves, **engine raised 40 times per run** | 0 moves | 0 moves |
| **noisy_healthy** (no fault, 8% slow probes) | 0 moves | 3 of 20 runs made a 25-point move and kept it | 18 of 20 runs moved, 9 points of total travel, ends at 50 | 12 points of travel |
| **both_degraded** | 45.0% | holds | holds (0.6 points of travel) | holds (frozen by advisor) |
| **flapping** (20 s on / 40 s off) | 26.7% | **12.5%**, ratchets to A=0 | 20.1%, 15 reversals, ends at A=43 | 10.0%, 15 reversals |
| **drain_blind_spot** (region only visible through its own traffic) | 25.0% | 9.7% | 5.3% | 3.6% |

What the numbers say:

1. **PID wins clearly only where the theory says it should**: the load-dependent case. The threshold rule
   hops 60 → 35 → 10 → 35 ... because no multiple of 25 lands in the feasible band (roughly A = 14–35%), and
   ends up no better than doing nothing. The PID settles in the band in about 6 moves.
2. **For a load-independent fault the PID is just a smoother ramp to the clamp.** The error does not respond
   to the weight, so there is no equilibrium to find. It still beats the threshold rule on a long fault,
   but only because it is not held back by a 120 s cooldown per 25 points.
3. **Nothing helps with the 30 s scenario.** The fault is over before any safe loop has confirmed it. Both
   controllers shave about one point off the bad-request rate and then leave traffic displaced for good.
4. **The threshold rule is better on a flapping fault.** It ratchets away and stays. The PID's median window
   rejects part of a 20 s burst as noise, and its P term gives some weight back each time the fault pauses.
5. **The PID is busier on a healthy but noisy system**: many small moves that net to zero, versus rare
   25-point moves that stick. Which is worse depends on what a weight change costs in the real actuator.
6. **The "ML" gain is mostly just gain.** Its win on latency faults comes from doubling the loop gain when
   the label is `network`. That is only safe while the label is right and the loop has gain margin (below).

Tuning sensitivity on `capacity_loss` (kp and ki scaled together): x0.25 sluggish (10.7% bad), x0.5 5.1%,
x1 2.8%, x2 1.9%, **x4 and x8 limit-cycle (58% bad, 53 reversals)**. The stable window is only about 4x
wide because queueing latency is steeply non-linear near saturation. I-only (`kp=0`) is quieter everywhere
(0 reversals on flapping, 4 points of travel on noise) at the cost of some overshoot (settles at A=21).
A 60 s sample interval roughly halves the benefit on the sustained fault (25.9% vs 17.2%).

## 4. Where PID does not apply

- **Hard outages.** A dead region needs a fast binary failover, not a ramp. Keep the threshold rule (or a
  health check) for that and let the PID trim only when both regions are alive.
- **Faults that do not depend on load** (path delay, packet loss). No equilibrium exists; PID degenerates
  into a rate-limited failover.
- **Faults shorter than the loop time.** With slow sampling plus confirmation, anything under about a minute
  is over before the loop acts.
- **Both regions degraded, or either one unknown.** Hold.
- **Capacity shortfall overall.** If the two regions together cannot carry the load, the right action is
  scale-out (the orchestrator's `scale_out`), not rebalancing.
- **Actuators that are not continuous or have long dead time.** CloudFront origin groups are failover-only,
  and DNS-weighted routing lags by TTL and resolver caching. The sample interval must exceed the actuator's
  dead time or the loop will oscillate regardless of tuning. This prototype has only been exercised against
  an instantaneous simulated actuator.

## 5. Other things noticed while reading the code

1. **`engine.py` raises `TypeError` on a null p95 with a low error ratio** (`s.get('p95_ms', inf)` returns
   `None` when the key is present). The internal collector only emits a null p95 together with
   `error_ratio = 1`, so it is latent today, but any other source would turn `/controller/tick` into a 500.
   Reproduced in the simulator; pinned by `test_threshold_engine_raises_on_null_p95_with_low_error_ratio`.
2. **`mitigation/orchestrator.classify_health` treats a null p95 as `degraded`**, not unknown. That
   contradicts the "null is unknown" rule in `TEAM_HANDOFF.md` and can fail recovery verification on a
   telemetry gap.
3. **The controller probably cannot see the local fault scenario.** `compose.yaml` points
   `INTERNAL_REGION_*_URL` at `demo-a/demo-b` directly, while `gateway.py` injects latency inside the
   gateway. Read from the code, not confirmed by running the stack.
4. **The collector's p95 over 5 probes is the maximum.** It is a very noisy input for any controller. More
   probes, or a windowed median as the PID uses, would help the threshold rule too.
5. **`collect()` hard-codes `age_seconds: 0`,** so the engine's 90 s freshness gate can never trip on
   collector data.
6. **A drained region goes blind** when health is derived from its own traffic. With failback enabled and no
   floor, the simulated PID stayed at A=0 forever; a 30% canary floor let it return (A=35) but cost
   15.8% bad requests instead of 5.3% during the fault. A dedicated per-region probe avoids the trade.
7. **The threshold rule has no failback and no intermediate target**, which is the root of the limit cycle
   in the capacity scenario.

## 6. Risks and limits of this prototype

- Every result is from a toy plant with an instantaneous actuator and synthetic noise. Gains tuned here mean
  nothing for AWS until re-identified against real step responses.
- The ML arm is trained and scored on synthetic local data; 97.5% says the simulator is easy, not that a
  real classifier would be this good. A wrong `network` label doubles the gain of a loop with roughly 4x margin.
- Two inputs are folded into one with `max()`. That is simple, but it means the loop cannot tell a latency
  breach from an error breach, and the effective plant gain changes when the dominant signal switches.
- `to_proposal` sets `confidence: 1.0` only because the rule is deterministic. It is not a probability.
- Not wired into `/controller/tick`, the orchestrator or the dashboard. That is deliberate.

## 7. Suggested next steps

1. Run it in shadow next to the threshold rule and journal both proposals; compare on recorded data.
2. Fix items 1 and 2 above (small, independent of PID).
3. If it ever actuates: threshold rule for outages, PID as a trim inside a band (for example 20–80%),
   I-only to start, gain scheduling or a log-latency error to tame the non-linearity, and the existing
   kill switch, audit log and rate limit in front of every write.
