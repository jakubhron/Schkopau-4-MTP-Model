# Schkopau MTP – The Complete Model Explained

*A plain-language guide to the power-plant dispatch optimisation model, written so that anyone with high-school maths can follow along.*

---

## Page 1 – What Is This Model and Why Does It Exist?

### The plant in one paragraph

Schkopau is a **lignite-fired combined-heat-and-power (CHP) plant** located in Germany.  It has **two generating blocks** (called **Block A** and **Block B**).  Each block can burn coal to produce electricity, and at the same time the plant delivers **steam (heat)** – labelled **DOW** (Dampf- und Wärmelieferung) – to a neighbouring industrial consumer.  The plant sells its electricity on the wholesale power market, earns revenue from the DOW steam delivery, and pays for coal, CO₂ emission allowances, start-up wear, grid fees, and its own auxiliary ("house") power consumption.

### What the model does

The model answers one big question:

> **For every single hour of a planning horizon (typically March to December – about 6 600 hours), should each block be ON or OFF, and if ON, how much power should it produce?**

The answer must maximise the plant's total **profit** (revenue minus costs) while respecting dozens of physical and contractual rules: minimum run times, minimum down times, start-up ramp limits, monthly coal-burn caps, and more.

### Why we need a computer for this

You might think: "just run whenever the electricity price is high."  But it is not that simple.  Starting a block costs money and takes several hours of ramping.  Once you start, you must stay on for at least 8 hours.  Once you stop, you must stay off for at least 6 hours.  Additionally, you only have a limited amount of coal each month, so you cannot simply run flat-out whenever prices are positive.  These interlocking rules make the problem far too complex for a spreadsheet – it is a **Mixed-Integer Linear Programme (MILP)** solved by the commercial solver **MOSEK**.

### The 30-second version of the workflow

```
  Input Excel  ──►  Load & Prepare  ──►  Build Pyomo Model  ──►  Solve (MOSEK)
       │                                                              │
       │         ◄── Extract Results ◄── Check Termination  ◄────────┘
       │                │
       └── Write Excel Report (with shadow-price analysis)
```

1. **Load** hourly price curves, block parameters, start-up tiers, and coal limits from an Excel workbook.
2. **Build** a mathematical optimisation model in *Pyomo* (a Python modelling library).
3. **Solve** the model using the MOSEK solver.
4. **Extract** the optimal hourly schedule, compute profit components, and **write** a result Excel.

---

## Page 2 – The Inputs: What Data Does the Model Need?

Think of the input workbook as the model's "recipe card."  It has four tabs (sheets):

### 2.1  Block_A / Block_B tabs (one per generating unit)

Each tab contains **one row per hour** of the planning horizon.  The key columns are:

| Column | Meaning | Example |
|--------|---------|---------|
| **Date** | Calendar date of the hour | 2026-04-01 |
| **Hour** | Hour of the day (0–23) | 14 |
| **Price** | Day-ahead wholesale electricity price (EUR/MWh) | 62.30 |
| **EUA** | Price of one EU emission allowance (EUR per tonne of CO₂) | 68.50 |
| **Coal price** | Price of the coal blend (EUR per tonne or per MWh-thermal, depending on convention) | 12.40 |
| **Grid fee (GRIDFEE)** | Network usage fee the plant pays when it draws power from the grid while offline (EUR/MWh) | 3.00 |
| **Warme / DOW** | Thermal steam load delivered to the industrial neighbour (MW-thermal) | 130 |
| **Pmin** | Minimum electrical output when a block is running (MW) | 110 |
| **Pmax** | Maximum electrical output when a block is running (MW) | 210 |
| **TC_PminN** | Total cost per MWh at minimum load (EUR/MWh) | 48.20 |
| **TC_Pmax** | Total cost per MWh at full load (EUR/MWh) | 42.10 |
| **coal_factor_pmin** | Coal burned (tonnes/h) when running at Pmin | 65 |
| **coal_factor_pmax** | Coal burned (tonnes/h) when running at Pmax | 120 |
| **unavailibility** | 1 = block is in planned outage this hour, 0 = available | 0 |

#### How cost curves are built

The model needs to know *how much it costs to run a block at any power level P*.  The input gives costs at two extreme points (Pmin and Pmax), and the model draws a straight line between them:

```
                     Cost (EUR/h)
                        ▲
        Cmax = TC_Pmax × Pmax ─ ─ ─ ─ ─ ─ ─ ●
                        │                   ╱
                        │                 ╱    ← this line is cost_slope × P + cost_fixed
                        │               ╱
        Cmin = TC_PminN × Pmin ─ ─ ─ ●
                        │
                        └──────────────────────► Power P (MW)
                                Pmin         Pmax
```

$$\text{cost\_slope} = \frac{C_{max} - C_{min}}{P_{max} - P_{min}}, \qquad \text{cost\_fixed} = C_{min} - \text{cost\_slope} \times P_{min}$$

So the running cost in any hour is:

$$\text{run\_cost}(t) = \text{cost\_slope}(t) \times P_{\text{eff}}(t) + \text{cost\_fixed}(t) \times \mathbb{1}[\text{on}]$$

The same idea is used for coal consumption: a straight line from coal_factor_pmin to coal_factor_pmax.

### 2.2  Starts tab (start-up tiers)

Starting a cold boiler is much more expensive and slower than restarting one that only stopped a few hours ago.  The Starts tab defines **five start-up tiers** for each block:

| Tier | Hours offline | Typical cost (EUR) | Ramp behaviour |
|------|--------------|-------------------|----------------|
| **Very hot** | 0 – 5 h | Low | Fast ramp-up |
| **Hot** | 5 – 10 h | Medium-low | Moderate ramp |
| **Warm** | 10 – 60 h | Medium | Slower ramp |
| **Cold** | 60 – 100 h | High | Slow ramp |
| **Very cold** | 100+ h | Highest | Slowest ramp |

Each tier provides a **ramp profile** (power output for hours 0 through 3 after starting) and a **lump-sum start-up cost** in EUR.

### 2.3  Coal_constrains tab (monthly coal limits)

This simple table says: *"In April 2026 you may burn at most X thousand tonnes of coal across both blocks combined."*

| Year | Month | Limit (kt) |
|------|-------|-----------|
| 2026 | 4     | 90        |
| 2026 | 5     | 95        |
| …    | …     | …         |

These limits are the heart of the scarcity problem and will be explained in depth on Pages 5–6.

---

## Page 3 – How the Model Works: Variables, Objective, and the Idea of "Maximise Profit"

### 3.1  Decision variables – the knobs the optimiser can turn

For **every hour t** and **every block b**, the model must choose:

| Variable | Type | Meaning |
|----------|------|---------|
| $\text{on}_{b,t}$ | **Binary** (0 or 1) | Is block b running in hour t? |
| $P_{b,t}$ | **Continuous** (MW) | Electrical output of block b in hour t |
| $\text{startup}_{b,t}$ | **Binary** | Did block b just turn on in hour t? |
| $\text{shutdown}_{b,t}$ | **Continuous** [0,1] | Did block b just turn off in hour t? |
| $\text{hot\_start}_{b,t}$ | **Binary** | Is this startup a *hot* start? |
| $\text{vcold\_start}_{b,t}$ | **Binary** | Is this startup a *very-cold* start? |
| $\text{both\_on}_t$ | **Continuous** [0,1] | Are **both** blocks on at once? |
| $\text{plant\_off}_t$ | **Continuous** [0,1] | Are **both** blocks off (entire plant dark)? |

Why "binary"?  A binary variable can only be 0 or 1 – like a light switch.  You cannot half-start a turbine.  The *P* variable is continuous because you can set any power level between Pmin and Pmax.

### 3.2  Effective power and DOW

The industrial neighbour draws steam (DOW) from whichever block is running.  When both blocks run, one block "owns" the full DOW load; only if that primary block (Block A) shuts down does the other block pick up the steam load.  The model captures this with:

$$P_{\text{eff},A,t} = P_{A,t} + \text{DOW}_t \times \text{on}_{A,t}$$

$$P_{\text{eff},B,t} = P_{B,t} + \text{DOW}_t \times (\text{on}_{B,t} - \text{both\_on}_t)$$

$P_{\text{eff}}$ is the *total thermal-equivalent load on the boiler* (electrical output plus steam).  It is this effective power that determines fuel consumption and running costs.

### 3.3  The objective function – "make as much money as possible"

The model **maximises total profit** over all hours:

$$\max \sum_{t} \Bigl[\underbrace{P_{b,t} \times \text{Price}_t}_{\text{electricity revenue}} - \underbrace{\text{run\_costs}_{b,t}}_{\text{fuel + CO₂ + variable}} - \underbrace{\text{start\_cost}_{b,t}}_{\text{wear \& tear}} - \underbrace{\text{OFF\_costs}_t}_{\text{house power if off}} + \underbrace{\text{DOW\_rev}_t}_{\text{steam income}}\Bigr]$$

Let us break that down piece by piece:

1. **Electricity revenue** — the power you produce times the market price.  Positive prices earn you money; negative prices (yes, they happen!) cost you money for every MWh produced.

2. **Running costs** — the linearised fuel + emissions cost: $\text{cost\_slope} \times P_{\text{eff}} + \text{cost\_fixed} \times \text{on}$.  This includes coal, CO₂ allowances, variable O&M, and other operating charges embedded in TC_PminN and TC_Pmax.

3. **Start-up costs** — a lump sum charged when `startup = 1`.  The amount depends on the tier:
   - Warm cost is the baseline: $(\text{warm\_cost} + \text{START\_MARGIN\_MIN}) \times \text{startup}$
   - Hot start adjusts: $+(\text{hot\_cost} - \text{warm\_cost}) \times \text{hot\_start}$
   - Very-cold start adjusts: $+(\text{vcold\_cost} - \text{warm\_cost}) \times \text{vcold\_start}$

4. **OFF costs** — even when the entire plant is dark, it still draws auxiliary power from the grid (own consumption of ~10 MW, plus ~130 MW for the DOW consumer's electric backup if DOW opportunity-cost mode is on).  The plant pays for every MWh at (Price + Grid fee).

5. **DOW revenue** — steam delivery income credited whenever at least one block is on.

### 3.4  DOW opportunity costs

When the plant is fully offline, the industrial DOW consumer switches to its own electric heating, drawing roughly 130 MW from the grid.  The model can be configured to account for this:
- A partial compensation of 6.9 EUR/MWh is deducted (the plant reimburses the neighbour).
- The full 130 MW of grid draw is charged at market price + grid fee.
This makes shutting down more expensive than it first appears, accurately reflecting the contractual reality.

---

## Page 4 – The Rules: Every Constraint the Plant Must Obey

Constraints are the "guardrails" that prevent the optimiser from doing impossible things.  Here is every family of rules:

### 4.1  Availability

$$\text{on}_{b,t} \le 1 - \text{unavailibility}_{b,t}$$

If a block is in a planned outage (unavailibility = 1), it **cannot** be on — a straightforward binary exclusion.

### 4.2  Start-up / shut-down logic

The on/off status must change consistently:

$$\text{startup}_{b,t} - \text{shutdown}_{b,t} = \text{on}_{b,t} - \text{on}_{b,t-1}$$

If the block goes from OFF to ON, startup = 1.  From ON to OFF, shutdown = 1.  You cannot start and shut down in the same hour.

Additional rules prevent nonsensical situations:
- You can only start up if you were off in the previous hour.
- A startup implies you must be on in the current hour.

### 4.3  Minimum up-time (8 hours)

$$\sum_{k=0}^{7} \text{on}_{b,t+k} \;\ge\; 8 \times \text{startup}_{b,t}$$

Once you start a block, you must keep it running for **at least 8 consecutive hours**.  This reflects the physical reality that rapid cycling damages boiler components.

### 4.4  Minimum down-time (6 hours)

$$\sum_{k=0}^{5} (1 - \text{on}_{b,t+k}) \;\ge\; 6 \times (\text{on}_{b,t-1} - \text{on}_{b,t})$$

Once you shut down, you must stay off for **at least 6 hours** to let the turbine cool safely.

### 4.5  Power bounds

When a block is ON, its output must stay between Pmin and Pmax:

$$P_{b,t} \ge P_{\min,b,t} \times \text{on}_{b,t} + \text{boost} \times \text{both\_on}_t$$

$$P_{b,t} \le P_{\max,b,t} \times \text{on}_{b,t} + \text{boost} \times \text{both\_on}_t$$

The **boost** (5 MW by default) accounts for the slightly higher capability when both blocks share auxiliary systems.  The upper bound is adjusted to subtract the DOW steam load so that the block does not exceed its electrical Pmax after accounting for steam extraction.

### 4.6  Startup / shutdown power pinning

In the hour you start up, you must produce exactly Pmin (+ boost if both on):

$$P_{b,t} = P_{\min,b,t} \times \text{on}_{b,t} + \text{boost} \times \text{both\_on}_t \qquad \text{(when startup}_{b,t} = 1\text{)}$$

Similarly, the hour *before* you shut down, you must reduce to Pmin.  This is implemented with Big-M constraints that activate only when the startup or shutdown indicator is 1.

### 4.7  Start-up tier detection

How does the model know whether a start is hot, warm, or very cold?  It counts how long the block has been off:

- **Hot start**: the block was ON somewhere in the previous 10 hours ($\text{on}_{b, t-10} = 1$).
- **Very cold start**: the block was OFF at every 8-hour checkpoint over the past 56 hours (checks at offsets 8, 16, 24, 32, 40, 48, 56).
- **Warm start**: everything in between (the "default" tier).

Binary variables `hot_start` and `vcold_start` are linked to `startup` so that if a start occurs, exactly one tier applies.

### 4.8  Both-on and plant-off linearisation

`both_on` and `plant_off` are *auxiliary* variables.  Their values are forced by linearisation constraints:

$$\text{both\_on}_t \le \text{on}_{A,t}, \quad \text{both\_on}_t \le \text{on}_{B,t}, \quad \text{both\_on}_t \ge \text{on}_{A,t} + \text{on}_{B,t} - 1$$

This ensures `both_on = 1` if and only if **both** blocks are simultaneously on.  Analogous constraints define `plant_off = 1` only when **both** blocks are off.

### 4.9  Optional: start-up ramp profile

When enabled (via `USE_SIMPLE_STARTUP_RAMP = False`), the model limits the power output during the first few hours after a start to follow the physical ramp profile read from the Starts tab.  Each tier has its own ramp: a hot start ramps faster than a very-cold start.  The `in_ramp` variable tracks whether a block is still in its ramp-up window.

---

## Page 5 – Coal Limits: The Scarcity That Shapes Everything

### 5.1  Why coal limits matter

Consider a plant rated at 420 MW of combined electrical capacity, yet allocated only 90 000 tonnes of coal for a given month.  At full load, both blocks together consume roughly 240 tonnes per hour — enough to exhaust the entire monthly budget in under 16 days.  The plant therefore cannot simply dispatch at maximum output whenever the electricity price is positive.

More subtly, coal allocation is not just a question of *whether* to run, but of *when* and *at what level*.  A block at full load burns significantly more coal per hour than a block at minimum load.  The optimiser must allocate the limited coal stock to the hours where each tonne generates the highest marginal profit, balancing load levels, minimum-run-time commitments, and price volatility across the full month.

### 5.2  How coal consumption is modelled

Just like running costs, coal consumption is linearised between Pmin and Pmax:

$$\text{coal}_{b,t} = \text{coal\_slope}_{b,t} \times P_{\text{eff},b,t} + \text{coal\_fixed}_{b,t} \times \text{on}_{b,t}$$

Where:
- $\text{coal\_slope}$ = $(C_{\text{coal,max}} - C_{\text{coal,min}}) / (P_{\text{max}} - P_{\text{min}})$
- $\text{coal\_fixed}$ = $C_{\text{coal,min}} - \text{coal\_slope} \times P_{\text{min}}$

Note that this uses **effective power** ($P_{\text{eff}}$), which includes the DOW steam load.  This is important because the DOW steam is produced from the same coal fire – burning coal to make steam counts against the monthly limit even though the electricity market does not directly see it.

### 5.3  The monthly constraint

For each month $(y, m)$ that has a limit in the Coal_constrains tab:

$$\sum_{b \in \{A,B\}} \;\sum_{t \in \text{month}(y,m)} \bigl[\text{coal\_slope}_{b,t} \times P_{\text{eff},b,t} + \text{coal\_fixed}_{b,t} \times \text{on}_{b,t}\bigr] \;\le\; \text{Limit}_{y,m} \times 1000$$

(The factor 1000 converts kilo-tonnes from the input into tonnes used by the model.)

This single inequality ties together **every hour of a month across both blocks**.  The optimiser cannot treat hours independently – choosing to run at full power during one high-price hour leaves less coal budget for later hours.

### 5.4  What happens when coal is scarce

When the coal limit is *not* binding (plenty of fuel), the plant can run whenever electricity revenue exceeds running cost.  But when the limit **is** binding, fascinating trade-offs emerge:

1. **The plant runs fewer hours** – it turns off during low-price hours to save coal for high-price hours.
2. **The plant may reduce load** – instead of running at Pmax, it drops to a lower output level to stretch coal further.
3. **Month-end pinch** – if the plant has used too much coal early in the month, it may be forced offline for the remaining days, missing profitable hours.
4. **DOW interaction** – even when a block runs at electrical Pmin, the DOW steam adds to the boiler's effective load and therefore to coal burn.  Under tight coal limits, the DOW "eats into" the remaining coal budget.

### 5.5  Warm-start coal awareness — how the heuristic builds a coal-feasible schedule

Before the MOSEK solver even begins searching, the model constructs an **initial operating schedule** — a suggested answer that the solver can use as a starting point.  This is called a "warm start."  Without it, the solver would start from scratch, blindly trying combinations of on/off switches for 6 600 hours × 2 blocks.  With a good warm start, MOSEK can begin improving from a reasonable baseline, cutting solve time from hours to minutes.

The warm-start heuristic goes through several phases.  The coal-related phase (Phase 1b) is described here in full detail.

#### Phase 1 — Start with "everything ON"

The heuristic begins with the most optimistic assumption: **every block is ON in every hour** (except when physically unavailable due to planned outages).  It checks the unavailability flag for each block and hour — if a block is marked unavailable, it is forced OFF.  It also respects the initial state: if config says Block A starts OFF, then hour 0 of Block A is set to OFF.

After this, the heuristic runs a cleanup pass to enforce **minimum down-time**: whenever a block transitions from ON to OFF (whether due to an outage or any other reason), the block must remain OFF for the next 6 consecutive hours.  This is a physical constraint — after shutting down a steam turbine, the rotor, seals, and bearings need time to cool and stabilise before a safe restart.  Restarting too quickly causes thermal stress and accelerated wear on critical components.  So if, for example, Block A is ON in hour 50 and is forced OFF in hour 51 by an outage, then hours 51–56 must all be OFF, even if the outage itself only lasts 1 hour.  Conversely, if an outage already lasts 10 hours or more, the minimum down-time of 6 hours is already satisfied — the heuristic does not need to extend anything.  This pass runs multiple times to handle cascading effects (one forced-off period may create a short ON-gap that is too short for minimum up-time, etc.).

The result of Phase 1 is a schedule where both blocks run as much as physically possible.  This schedule is almost certainly **not feasible** from a coal perspective — running flat-out 24/7 would consume far more coal than the monthly limits allow.

#### Phase 1b — Trim the schedule to fit within coal budgets

This is where coal awareness happens.  The heuristic goes through each constrained month one by one:

**Step 1 — Compute how much coal each (block, hour) slot would burn.**

For every block and every hour, the code calculates the coal consumption if that block runs at its **minimum possible output** ($P_{\min}$).  Why minimum, not maximum?  Because the warm start is **not the final answer** — it is only a starting point.  The heuristic only decides ON/OFF flags; MOSEK will later optimise both the ON/OFF pattern *and* the power levels simultaneously.

Using Pmin keeps **more hours ON**, which gives MOSEK **maximum flexibility**.  From that larger pool of ON-hours, MOSEK can:
- Crank power to Pmax in the highest-price hours (burning more coal there).
- Keep power at Pmin in medium-price hours (saving coal).
- Turn off some hours entirely if the coal math works out better.

You might think: "wouldn't it be better to estimate coal at Pmax and keep fewer hours — but all at full power?"  No, because using Pmax would **over-prune** the schedule.  The heuristic would turn off far more hours than necessary.  The problem: MOSEK cannot easily *add hours back* once the heuristic has removed them, because turning an hour ON triggers the 8-hour minimum up-time constraint — you cannot just re-enable one isolated hour; you must commit to 8 consecutive hours.  This creates rigid ON-blocks that the solver struggles to break apart.  Cutting too aggressively paints MOSEK into a corner.

In short: it is much easier for MOSEK to *remove* an hour the heuristic left ON (just set it to OFF) than to *add* an hour the heuristic turned OFF (must satisfy min-up, min-down, and cascading effects).  So the heuristic intentionally errs on the side of **leaving too many hours ON** and letting MOSEK do the fine-tuning.  The 85% safety margin (see Step 2 below) exists precisely to give MOSEK headroom to push power above Pmin in the best hours without blowing the coal budget.

For the primary DOW block (Block A), Pmin includes the DOW steam load: the boiler must produce $P_{\min} + \text{DOW}$ of thermal output.  This means the coal rate per hour for Block A is higher than for Block B in the same hour (Block B only picks up DOW when Block A is off).

This gives a coal consumption rate — in tonnes per hour — for each (block, hour) combination.

**Step 2 — Add up total coal for the month and compare to the limit.**

The code sums up the coal from all (block, hour) slots that are currently set to ON in this month.  It then compares this total to an **effective limit**, which is set at **85% of the actual monthly limit**.

Why only 85%?  Safety margin.  The warm start sets power to Pmin, but:
- The solver may later increase power output above Pmin (burning more coal).
- Minimum up-time constraints (8 hours) mean that turning a block ON in a profitable hour forces it to stay ON for 7 more hours, consuming additional coal.
- The 15% buffer leaves room for these adjustments without immediately violating the coal limit.

**Step 3 — If over budget: turn off the cheapest slots first.**

If the month's coal consumption exceeds the 85% effective limit, the heuristic must turn some (block, hour) slots OFF to save coal.  But which ones?

It gathers all ON slots in this month into a list and **sorts them by electricity price, from cheapest to most expensive**.  Then it walks through this sorted list and switches slots OFF one by one, starting with the lowest-price hours:

- Hour with price €5/MWh → turn off (this hour barely earns anything anyway)
- Hour with price €12/MWh → turn off
- Hour with price €18/MWh → turn off
- … keep going until total coal drops below the effective limit
- Hour with price €85/MWh → stop here, enough coal has been saved

Each time a slot is turned OFF, the code subtracts that slot's coal consumption from the running excess.  When the excess reaches zero, it stops.

**A concrete example from the model output:**

```
Heuristic coal cut 2026-04: turned off 474 slots (coal@Pmin 207671t > eff.limit 131750t)
```

This tells us:
- In April 2026, if both blocks ran wherever available at Pmin, they would burn **207 671 tonnes** of coal.
- The effective limit (85% of 155 000 kt) is **131 750 tonnes**.
- To get below 131 750 tonnes, the heuristic had to turn off **474 (block, hour) slots** — the 474 cheapest-price hours across both blocks.

After this trimming, April's warm-start schedule has both blocks ON during the highest-price hours and OFF during the lowest-price hours, with total coal consumption safely below the limit.

#### Phase 1c — Final cleanup: enforce min-up and min-down again

Turning off 474 slots may have created new violations:
- A block might now be ON for only 3 consecutive hours (violating the 8-hour minimum up-time).
- A block might be OFF for only 4 hours between two ON periods (violating the 6-hour minimum down-time).

The heuristic runs up to 10 cleanup passes:
- **Minimum down-time**: if a block goes from ON to OFF, the next 6 hours are forced OFF.
- **Minimum up-time**: if a block is ON for fewer than 8 consecutive hours, the entire short ON-period is turned OFF (it is not economical to start up for so few hours).

These passes repeat until no more changes are needed (the schedule stabilises).

#### Phase 2 — Set all derived variables consistently

Once the on/off schedule is finalised, the heuristic fills in all remaining variable values so the warm start is internally consistent:

- **Startup and shutdown flags**: if block goes from OFF to ON, startup = 1; from ON to OFF, shutdown = 1.
- **Start-up tier classification**: based on how long the block was off before starting (< 10 hours → hot, 10–59 hours → warm, ≥ 60 hours → very cold).
- **Power levels**: set to $P_{\min}$ (+ 5 MW boost if both blocks are on).
- **both_on and plant_off**: set based on the per-block on/off states.
- **Effective power and running costs**: computed from P, DOW, and cost curves to match the constraint definitions.

All of this is then injected into MOSEK's memory as its initial solution.  When MOSEK starts solving, it does not begin from zero — it begins from this carefully constructed schedule and tries to **improve** it (e.g. by increasing power in profitable hours, or finding a slightly different on/off pattern that earns more while staying within coal limits).

### 5.6  No annual limit

A common misconception: the model does **not** enforce a yearly cap.  Each month is independent.  Unused coal from March does not "roll over" to April.  This is a deliberate design choice reflecting the contractual structure of coal deliveries.

---

## Page 6 – More on Coal: Interaction with Other Constraints and Practical Effects

### 6.1  The tug-of-war: coal limit vs. minimum up-time

Suppose the price spikes for just 3 hours this afternoon.  Normally you would love to start up, run those 3 hours, and shut down.  But minimum up-time forces you to stay on for 8 hours.  Five of those hours may have low prices, and all 8 hours burn coal.  Under a tight coal limit, the solver might decide **not** to start at all – the coal cost of the unwanted 5 hours outweighs the profit from the 3 good hours.

### 6.2  Coal and DOW – a subtle interaction

The DOW steam load is contractually fixed each hour – the plant must deliver it whenever running.  Because the model includes DOW in $P_{\text{eff}}$, DOW directly increases monthly coal consumption even though it does not increase electrical output.  In months with tight coal limits, the model effectively sees every running hour as more "expensive" (in coal terms) than the electrical power alone would suggest.

### 6.3  Coal and two blocks

With two blocks, the model has an additional lever: it can choose to run **one** block instead of two.  Running one block burns roughly half the coal of running two (per hour), at the expense of lower electricity production.  Under tight coal limits, the solver often keeps only one block online during shoulder hours and fires up the second block only during the most profitable peaks.

### 6.4  Shadow prices on coal – "How much is an extra tonne worth?"

This brings us directly to the next major topic.

---

## Page 7 – Shadow Prices: Measuring the Value of Scarce Coal

### 7.1  What is a shadow price?

Suppose the monthly optimisation yields a total profit of €5 million.  Now suppose the coal limit were increased by **one tonne**.  Re-solving the model gives €5 000 020.  The increase of €20 is the **shadow price** of coal in that month: the marginal value of relaxing the constraint by one unit.

In formal terms:

$$\lambda_{y,m} = \frac{\partial \;\text{Optimal Profit}}{\partial \;\text{Coal Limit}_{y,m}}$$

A high shadow price means coal is extremely scarce – the plant is leaving a lot of money on the table by being forced to idle.  A shadow price of zero means the plant has more coal than it needs; the constraint is not binding.

### 7.2  How the model computes shadow prices — step by step

Solving a MILP (with integer on/off decisions) does **not** directly give shadow prices.  To understand why, and how the model works around it, we first need to understand what a "dual" is.

#### What is a dual value (shadow price) in optimisation?

Think of an optimisation problem as a machine: you feed in constraints (rules), and it outputs the best possible profit.  A **dual value** (also called **shadow price**) answers a simple "what if" question for each constraint:

> *"If I loosened this one rule by one tiny unit, how much more profit could I make?"*

For example, if the monthly coal limit is 90 000 tonnes and the dual value on that constraint is 20 EUR/t, it means: *relaxing the limit to 90 001 tonnes would increase profit by approximately €20.*

Dual values are a **natural byproduct** that LP solvers compute alongside the optimal solution.  They come from the mathematical theory of linear programming (the "dual problem"), and every LP solver — including MOSEK — can report them automatically.

**Important**: every constraint in the model has its own dual variable — not just the coal limits.  After the LP re-solve, the solver returns a dual value for *every* constraint: power upper/lower bounds, minimum up-time, minimum down-time, availability, start-up logic, and so on.  Each dual answers the same "what if" question for its respective constraint.  For example:

- The dual on a **power upper bound** tells you how much additional profit one extra MW of capacity would yield in that specific hour — useful for evaluating capacity upgrades.
- The dual on a **minimum up-time** constraint quantifies the cost of the 8-hour commitment: how much profit the plant sacrifices by being forced to stay on in the remaining low-price hours after a startup.
- The dual on an **availability** constraint reveals the opportunity cost of a planned outage in a particular hour.

The model currently extracts only the coal-limit duals because they have the most direct commercial application (spot coal procurement).  However, the infrastructure is general — reading any other dual requires only one additional line: `m.dual[<constraint>]`.

**The catch**: dual values are only well-defined for **Linear Programmes (LP)**, where all variables are continuous (can take any fractional value like 127.3 MW).  In our model, many variables are **binary** (0 or 1 only) — the on/off switches.  A Mixed-Integer Linear Programme (MILP) has a jagged, staircase-like solution landscape where the concept of "loosen by a tiny bit" breaks down.  You cannot turn a block "half on."

So the model uses a standard workaround:

#### Step 1 — Solve the original MILP

MOSEK finds the optimal on/off schedule for all ~6 600 hours × 2 blocks.  After this, we know the exact value of every binary variable: which hours each block is ON, every startup event, every hot/warm/vcold classification.

#### Step 2 — Fix all integer variables and relax their domains

This is the key trick that converts the problem with on/off switches into a smooth continuous problem.  The code does two things to every on/off (binary) variable:

**A) Lock each on/off decision to the answer the solver already found.**

After Step 1, the solver has decided for every hour and every block whether it should be ON (= 1) or OFF (= 0).  Sometimes, due to numerical precision, a value that should be exactly 1 comes back as 0.99999999987 — so the code rounds it to the nearest whole number (0 or 1) first.

Now the code goes through **all ~53 000** on/off variables one by one and locks ("fixes") each one.  For example:

- Block A, hour 42: the solver said ON → lock it to 1.  No matter what happens next, this variable stays at 1.
- Block B, hour 100: the solver said this was not a startup → lock it to 0.
- Block A, hour 200: the solver said this was a hot start → lock it to 1.
- … and so on for every `on`, `startup`, `shutdown`, `hot_start`, and `vcold_start` variable.

After locking, these are no longer "decisions" — they are frozen constants.  The solver cannot change them.

**B) Tell the solver these variables are no longer on/off switches.**

Each binary variable has a label that says "I can only be 0 or 1."  This label forces the solver to use special, slower algorithms designed for integer problems.  Since all the values are already locked anyway, the code peels off this label and replaces it with "I can be any non-negative number."  The actual value doesn't change (it's locked), but the solver now classifies the entire problem as a smooth, continuous Linear Programme (LP) and uses its fast LP algorithm.

**Why are both A and B needed?**
- If we only did A (lock values) but kept the "0 or 1" labels, the solver would still think it's an integer problem and would refuse to compute dual values (shadow prices).
- If we only did B (remove the labels) but didn't lock the values, the solver could assign nonsensical fractional values like 0.37 to an on/off switch — a block cannot be "37% on."

Together, A and B achieve: the on/off schedule from Step 1 is preserved exactly, AND the solver treats the problem as a pure LP so it can compute shadow prices.

**What is still free to change after this step?** The power output $P_{b,t}$ for each block and hour — this was always a continuous variable (e.g. 150.7 MW), never an on/off switch.  So it remains free to move between $P_{\min}$ and $P_{\max}$.  Similarly, derived quantities like effective power and running costs can adjust.  The remaining question for the LP is: *"Given this exact on/off schedule, what is the best power level in each hour, and how much would the profit improve if each coal limit were raised by one tonne?"*

#### Step 3 — Attach a dual suffix and re-solve

The code attaches a `Suffix` object called `dual` with direction `IMPORT`.  This is Pyomo's way of telling the solver: *"after you solve, please send me the dual value for every constraint."*

MOSEK then solves the LP.  Because all integers are fixed, this is fast — typically a few seconds compared to minutes for the original MILP.

#### Step 4 — Read the duals on the coal constraints

For each month $(y, m)$ that has a coal limit constraint:

$$\lambda_{y,m} = \texttt{m.dual[m.coal\_monthly\_limit[ym]]}$$

MOSEK returns a **positive dual** for a binding $\le$ constraint in a maximisation problem.  This dual value is directly the shadow price in **EUR per tonne of coal**: the marginal increase in optimal profit if the coal limit were raised by one tonne.

If the constraint is **not binding** (the plant used less coal than the limit), the dual is zero — extra coal would not help because the plant already has more than it needs.

#### Step 5 — Restore everything

All domains are restored to their original integer/binary type.  Variables that were unfixed before are unfixed again.  The `dual` suffix is deleted from the model.  The model is returned to its original state, ready for the merchant re-solve.

```
  MILP solve                 ──►   Fix all 53,000 integer vars at MIP values
  (finds on/off schedule)          + relax domains Binary → NonNegativeReals
                                                             │
                                                    Now it's a pure LP
                                                             │
                                                    Attach dual suffix
                                                    (= "solver, report duals")
                                                             │
                                                    MOSEK solves LP (fast)
                                                             │
                                                    Read dual on each
                                                    coal monthly constraint
                                                             │
                                                    Shadow price (EUR/t)
```

The shadow price tells management: *"If you could procure one additional tonne of coal for month X at less than €Y, it would be profitable to do so."*

#### What the LP can actually adjust

With the on/off schedule frozen, the LP still has freedom to **slide power levels** between $P_{\min}$ and $P_{\max}$ within every running hour.  That is the mechanism through which an extra tonne of coal creates value: the LP can increase output in the most profitable hours (where Price is highest) and decrease it in less profitable hours.  The dual value quantifies exactly how much this flexibility is worth per additional tonne of coal.

### 7.3  Full shadow prices vs. merchant-only shadow prices

The model computes **two** separate sets of shadow prices.  Understanding the difference requires looking at **what the DOW steam obligation does to the objective and to coal consumption**.

#### Full shadow prices (with DOW)

These are computed first.  The LP includes all real-world parameters:

- **DOW thermal load** ($\text{DOW}_t$, in MW-thermal): whenever a block is on, it burns coal to produce both electricity *and* steam.  The effective boiler load is $P_{\text{eff}} = P + \text{DOW} \times \text{on}$, and coal consumption depends on $P_{\text{eff}}$, not just $P$.
- **DOW revenue** ($\text{DOW\_rev}_t$): income from steam delivery, credited whenever at least one block is on.
- **Grid fee** ($\text{gridfee}_t$): when the entire plant is offline, it draws auxiliary power from the grid at a cost of $(\text{Price} + \text{gridfee}) \times \text{off\_consumption}$.  The grid fee makes shutting down more expensive and therefore influences how tight the coal constraint feels.
- **DOW off-consumption** (130 MW): when both blocks are off, the DOW consumer switches to electric heating, drawing 130 MW from the grid.  The plant pays for this at $(\text{Price} + \text{gridfee})$ and receives only a partial compensation of 6.9 EUR/MWh.  This large penalty makes it costly to go fully offline.

The full shadow price thus reflects the **complete economic reality**: DOW revenue makes running more attractive (pulling shadow prices up), DOW coal consumption makes running more coal-hungry (also pulling shadow prices up), and the OFF-cost penalty (including grid fee and DOW backup) makes idling more painful (reducing the plant's willingness to save coal by shutting down, again pushing shadow prices up).

#### Merchant-only shadow prices (without DOW)

These answer a hypothetical question: *"What if we were a pure merchant power plant with no DOW obligation?"*

To compute them, the model:

1. **Saves** all current continuous variable values (so they can be restored later).
2. **Sets DOW = 0** for every hour — the steam load disappears.  Now $P_{\text{eff}} = P$ (electrical output only).  Each running hour burns **less coal** because the boiler no longer needs to produce steam.
3. **Sets DOW_rev = 0** for every hour — no steam income.
4. **Re-fixes all integers** at their MIP solution values (same procedure as before).
5. **Re-solves the LP** and reads the duals on the coal constraints.
6. **Restores** DOW, DOW_rev, integer domains, and all continuous variable values to their original state.

Note that the **on/off schedule stays the same** between the two solves — only the DOW parameters change.  This isolates the effect of DOW on the coal shadow price.

#### Why the two differ

| Factor | Full (with DOW) | Merchant (no DOW) |
|--------|-----------------|-------------------|
| Coal burned per running hour | Higher ($P_{\text{eff}}$ includes DOW) | Lower ($P_{\text{eff}} = P$ only) |
| Revenue per running hour | Electricity + DOW revenue | Electricity only |
| OFF penalty (grid fee + DOW backup) | High (130 MW DOW backup at Price + gridfee) | Lower (only 10 MW own consumption) |
| Typical shadow price | **Higher** | **Lower** |

The full shadow price is almost always **higher** than the merchant shadow price.  This is because DOW simultaneously:
- Increases coal consumption per hour (making the coal budget tighter).
- Increases the penalty for going offline (making it harder to "save coal" by shutting down).

The **difference** between the two shadow prices quantifies the **coal cost of the DOW obligation**.  For example, if the full shadow price is 63 EUR/t and the merchant shadow is 52 EUR/t, the DOW contract is responsible for 11 EUR/t of the coal pressure — information that is critical for DOW contract renegotiation and for understanding the true cost of heat delivery.

#### The role of grid fee in shadow prices

The grid fee ($\text{gridfee}_t$) often gets overlooked, but it plays a real role in shaping shadow prices:

- When the plant is offline, it pays $(\text{Price}_t + \text{gridfee}_t) \times \text{off\_consumption}$ for auxiliary and DOW backup power.
- A higher grid fee makes shutting down more expensive, which means the plant prefers to stay on even when electricity margins are thin.
- Staying on burns coal, which tightens the monthly coal constraint.
- This drives shadow prices **up**.

In the LP re-solve, the grid fee directly enters the OFF_costs expression. The LP can trade off between "stay on and burn coal but avoid grid fees" vs. "shut down to save coal but pay grid fees."  The shadow price reflects this trade-off at the margin.

### 7.4  Implementation details — the full procedure

The LP re-solve procedure in `extract_coal_shadow_prices` is meticulous about leaving the model in exactly its original state:

**Phase 1 — Full shadow prices:**

1. **Loop** over every variable in the Pyomo model (`m.component_objects(Var)`).
2. For each variable with an integer or binary domain:
   - Record the original domain (e.g. `Binary`).
   - If the variable is **not already fixed** (by initial conditions), fix it at its rounded MIP value and record that it was newly fixed.
   - If the variable is **already fixed** (e.g. `on[A,0]` was fixed by initial conditions), skip fixing but still record it.
   - Set the domain to `NonNegativeReals` in all cases.
3. Attach `m.dual = Suffix(direction=Suffix.IMPORT)`.
4. Create a fresh MOSEK solver and solve the LP (`tee=False` — no solver output printed).
5. For each constrained month `ym`:
   - Read the dual: `m.dual.get(m.coal_monthly_limit[ym], 0.0)`.
   - Store in `shadow_prices[ym]`.
6. **Restore**: for each variable, set domain back to original.  Unfix variables that were newly fixed.  Delete `m.dual`.
7. Print results: `Coal price add-on YYYY-MM: +XX.XX EUR/t (binding)` or `(not binding)`.

**Phase 2 — Merchant-only shadow prices** (only when `USE_DOW_OPPORTUNITY_COSTS` is enabled):

1. **Save** the value of every continuous variable (so the merchant LP solve does not corrupt them).
2. **Save** `DOW_rev[t]` and `DOW[t]` for every hour.
3. **Zero out** `DOW_rev[t] = 0` and `DOW[t] = 0` for all `t`.  This removes steam revenue from the objective and steam load from $P_{\text{eff}}$, so coal consumption drops.
4. **Repeat** the integer-fix-and-relax procedure (same as Phase 1).
5. Attach `m.dual`, solve LP, read duals into `merchant_shadow[ym]`.
6. **Restore** integer domains and fixes.  Delete `m.dual`.
7. **Restore** `DOW_rev[t]` and `DOW[t]` to their saved values.
8. **Restore** all continuous variable values to their pre-merchant-LP values.
9. Print results: `Merchant shadow YYYY-MM: +XX.XX EUR/t (binding)` or `(not binding)`.

Both phases return their results as dictionaries. The function returns a tuple `(shadow_prices, merchant_shadow)` which is then written into the Excel report.

### 7.5  A worked example (hypothetical)

| Month | Coal limit (kt) | Coal used (kt) | Shadow price (EUR/t) | Merchant shadow (EUR/t) |
|-------|-----------------|----------------|---------------------|------------------------|
| Apr   | 90              | 90.0 (binding) | 18.50               | 12.30                  |
| May   | 95              | 82.4 (slack)   | 0.00                | 0.00                   |
| Jun   | 85              | 85.0 (binding) | 24.70               | 16.10                  |

- **April**: The plant burned every last tonne.  An extra tonne would be worth €18.50 in profit.  Without DOW, only €12.30 – DOW accounts for about one-third of the coal pressure.
- **May**: Plenty of coal.  The limit is not binding, so the shadow price is zero.
- **June**: Even tighter.  Each tonne is worth €24.70.

### 7.6  Why shadow prices are valuable for decision-making

Shadow prices feed into several practical decisions:

- **Coal procurement**: If additional spot coal is available at €15/t and the shadow price is €18.50/t, buying more coal is profitable.
- **DOW contract renegotiation**: Comparing full vs. merchant shadow prices quantifies the coal cost of the DOW obligation.
- **Maintenance scheduling**: Scheduling a planned outage in a month with a high shadow price is wasteful (the plant would have been offline anyway due to coal scarcity).  Better to schedule outages in low-shadow-price months.
- **Hedging and trading**: Shadow prices help set the "effective cost" of generation, informing forward sales.

---

## Page 8 – Bringing It All Together: From Data to Decision

### 8.1  The full lifecycle, step by step

1. **Data preparation**: Read the Excel input file.  Merge Block A and Block B hourly data.  Compute linearised cost curves and coal curves.  Parse start-up tiers and coal limits.

2. **Warm start**: Before calling the solver, the model constructs an initial feasible schedule:
   - Default everything to ON.
   - Respect unavailability.
   - Clean up min-up / min-down violations iteratively.
   - If coal limits exist, trim the cheapest-price hours so each month stays below 85% of the coal cap.
   - Set power levels to Pmin (+ boost if both on).
   - Feed this as a starting point to MOSEK so it can converge faster.

3. **MIP solve**: MOSEK searches for the profit-maximising schedule.  It uses branch-and-bound (an algorithm that systematically tries combinations of on/off decisions).  The solver is given a 10-minute time limit and a 5% optimality gap tolerance – it stops when it finds a solution within 5% of the theoretical best.

4. **Shadow-price LP**: After the MIP, fix all on/off decisions and re-solve as a pure LP to extract dual values on the coal constraints (twice: once with DOW, once without).

5. **Result extraction**: Read all variable values back into the DataFrame.  Compute detailed PnL columns: electricity revenue, running cost, start-up cost, OFF costs, DOW revenue.

6. **Audit**: Verify that the sum of hourly PnL equals the objective value from the solver (catches numerical bugs).

7. **Reporting**: Write everything to a multi-sheet Excel workbook:
   - **Results** sheet – hourly detail with all PnL components, coal consumption, CO₂ emissions, and merchant/DOW splits.
   - **Monthly** sheet – aggregated plant-level totals.
   - **Monthly_A / Monthly_B** – per-block monthly summaries.

### 8.2  Key configuration dials

| Parameter | Default | Effect |
|-----------|---------|--------|
| `USE_COAL_CONSTRAINS` | True | Enable/disable monthly coal caps |
| `MOSEK_MIO_TOL_REL_GAP` | 5% | Acceptable gap between best solution and theoretical optimum |
| `MOSEK_MIO_MAX_TIME` | 600 s | Maximum solver time |
| `MIN_UP` | 8 h | Minimum hours a block must stay on after starting |
| `MIN_DOWN` | 6 h | Minimum hours a block must stay off after stopping |
| `OWN_CONSUMPTION` | 10 MW | Auxiliary power drawn when offline |
| `DOW_OFF_CONSUMPTION` | 130 MW | DOW consumer's electric backup load when plant is offline |
| `DUAL_BLOCK_BOOST` | 5 MW | Extra capacity when both blocks run simultaneously |
| `START_MARGIN_MIN` | 0 EUR | Additional margin added to every start-up cost |

### 8.3  Summary in plain English

The Schkopau model is a **planning engine**.  It takes thousands of hours of electricity prices, fuel costs, emission costs, and plant capabilities, and determines the most profitable operating schedule while obeying physical and contractual limits.  The most impactful of these limits is the **monthly coal cap**, which forces the plant to be selective about when it runs.  The **shadow prices** computed after the main solve quantify exactly how valuable extra coal would be in each month, enabling smarter procurement, scheduling, and commercial decisions.
