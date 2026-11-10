# PID weight controller: simulated comparison

Simulation only (20 seeds per cell, 5 s ticks). Numbers describe the toy plant in `experiments/pid_comparison.py`, not the real gateway or AWS.

ML advisor: `{"available": true, "train_rows_used": 500, "heldout_rows_scored": 640, "heldout_accuracy": 0.975, "model": "k-NN (k=7), uncalibrated"}`

## local_latency_30s

experiments/scenarios/local-latency.json: region-a +1500 ms for 30 s

| controller | bad req % | mean ms | p95 ms | moves | travel pp | reversals | first move s | final A % | runs moved | moves on unknown | ctrl errors |
|---|---|---|---|---|---|---|---|---|---|---|---|
| no_action | 10.0 | 201.86 | 1547.78 | 0.0 | 0.0 | 0.0 | None | 100.0 | 0/20 | 0.0 | 0.0 |
| threshold | 8.74 | 175.58 | 1504.48 | 1.0 | 25.0 | 0.0 | 10.0 | 75.0 | 20/20 | 0.0 | 0.0 |
| pid | 9.61 | 190.96 | 1534.27 | 4.0 | 23.9 | 1.0 | 15.0 | 84.0 | 20/20 | 0.0 | 0.0 |
| ml_pid | 9.24 | 183.33 | 1521.74 | 3.9 | 29.0 | 1.0 | 15.0 | 75.0 | 20/20 | 0.0 | 0.0 |

## sustained_latency

same fault held for 300 s (longer than the gateway's 120 s cap)

| controller | bad req % | mean ms | p95 ms | moves | travel pp | reversals | first move s | final A % | runs moved | moves on unknown | ctrl errors |
|---|---|---|---|---|---|---|---|---|---|---|---|
| no_action | 50.0 | 808.95 | 1879.0 | 0.0 | 0.0 | 0.0 | None | 100.0 | 0/20 | 0.0 | 0.0 |
| threshold | 29.38 | 487.06 | 1777.2 | 3.0 | 75.0 | 0.0 | 10.0 | 25.0 | 20/20 | 0.0 | 0.0 |
| pid | 17.24 | 308.16 | 1672.68 | 14.9 | 103.3 | 1.0 | 15.0 | 3.0 | 20/20 | 0.0 | 0.0 |
| ml_pid | 9.04 | 185.88 | 1511.04 | 8.15 | 101.9 | 1.0 | 15.0 | 2.0 | 20/20 | 0.0 | 0.0 |

## capacity_loss

region-a keeps 40% of its capacity; neither region can carry everything

| controller | bad req % | mean ms | p95 ms | moves | travel pp | reversals | first move s | final A % | runs moved | moves on unknown | ctrl errors |
|---|---|---|---|---|---|---|---|---|---|---|---|
| no_action | 56.04 | 1262.19 | 3000.0 | 0.0 | 0.0 | 0.0 | None | 60.0 | 0/20 | 0.0 | 0.0 |
| threshold | 54.61 | 1282.81 | 3000.0 | 7.0 | 175.0 | 5.0 | 10.0 | 35.0 | 20/20 | 0.0 | 0.0 |
| pid | 2.77 | 195.73 | 198.81 | 6.0 | 40.65 | 1.0 | 15.0 | 29.0 | 20/20 | 0.0 | 0.0 |
| ml_pid | 2.72 | 197.39 | 211.72 | 5.25 | 39.1 | 1.0 | 15.0 | 28.0 | 20/20 | 0.0 | 0.0 |

## telemetry_gap

sustained fault, region-a telemetry stale from 120 s to 210 s

| controller | bad req % | mean ms | p95 ms | moves | travel pp | reversals | first move s | final A % | runs moved | moves on unknown | ctrl errors |
|---|---|---|---|---|---|---|---|---|---|---|---|
| no_action | 50.0 | 808.95 | 1879.0 | 0.0 | 0.0 | 0.0 | None | 100.0 | 0/20 | 0.0 | 0.0 |
| threshold | 31.86 | 524.74 | 1792.5 | 3.0 | 75.0 | 0.0 | 10.0 | 25.0 | 20/20 | 0.0 | 0.0 |
| pid | 26.57 | 448.82 | 1761.42 | 13.9 | 103.25 | 1.0 | 15.0 | 3.0 | 20/20 | 0.0 | 0.0 |
| ml_pid | 15.37 | 280.11 | 1646.84 | 7.7 | 101.9 | 1.0 | 15.0 | 2.0 | 20/20 | 0.0 | 0.0 |

## null_p95

healthy system, region-a p95 null for 200 s (histogram buckets unavailable)

| controller | bad req % | mean ms | p95 ms | moves | travel pp | reversals | first move s | final A % | runs moved | moves on unknown | ctrl errors |
|---|---|---|---|---|---|---|---|---|---|---|---|
| no_action | 0.0 | 37.91 | 47.93 | 0.0 | 0.0 | 0.0 | None | 50.0 | 0/20 | 0.0 | 0.0 |
| threshold | 0.0 | 37.91 | 47.93 | 0.0 | 0.0 | 0.0 | None | 50.0 | 0/20 | 0.0 | 40.0 |
| pid | 0.0 | 37.91 | 47.93 | 0.0 | 0.0 | 0.0 | None | 50.0 | 0/20 | 0.0 | 0.0 |
| ml_pid | 0.0 | 37.91 | 47.93 | 0.0 | 0.0 | 0.0 | None | 50.0 | 0/20 | 0.0 | 0.0 |

## noisy_healthy

no fault; 8% of ticks contain one slow probe per region

| controller | bad req % | mean ms | p95 ms | moves | travel pp | reversals | first move s | final A % | runs moved | moves on unknown | ctrl errors |
|---|---|---|---|---|---|---|---|---|---|---|---|
| no_action | 0.0 | 37.91 | 47.93 | 0.0 | 0.0 | 0.0 | None | 50.0 | 0/20 | 0.0 | 0.0 |
| threshold | 0.0 | 38.14 | 48.46 | 0.15 | 3.75 | 0.0 | None | 50.0 | 3/20 | 0.0 | 0.0 |
| pid | 0.0 | 37.91 | 47.97 | 4.3 | 9.05 | 2.75 | None | 50.0 | 18/20 | 0.0 | 0.0 |
| ml_pid | 0.0 | 38.03 | 48.26 | 3.65 | 12.25 | 2.0 | None | 50.0 | 18/20 | 0.0 | 0.0 |

## both_degraded

both regions +1500 ms service delay for 180 s

| controller | bad req % | mean ms | p95 ms | moves | travel pp | reversals | first move s | final A % | runs moved | moves on unknown | ctrl errors |
|---|---|---|---|---|---|---|---|---|---|---|---|
| no_action | 45.0 | 720.46 | 1846.7 | 0.0 | 0.0 | 0.0 | None | 50.0 | 0/20 | 0.0 | 0.0 |
| threshold | 45.0 | 720.46 | 1846.7 | 0.0 | 0.0 | 0.0 | None | 50.0 | 0/20 | 0.0 | 0.0 |
| pid | 45.0 | 720.47 | 1846.75 | 0.2 | 0.6 | 0.1 | 15.0 | 50.0 | 2/20 | 0.0 | 0.0 |
| ml_pid | 45.0 | 720.46 | 1846.7 | 0.0 | 0.0 | 0.0 | None | 50.0 | 0/20 | 0.0 | 0.0 |

## flapping

region-a +1500 ms, 20 s on / 40 s off, for 8 minutes

| controller | bad req % | mean ms | p95 ms | moves | travel pp | reversals | first move s | final A % | runs moved | moves on unknown | ctrl errors |
|---|---|---|---|---|---|---|---|---|---|---|---|
| no_action | 26.67 | 454.86 | 1769.91 | 0.0 | 0.0 | 0.0 | None | 100.0 | 0/20 | 0.0 | 0.0 |
| threshold | 12.48 | 233.49 | 1600.54 | 4.0 | 100.0 | 0.0 | 10.0 | 0.0 | 20/20 | 0.0 | 0.0 |
| pid | 20.11 | 346.58 | 1708.72 | 18.85 | 134.05 | 15.0 | 15.0 | 43.0 | 20/20 | 0.0 | 0.0 |
| ml_pid | 9.98 | 197.32 | 1539.32 | 18.15 | 135.65 | 15.0 | 15.0 | 2.0 | 20/20 | 0.0 | 0.0 |

## drain_blind_spot

sustained fault; a region is only observable through the traffic it receives

| controller | bad req % | mean ms | p95 ms | moves | travel pp | reversals | first move s | final A % | runs moved | moves on unknown | ctrl errors |
|---|---|---|---|---|---|---|---|---|---|---|---|
| no_action | 25.03 | 417.88 | 1745.77 | 0.0 | 0.0 | 0.0 | None | 50.0 | 0/20 | 0.0 | 0.0 |
| threshold | 9.71 | 192.51 | 1507.89 | 1.85 | 46.25 | 0.0 | 13.0 | 0.0 | 20/20 | 0.0 | 0.0 |
| pid | 5.26 | 126.91 | 803.47 | 4.75 | 49.65 | 0.1 | 11.5 | 0.0 | 20/20 | 0.0 | 0.0 |
| ml_pid | 3.62 | 102.38 | 285.0 | 2.75 | 49.05 | 0.25 | 11.5 | 0.0 | 20/20 | 0.0 | 0.0 |

## PID gain sweep on capacity_loss (kp and ki scaled together)

| gain | bad req % | p95 ms | moves | travel pp | reversals | final A % |
|---|---|---|---|---|---|---|
| kp,ki x0.25 | 10.67 | 2879.12 | 16.75 | 30.75 | 2.75 | 32.0 |
| kp,ki x0.5 | 5.14 | 362.2 | 8.0 | 30.0 | 1.0 | 32.0 |
| kp,ki x1 | 2.77 | 198.81 | 6.0 | 40.65 | 1.0 | 29.0 |
| kp,ki x2 | 1.91 | 206.94 | 4.0 | 47.65 | 1.0 | 28.0 |
| kp,ki x4 | 57.93 | 3000.0 | 55.0 | 1373.85 | 53.0 | 36.0 |
| kp,ki x8 | 57.47 | 3000.0 | 55.0 | 1374.75 | 53.0 | 35.0 |
| I only (kp=0) | 3.61 | 321.75 | 6.0 | 39.0 | 0.0 | 21.0 |
| with D (kd=20) | 2.73 | 198.12 | 6.0 | 42.9 | 2.0 | 29.0 |

## PID variants

| scenario / variant | bad req % | p95 ms | moves | travel pp | reversals | final A % |
|---|---|---|---|---|---|---|
| flapping / pid | 20.11 | 1708.72 | 18.85 | 134.05 | 15.0 | 43.0 |
| flapping / I only (kp=0) | 20.1 | 1708.67 | 11.75 | 56.6 | 0.0 | 43.0 |
| noisy_healthy / I only (kp=0) | 0.0 | 48.05 | 2.2 | 3.85 | 0.75 | 50.0 |
| sustained_latency / failback 0.2 pp/s to 100 | 17.24 | 1672.68 | 20.9 | 121.3 | 1.0 | 21.0 |
| sustained_latency / canary floor 5% | 18.08 | 1682.54 | 14.2 | 98.25 | 1.0 | 8.0 |
| sustained_latency / 60 s sample interval | 25.89 | 1756.19 | 5.0 | 102.05 | 1.0 | 2.0 |
| drain_blind_spot / failback 0.2 pp/s to 50 | 5.26 | 803.47 | 4.75 | 49.65 | 0.1 | 0.0 |
| drain_blind_spot / failback + canary floor 30% | 15.78 | 1647.83 | 3.85 | 25.25 | 1.0 | 35.5 |
