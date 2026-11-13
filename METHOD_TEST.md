# Testing the "your area" methods — `method_test.py`

The dashboard turns readings from several nearby monitors into one number for **your area**. It can do that three ways:

| Method | How | Strength | Weakness |
|---|---|---|---|
| **Median** | middle value of the monitors that reported that hour | one or two faulty monitors barely move it | ignores distance |
| **Average** | plain mean | uses every reading equally | one bad monitor pulls it |
| **Distance-weighted** (IDW) | mean weighted by 1 / distance² from your location | nearer monitors count more | if the nearest monitor is faulty, the result is faulty |

`method_test.py` measures which one is actually best **for your monitors, on your recorded data**.

---

## The problem it solves

There's no monitor at your house, so there's no "true" value to check the area number against.

## The trick: leave-one-out cross-validation

Every monitor *is* a location with a known true value. So, for every hour:

1. **Hide** one monitor.
2. **Estimate** its reading from the other monitors, with each method.
3. **Compare** the estimate with what that monitor actually measured.

Do this for every monitor and every hour (thousands of predictions). The method with the smallest typical error is the best at estimating a spot with no monitor — like your house.

For the distance-weighted method, distances are measured **from the hidden monitor** to the others, exactly as if the hidden monitor's location were your house.

## Second test: fault injection

Pretend one monitor misbehaves — reads **×0.5** (e.g. water sprinklers next to it) or **×2** (e.g. a fire next to it) — and measure how far the area value at **your** location moves, in %. Each monitor is corrupted in turn, every hour. A good method should hardly move.

---

## How to run it

From the `aqi-dashboard` folder, in PowerShell:

```powershell
.venv\Scripts\python method_test.py
```

Options:

| Option | Example | What it does |
|---|---|---|
| `--from` / `--to` | `--from 2026-11-01` | limit to a date range (local dates) |
| `--min-monitors` | `--min-monitors 4` | only hours where at least N monitors reported (default 3) |
| `--power` | `--power 1` | change the distance weighting (1 = gentler, 2 = default, 3 = steeper) |
| `--faults` | `--faults 0.5 0.7 1.5 2` | which corruption factors to test |

Only the Python standard library is used. Nothing is downloaded — it reads the CSVs the app already recorded.

## Reading the output

**Test 1 — leave-one-out** (PM2.5 in µg/m³, lower is better)

- **MAE** (mean absolute error): the typical size of a miss. The main number to compare.
- **RMSE**: like MAE but punishes big misses more. If RMSE is much bigger than MAE, the method sometimes misses badly.
- **bias**: average signed error. Positive = the method over-estimates on average.

**Per-monitor table**: how well each monitor is predicted by its neighbours.
- A large positive bias means the monitor **reads lower** than its neighbours; negative means it **reads higher**.
- A monitor with a big MAE and big bias is out of line with the rest of the area — a siting problem, a local source, or a faulty instrument.

**Test 2 — fault injection**: average and worst-case % the area value moves when one monitor is corrupted. Lower = more robust. Averaged over which monitor fails, *average* and *distance-weighted* always move by the same amount (a maths identity); the worst case shows the difference — distance-weighted suffers most when its heavily-weighted nearest monitor is the faulty one.

**Weights table**: how much each monitor counts in the distance-weighted average at your location. If one monitor carries a large share, the distance-weighted result mostly reflects that monitor.

**`data/method_test_results.csv`**: every single prediction (hour, hidden monitor, actual, method, estimate, error) — open it in Excel to plot or filter.

## Limits — what this test can't tell you

- It shows how well each method **agrees with the monitors**. If all monitors share the same bias (e.g. all read low), no method — and no test using only these monitors — can detect it.
- It estimates **monitor locations**, which stand in for your house. Your house could still differ (e.g. a busy road next to it).
- Results depend on the monitors and the season. Re-run it every few weeks; the app also refreshes `data/evaluation_report.txt` daily with the same tests.
