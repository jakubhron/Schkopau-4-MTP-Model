# Schkopau MTP – Model Documentation

*Technical documentation for the Schkopau power-plant dispatch optimisation model.*

---

## 1. Overview

### 1.1  Plant description

Schkopau is a **lignite-fired combined-heat-and-power (CHP) plant** located in Germany.  It has **two generating blocks** (called **Block A** and **Block B**).  Each block can burn coal to produce electricity, and at the same time the plant delivers **steam (heat)** – labelled **DOW** (Dampf- und Wärmelieferung) – to a neighbouring industrial consumer.  The plant sells its electricity on the wholesale power market, earns revenue from the DOW steam delivery, and pays for coal, CO₂ emission allowances, start-up wear, grid fees, and its own auxiliary ("house") power consumption.

### 1.2  Purpose

The model determines an optimal operating schedule for the planning horizon (typically April to December, approximately 6 400 hours):

> **For every hour, should each block be ON or OFF, and if ON, at what power level?**

The objective is to maximise total **profit** (revenue minus costs) subject to physical and contractual constraints: minimum run/down times, start-up ramp limits, monthly coal-burn caps, and others.

### 1.3  Why mathematical optimisation is required

A naive heuristic — "run whenever the price is positive" — fails because of interlocking constraints.  Starting a block incurs a lump-sum cost and requires at least 8 consecutive hours of operation (minimum up-time).  Shutting down requires at least 6 consecutive hours offline (minimum down-time).  Monthly coal budgets further restrict how many hours the plant can run.  These interdependencies make the problem a **Mixed-Integer Linear Programme (MILP)**, solved by the commercial optimiser **MOSEK**.

### 1.4  High-level workflow

```
  Input Excel  ──►  Load & Prepare  ──►  Build Pyomo Model  ──►  Solve (MOSEK)
       │                                                              │
       │         ◄── Extract Results ◄── Check Termination  ◄────────┘
       │                │
       └── Write Excel Report (with shadow-price analysis)
```

1. **Load** hourly price curves, block parameters, start-up tiers, and coal limits from an Excel workbook.
2. **Build** a mathematical optimisation model in *Pyomo* (a Python modelling library).
3. **Warm-start** — construct an initial feasible schedule and inject it into MOSEK.
4. **Solve** the model using the MOSEK solver (with CONSTRUCT_SOL to validate the warm start).
5. **Extract** the optimal hourly schedule, compute profit components, run an audit, and **write** a result Excel.

---

## 2. Input Data

The model reads a single Excel workbook with four tabs (sheets).

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
| **coal conversion factor at pmin** | Coal burned (tonnes/h) when running at Pmin | 65 |
| **coal conversion factor at pmax** | Coal burned (tonnes/h) when running at Pmax | 120 |
| **unavailibility** | 1 = block is in planned outage this hour, 0 = available | 0 |

#### Cost curve linearisation

Running costs are linearised between the two known operating points (Pmin and Pmax):

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

The same linearisation is applied to coal consumption (from coal conversion factor at pmin to coal conversion factor at pmax).

### 2.2  Starts tab (start-up tiers)

Start-up costs and ramp duration depend on how long the block has been offline.  The Starts tab defines **five start-up tiers** for each block:

| Tier | Hours offline | Typical cost (EUR) | Ramp behaviour |
|------|--------------|-------------------|----------------|
| **Very hot** | 0 – 5 h | Low | Fast ramp-up |
| **Hot** | 5 – 10 h | Medium-low | Moderate ramp |
| **Warm** | 10 – 60 h | Medium | Slower ramp |
| **Cold** | 60 – 100 h | High | Slow ramp |
| **Very cold** | 100+ h | Highest | Slowest ramp |

Each tier provides a **ramp profile** (power output for hours 0 through 3 after starting) and a **lump-sum start-up cost** in EUR.

**Important**: although all five tiers are read from the input, the optimiser only uses **three** of them: **hot**, **warm**, and **very cold**.  The "very hot" and "cold" tiers are parsed for reference but never appear in the model's constraints or objective.  The warm tier serves as the baseline start-up cost, with the hot and very-cold tiers applying cost adjustments relative to it.

### 2.3  Coal_constrains tab (monthly coal limits)

Specifies the maximum coal consumption (in kilo-tonnes) across both blocks combined for each calendar month:

| Year | Month | Limit (kt) |
|------|-------|-----------|
| 2026 | 4     | 90        |
| 2026 | 5     | 95        |
| …    | …     | …         |

These limits are the binding scarcity constraint in most scenarios (see Sections 5–6).

---

## 3. Model Formulation: Variables, Objective, and Constraints

### 3.1  Decision variables

For **every hour $t$** and **every block $b$**, the model determines:

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

Binary variables are restricted to {0, 1} (on/off decisions).  The power variable $P$ is continuous, taking any value between $P_{\min}$ and $P_{\max}$.

### 3.2  Effective power and DOW

The industrial neighbour draws steam (DOW) from whichever block is running.  When both blocks run, one block "owns" the full DOW load; only if that primary block (Block A) shuts down does the other block pick up the steam load.

The model supports **two DOW modes**, controlled by the `USE_DOW_OPPORTUNITY_COSTS` flag:

**When `USE_DOW_OPPORTUNITY_COSTS = True` (DOW ON):**

$$P_{\text{eff},A,t} = P_{A,t} + \text{DOW}_t \times \text{on}_{A,t}$$

$$P_{\text{eff},B,t} = P_{B,t} + \text{DOW}_t \times (\text{on}_{B,t} - \text{both\_on}_t)$$

$P_{\text{eff}}$ is the *total thermal-equivalent load on the boiler* (electrical output plus steam).  It is this effective power that determines fuel consumption and running costs.  DOW revenues are computed as $\text{DOW\_OPPORTUNITY\_REVENUE} \times \text{DOW}$ (188 EUR/MW × DOW load in MW) and credited whenever at least one block is running.

**When `USE_DOW_OPPORTUNITY_COSTS = False` (DOW OFF):**

$$P_{\text{eff},b,t} = P_{b,t}$$

In this mode, $P_{\text{eff}}$ equals electrical output only — no DOW steam load is added to the effective boiler load.  DOW revenues are set to zero.  However, DOW is **still deducted from the power upper bound** (Pmax) in both modes, because the physical capacity reserved for steam extraction is a hard constraint regardless of the accounting treatment.  This means:

- The block's maximum *electrical* output is always $P_{\max} - \text{DOW}$ (the steam extraction physically steals capacity).
- But in DOW OFF mode, the coal consumption and running costs are computed on electrical output alone, as if the steam load were free.

This two-mode design lets the model compare "full DOW reality" scenarios vs "pure merchant" what-if analyses.

### 3.3  Objective function

The model **maximises total profit** over all hours:

$$\max \sum_{t} \Bigl[\underbrace{P_{b,t} \times \text{Price}_t}_{\text{electricity revenue}} - \underbrace{\text{run\_costs}_{b,t}}_{\text{fuel + CO₂ + variable}} - \underbrace{\text{start\_cost}_{b,t}}_{\text{wear \& tear}} - \underbrace{\text{OFF\_costs}_t}_{\text{house power if off}} + \underbrace{\text{DOW\_rev}_t}_{\text{steam income}}\Bigr]$$

The components are:

1. **Electricity revenue** — power output multiplied by the market price.  During negative-price hours, generation incurs a cost rather than earning revenue.

2. **Running costs** — the linearised fuel + emissions cost: $\text{cost\_slope} \times P_{\text{eff}} + \text{cost\_fixed} \times \text{on}$.  This includes coal, CO₂ allowances, variable O&M, and other operating charges embedded in TC_PminN and TC_Pmax.

3. **Start-up costs** — a lump sum charged when `startup = 1`.  The amount depends on the tier:
   - Warm cost is the baseline: $(\text{warm\_cost} + \text{START\_MARGIN\_MIN}) \times \text{startup}$
   - Hot start adjusts: $+(\text{hot\_cost} - \text{warm\_cost}) \times \text{hot\_start}$
   - Very-cold start adjusts: $+(\text{vcold\_cost} - \text{warm\_cost}) \times \text{vcold\_start}$

4. **OFF costs** — when the entire plant is offline, it still draws auxiliary power from the grid (own consumption of 10 MW, plus 130 MW for the DOW consumer's electric backup when DOW opportunity-cost mode is active).  The cost is $(\text{Price} + \text{Grid fee}) \times \text{off\_consumption}$.

5. **DOW revenue** — steam delivery income credited whenever at least one block is on.

### 3.4  DOW opportunity costs

When the plant is fully offline, the industrial DOW consumer switches to its own electric heating, drawing approximately 130 MW from the grid.  The model can be configured to account for this via the `USE_DOW_OPPORTUNITY_COSTS` flag:
- When enabled: a partial compensation of 6.9 EUR/MWh is deducted (the plant reimburses the neighbour), and the full 130 MW of grid draw is charged at market price + grid fee.  DOW revenues (188 EUR/MW × DOW) are credited when at least one block runs.
- When disabled: OFF costs still include the plant's own consumption (10 MW), but the 130 MW DOW backup and DOW revenues are excluded.

This makes shutting down more expensive than it first appears when DOW accounting is active, accurately reflecting the contractual reality.

---

## 4. Constraints

The following constraint families enforce physical and contractual limits:

### 4.1  Availability

$$\text{on}_{b,t} \le 1 - \text{unavailibility}_{b,t}$$

If a block is in a planned outage ($\text{unavailibility} = 1$), it cannot be on.

### 4.2  Start-up / shut-down logic

The on/off status must change consistently:

$$\text{startup}_{b,t} - \text{shutdown}_{b,t} = \text{on}_{b,t} - \text{on}_{b,t-1}$$

A transition from OFF to ON sets $\text{startup} = 1$; from ON to OFF sets $\text{shutdown} = 1$.  Additional linking constraints ensure:
- A start-up can only occur if the block was off in the previous hour.
- A start-up implies the block is on in the current hour.

### 4.3  Minimum up-time (8 hours)

$$\sum_{k=0}^{7} \text{on}_{b,t+k} \;\ge\; 8 \times \text{startup}_{b,t}$$

Once started, a block must remain online for **at least 8 consecutive hours**, reflecting the thermal-stress limits of boiler components.

### 4.4  Minimum down-time (6 hours)

$$\sum_{k=0}^{5} (1 - \text{on}_{b,t+k}) \;\ge\; 6 \times (\text{on}_{b,t-1} - \text{on}_{b,t})$$

After shutdown, a block must remain offline for **at least 6 consecutive hours** to allow safe turbine cooling.

### 4.5  Power bounds

When a block is ON, its output must stay between Pmin and Pmax:

$$P_{b,t} \ge P_{\min,b,t} \times \text{on}_{b,t} + \text{boost} \times \text{both\_on}_t$$

$$P_{b,t} \le P_{\max,b,t} \times \text{on}_{b,t} + \text{boost} \times \text{both\_on}_t$$

The **boost** (5 MW by default, configured as `DUAL_BLOCK_BOOST`) accounts for the slightly higher capability when both blocks share auxiliary systems.  The upper bound is adjusted to subtract the DOW steam load so that the block does not exceed its electrical Pmax after accounting for steam extraction.  Note that DOW is **always** deducted from Pmax — regardless of the `USE_DOW_OPPORTUNITY_COSTS` flag — because the physical capacity reserved for steam is a hard limit.

### 4.6  Startup / shutdown power pinning (with tightened Big-M)

In the start-up hour, output must equal exactly Pmin (plus boost if both blocks are on):

$$P_{b,t} = P_{\min,b,t} \times \text{on}_{b,t} + \text{boost} \times \text{both\_on}_t \qquad \text{(when startup}_{b,t} = 1\text{)}$$

Symmetrically, in the hour before shutdown, output must return to Pmin.  Both rules are implemented as Big-M constraints that activate only when the respective startup or shutdown indicator equals 1.

#### Big-M tightening

Big-M constraints use a large constant $M$ to "turn off" parts of a constraint.  A loose $M$ (e.g. 500) works mathematically but weakens the LP relaxation — the solver sees a huge feasible region that does not reflect reality, making it harder to find good bounds and prune the branch-and-bound tree.

The model computes **per-(block, hour) tight Big-M values** instead of using a single global constant:

- **Upper-bound M**: $M_{ub}(b,t) = P_{\max}(b,t) - P_{\min}(b,t) + 1$
- **Lower-bound M**: $M_{lb}(b,t) = P_{\min}(b,t) + \text{boost} + 1$

For a typical block with $P_{\max} = 290$ and $P_{\min} = 161$, this gives $M_{ub} = 130$ and $M_{lb} = 167$ — far smaller than the old global value of 500.  The tighter bounds help MOSEK's LP relaxation cut more aggressively, resulting in faster solve times and smaller optimality gaps.

### 4.7  Start-up tier detection

The start-up tier is determined by how long the block has been offline:

- **Hot start**: the block was ON somewhere in the previous 10 hours ($\text{on}_{b, t-10} = 1$).
- **Very cold start**: the block was OFF at every 8-hour checkpoint over the past 56 hours (checks at offsets 8, 16, 24, 32, 40, 48, 56).
- **Warm start**: everything in between (the "default" tier).

Binary variables `hot_start` and `vcold_start` are linked to `startup` so that if a start occurs, exactly one tier applies.

### 4.8  Both-on and plant-off linearisation

`both_on` and `plant_off` are auxiliary coupling variables, forced by standard linearisation constraints:

$$\text{both\_on}_t \le \text{on}_{A,t}, \quad \text{both\_on}_t \le \text{on}_{B,t}, \quad \text{both\_on}_t \ge \text{on}_{A,t} + \text{on}_{B,t} - 1$$

This ensures `both_on = 1` if and only if **both** blocks are simultaneously on.  Analogous constraints define `plant_off = 1` only when **both** blocks are off.

### 4.9  Optional: start-up ramp profile

The model has two ramp modes, controlled by the `USE_SIMPLE_STARTUP_RAMP` flag:

- **Simple mode (default, `True`)**: No detailed ramp constraints.  The `in_ramp` variable is fixed to 0 for all hours, so the standard Pmin lower bound applies from the first hour of startup.  This is the current production setting.

- **Detailed mode (`False`)**: When enabled, the model limits the power output during the first few hours after a start to follow the physical ramp profile read from the Starts tab.  Each tier has its own ramp: a hot start ramps faster than a very-cold start.  The `in_ramp` variable tracks whether a block is still in its ramp-up window, and Big-M constraints relax the Pmin lower bound during ramp hours to allow the lower ramp-profile power levels.

---

## 5. Coal Limits: The Binding Scarcity Constraint

### 5.1  Why coal limits matter

Consider a plant rated at 420 MW of combined electrical capacity, yet allocated only 90 000 tonnes of coal for a given month.  At full load, both blocks together consume approximately 240 tonnes per hour — enough to exhaust the entire monthly budget in under 16 days.  The plant therefore cannot simply dispatch at maximum output whenever the electricity price is positive.

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

When the coal limit is not binding, the plant dispatches whenever electricity revenue exceeds running cost.  When the limit **is** binding, several trade-offs arise:

1. **The plant runs fewer hours** – it turns off during low-price hours to save coal for high-price hours.
2. **The plant may reduce load** – instead of running at Pmax, it drops to a lower output level to stretch coal further.
3. **Month-end pinch** – if the plant has used too much coal early in the month, it may be forced offline for the remaining days, missing profitable hours.
4. **DOW interaction** – even when a block runs at electrical Pmin, the DOW steam adds to the boiler's effective load and therefore to coal burn.  Under tight coal limits, the DOW "eats into" the remaining coal budget.

### 5.5  Warm-start heuristic: constructing a coal-feasible initial schedule

Before launching the MILP solver, the model constructs an **initial feasible schedule** (warm start) that provides MOSEK with a high-quality incumbent solution.  A good warm start reduces solve time from hours to minutes by enabling the branch-and-bound algorithm to start from a near-optimal baseline.

The heuristic proceeds through several phases, with coal awareness being the most critical.

#### Phase 1 — Start with "everything ON"

The heuristic begins with the most optimistic assumption: **every block is ON in every hour** (except when physically unavailable due to planned outages).  It checks the unavailability flag for each block and hour — if a block is marked unavailable, it is forced OFF.  It also respects the initial state: if config says Block A starts OFF (via `INITIAL_ON = {"A": 0, "B": 1}`), then hour 0 of Block A is set to OFF.

After this, the heuristic runs 3 cleanup passes to enforce **minimum down-time**: whenever a block transitions from ON to OFF (whether due to an outage or other reason), the subsequent 6 hours are forced OFF.  This reflects the physical requirement for turbine cooling.  The passes repeat to resolve cascading effects — e.g. a forced-off period may create an ON-gap shorter than the minimum up-time.

The result of Phase 1 is a schedule where both blocks run as much as physically possible.  This schedule is almost certainly **not coal-feasible** — sustained maximum-availability operation would consume far more coal than the monthly limits allow.

#### Phase 1b — Trim the schedule to fit within coal budgets

This is where coal awareness happens.  The heuristic goes through each constrained month one by one:

**Step 1 — Compute how much coal each (block, hour) slot would burn.**

For every block and every hour, the code calculates the coal consumption if that block runs at its **minimum possible output**.  This minimum accounts for two factors that raise the floor above raw Pmin:

- **DUAL_BLOCK_BOOST**: When both blocks are ON simultaneously, the model's `p_lower` constraint forces $P \ge P_{\min} + 5\text{ MW}$.  The heuristic checks whether both blocks are ON in each hour and adds the 5 MW boost accordingly.  Failing to account for this would underestimate coal consumption by approximately 2 000–3 000 tonnes per month, potentially producing a warm-start schedule that violates the coal limit when the solver enforces the boost constraint.

- **DOW steam load**: For the primary DOW block (Block A), when `USE_DOW_OPPORTUNITY_COSTS = True`, Pmin includes the DOW steam load: the boiler must produce $P_{\min} + \text{boost} + \text{DOW}$ of thermal output.  This means the coal rate per hour for Block A is higher than for Block B in the same hour (Block B only picks up DOW when Block A is off).

In code:
```
both = 1 if all(on_hint[bb][t] == 1 for bb in blocks) else 0
boost = DUAL_BLOCK_BOOST × both
p_eff_min = Pmin + boost + DOW
coal_rate_min = coal_slope × p_eff_min + coal_fixed
```

This gives a coal consumption rate — in tonnes per hour — for each (block, hour) combination.

**Step 2 — Add up total coal for the month and compare to the limit.**

The code sums up the coal from all (block, hour) slots that are currently set to ON in this month.  It then compares this total to the **full monthly limit** (100% of the limit from the Coal_constrains tab).

Why the full limit and not a safety margin?  Because the heuristic computes coal at minimum output — this is already a conservative estimate.  The subsequent Phase 2 P-scaling step (described below) distributes any remaining coal headroom by raising power levels above Pmin proportionally.  Adding a safety margin on top of the Pmin estimate would over-prune the schedule, turning off hours that the solver could have used productively.

**Step 3 — If over budget: turn off the cheapest slots first.**

If the month's coal consumption exceeds the limit, the heuristic must turn some (block, hour) slots OFF to save coal.  But which ones?

All ON slots for the month are collected and **sorted by electricity price in ascending order**.  The heuristic then iterates through this list, switching slots OFF starting with the lowest-price hours and subtracting each slot's coal consumption from the running excess.  The process terminates as soon as total coal falls within the limit.  This prioritises retaining the highest-value hours.

**A concrete example from the model output:**

```
Heuristic coal cut 2026-04: turned off 474 slots (coal@Pmin 207671t > eff.limit 155000t)
```

This tells us:
- In April 2026, if both blocks ran wherever available at Pmin (including the DUAL_BLOCK_BOOST and DOW steam load), they would burn **207 671 tonnes** of coal.
- The effective limit is **155 000 tonnes** (the full monthly cap).
- To get below 155 000 tonnes, the heuristic had to turn off **474 (block, hour) slots** — the 474 cheapest-price hours across both blocks.

After this trimming, April's warm-start schedule has both blocks ON during the highest-price hours and OFF during the lowest-price hours, with total coal consumption at Pmin safely within the limit.  The subsequent P-scaling step then uses the remaining headroom to push power above Pmin in all non-pinned hours.

#### Phase 1c — Final cleanup: enforce min-up and min-down again

Turning off hundreds of slots may have created new violations:
- A block might now be ON for only 3 consecutive hours (violating the 8-hour minimum up-time).
- A block might be OFF for only 4 hours between two ON periods (violating the 6-hour minimum down-time).

The heuristic runs up to 10 cleanup passes per block:
- **Minimum down-time**: if a block goes from ON to OFF, the next 6 hours are forced OFF.
- **Minimum up-time**: if a block is ON for fewer than 8 consecutive hours, the entire short ON-period is turned OFF (it is not economical to start up for so few hours).

These passes repeat until no more changes are needed (the schedule stabilises).

#### Phase 2 — Set all derived variables and scale power levels

Once the on/off schedule is finalised, the heuristic fills in all remaining variable values so the warm start is internally consistent:

- **Startup and shutdown flags**: if block goes from OFF to ON, startup = 1; from ON to OFF, shutdown = 1.
- **Start-up tier classification**: based on how long the block was off before starting (< 10 hours → hot, 10–59 hours → warm, ≥ 60 hours → very cold).
- **Power levels**: initially set to $P_{\min}$ (+ 5 MW boost if both blocks are on).
- **both_on and plant_off**: set based on the per-block on/off states.

#### Phase 2 P-scaling — distribute coal headroom across ON hours

After setting power to Pmin, the heuristic has a coal-feasible schedule — but one that wastes coal headroom.  If the monthly limit is 130 000 tonnes and coal at Pmin totals 110 000 tonnes, there are 20 000 tonnes of unused budget.  The P-scaling step uses this headroom to push power levels **above Pmin toward Pmax**, giving MOSEK a better starting point.

The procedure for each constrained month:

1. **Compute coal at current P** — sum coal consumption across all ON slots at their current power level (Pmin + boost).

2. **Compute headroom** — $\text{coal\_headroom} = \text{limit} - \text{coal\_at\_pmin}$.  If zero or negative, skip this month.

3. **Collect growable slots** — ON hours where power can be increased.  Two types of hours are **excluded**:
   - **Startup hours**: P must stay at Pmin (startup power pinning constraint).
   - **Pre-shutdown hours**: the hour before a shutdown must also be at Pmin.

4. **For each growable slot**, compute the available room: $\text{room} = P_{\max} - P_{\text{current}}$ — and the marginal coal cost to fill it: $\text{coal\_slope} \times \text{room}$.

5. **Distribute headroom proportionally** — compute a single scale factor:

$$\text{scale} = \min\!\left(\frac{\text{coal\_headroom}}{\sum \text{coal\_slope} \times \text{room}},\; 1.0\right)$$

6. **Apply** — each growable slot gets: $P_{b,t} \mathrel{+}= \text{room} \times \text{scale}$.

For example, if $\text{scale} = 0.6$, every growable hour operates at 60% of its Pmin-to-Pmax range.  This uniform allocation is not optimal — MOSEK will subsequently redistribute coal to the highest-value hours — but it provides a materially better initial objective than flat Pmin dispatch.

#### Phase 2 final — consistency pass

After P-scaling:

- **Effective power and running costs**: computed from P, DOW, and cost curves to match the constraint definitions.
- **P_eff capping**: if P_eff would exceed its variable upper bound, it is capped and P is back-adjusted.
- **Constraint violation diagnostic**: the heuristic iterates every active constraint in the Pyomo model, evaluates $\text{body} - \text{lb}$ and $\text{ub} - \text{body}$, and reports any violations.  This validates that the warm-start schedule is internally consistent.

The completed schedule is then injected into MOSEK (see Section 8).

### 5.6  No annual coal limit

The model does **not** enforce a yearly cap.  Each month is independent — unused coal does not roll over.  This reflects the contractual structure of coal deliveries.

---

## 6. Coal Interactions with Other Constraints

### 6.1  Coal limit vs. minimum up-time

If prices spike for only 3 hours, a start-up still commits the block for 8 hours (minimum up-time), during which the remaining 5 low-price hours burn coal with marginal or negative returns.  Under a tight coal limit, the solver may forgo the start entirely — the coal consumed during the unprofitable tail hours outweighs the revenue from the price spike.

### 6.2  Coal and DOW interaction

The DOW steam load is contractually fixed each hour and must be delivered whenever the plant is operating.  Because coal consumption depends on $P_{\text{eff}}$ (which includes DOW), the steam obligation directly increases monthly coal consumption without contributing to electrical output.  In coal-constrained months, this raises the effective coal cost per MWh of electricity sold.

### 6.3  Single-block vs. dual-block dispatch

With two blocks, the solver has an additional degree of freedom: dispatching only one block halves the hourly coal burn (at the expense of lower production capacity).  Under tight coal constraints, the solver frequently operates one block during shoulder hours and commits the second block only for the highest-price peaks.

### 6.4  Shadow prices on coal

The value of an additional tonne of coal under binding constraints is formalised through shadow prices.

---

## 7. Shadow Prices: Measuring the Value of Scarce Coal

### 7.1  Definition

The **shadow price** (dual value) of a coal-limit constraint is the marginal increase in optimal profit per additional tonne of coal.  If the optimal profit is €5 000 000 and relaxing the coal limit by one tonne yields €5 000 020, the shadow price is €20/t.

Formally:

$$\lambda_{y,m} = \frac{\partial \;\text{Optimal Profit}}{\partial \;\text{Coal Limit}_{y,m}}$$

A high shadow price indicates severe scarcity — the plant is foregoing significant profit due to the coal constraint.  A zero shadow price indicates the constraint is non-binding (the plant has surplus coal).

### 7.2  How the model computes shadow prices

Dual values (shadow prices) are only well-defined for **Linear Programmes (LP)** with continuous variables.  Our MILP contains binary on/off decisions where the standard LP duality theory does not directly apply.  The model uses a standard fix-and-relax procedure to work around this:

#### Step 1 — Solve the original MILP

MOSEK determines the optimal on/off schedule for all approximately 6 400 hours × 2 blocks.

#### Step 2 — Fix all integer variables and relax their domains

This step converts the MILP into a pure LP by:

**A) Fixing** each binary variable at its rounded MIP solution value ($0$ or $1$).  All on/off decisions become frozen constants.

**B) Relaxing domains** from `Binary` to `NonNegativeReals`.  This removes the integrality label so the solver treats the problem as a continuous LP and can compute dual values.

Both operations are required: fixing without relaxation would leave the solver in MIP mode (no duals reported); relaxation without fixing would allow fractional on/off values.  Together, they preserve the exact MIP schedule while enabling LP duality.

The continuous variables ($P_{b,t}$, $P_{\text{eff}}$, run costs) remain free to adjust between their bounds.  The LP then answers: *given this on/off schedule, what are the optimal power levels, and what is the marginal value of relaxing each coal constraint?*

#### Step 3 — Attach a dual suffix and re-solve

A Pyomo `Suffix` object (`dual`, direction `IMPORT`) is attached, instructing the solver to return dual values for all constraints.

MOSEK solves the resulting LP — typically in seconds, compared to minutes for the original MILP.

#### Step 4 — Read the duals on the coal constraints

For each month $(y, m)$ that has a coal limit constraint:

$$\lambda_{y,m} = \texttt{m.dual[m.coal\_monthly\_limit[ym]]}$$

MOSEK returns a positive dual for a binding $\le$ constraint in a maximisation problem.  This value is the shadow price in **EUR per tonne of coal**: the marginal increase in optimal profit per additional tonne of coal.

For non-binding constraints (coal usage below the limit), the dual is zero — additional coal would have no effect on profit.

#### Step 5 — Restore model state

All domains are restored to their original integer/binary type.  Variables that were newly fixed are unfixed.  The `dual` suffix is removed.  The model is returned to its pre-LP state, ready for the merchant re-solve.

```
  MILP solve                 ──►   Fix all 53,000 integer vars at MIP values
  (finds on/off schedule)          + relax domains Binary → NonNegativeReals
                                                             │
                                                    Problem is now a pure LP
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

The shadow price directly informs procurement: if additional spot coal can be acquired for less than the shadow price, the purchase is profitable.

#### Mechanism of value creation

With the on/off schedule frozen, the LP retains freedom to adjust power levels between $P_{\min}$ and $P_{\max}$.  An additional tonne of coal enables higher output in the most profitable hours while permitting load reduction elsewhere.  The dual value quantifies this flexibility per marginal tonne.

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

These quantify the coal constraint value under a hypothetical pure-merchant scenario (no DOW obligation).  The procedure:

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

The **difference** between the two shadow prices quantifies the **coal cost of the DOW obligation**.  For example, if the full shadow price is 63 EUR/t and the merchant shadow is 52 EUR/t, the DOW contract imposes 11 EUR/t of additional coal pressure.  This decomposition is essential for DOW contract valuation and renegotiation.

#### The role of the grid fee in shadow prices

The grid fee ($\text{gridfee}_t$) materially influences shadow prices:

- When the plant is offline, it pays $(\text{Price}_t + \text{gridfee}_t) \times \text{off\_consumption}$ for auxiliary and DOW backup power.
- A higher grid fee increases the cost of shutdown, incentivising continued operation even with thin electricity margins.
- Continued operation consumes coal, tightening the monthly constraint.
- This pushes shadow prices **upward**.

In the LP re-solve, the grid fee enters the OFF_costs expression directly.  The LP trades off between continued operation (burning coal but avoiding grid fees) and shutdown (saving coal but incurring grid fees).  The shadow price reflects this trade-off at the margin.

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

### 7.5  Illustrative example

| Month | Coal limit (kt) | Coal used (kt) | Full shadow price (EUR/t) | Merchant shadow (EUR/t) |
|-------|-----------------|----------------|---------------------|------------------------|
| Apr   | 90              | 90.0 (binding) | 18.50               | 12.30                  |
| May   | 95              | 82.4 (slack)   | 0.00                | 0.00                   |
| Jun   | 85              | 85.0 (binding) | 24.70               | 16.10                  |

- **April**: Fully binding.  Marginal value of coal is €18.50/t (full), of which €6.20/t is attributable to the DOW obligation.
- **May**: Non-binding — surplus coal, zero shadow price.
- **June**: Tightest month.  Marginal coal value reaches €24.70/t.

### 7.6  Applications of shadow prices

Shadow prices support several commercial and operational decisions:

- **Coal procurement**: If spot coal is available below the shadow price, the marginal purchase is NPV-positive.
- **DOW contract valuation**: The full-vs.-merchant differential quantifies the coal cost attributable to the DOW obligation.
- **Maintenance planning**: Outages should be scheduled in low-shadow-price months where coal is not the binding constraint.
- **Hedging and forward sales**: Shadow prices inform the effective marginal cost of generation.

---

## 8. End-to-End Workflow

### 8.1  Processing pipeline

1. **Data preparation**: Read the Excel input file.  Merge Block A and Block B hourly data.  Compute linearised cost curves and coal curves.  Parse start-up tiers and coal limits.

2. **Warm start**: Before calling the solver, the model constructs an initial feasible schedule:
   - Default everything to ON (respecting unavailability and initial state).
   - Enforce min-down in 3 cleanup passes.
   - If coal limits exist, trim the cheapest-price hours so each month stays within the coal cap (using coal rates that account for DUAL_BLOCK_BOOST and DOW).
   - Enforce min-up and min-down again in up to 10 cleanup passes.
   - Set power levels to Pmin (+ boost if both on), then scale P up toward Pmax proportionally within remaining coal headroom.
   - Compute P_eff and run_costs consistently; run a full constraint violation diagnostic.

3. **Warm-start injection**: The heuristic schedule is injected into MOSEK using a low-level API mechanism:
   - The solver's internal `_apply_solver` method is intercepted to gain access to the MOSEK task object.
   - **Only integer variables** (on/off switches, startup, hot_start, vcold_start) are written into the initial solution vector via `task.putxx()`.  Continuous variables (P, P_eff, run_costs) are left at zero.
   - This is deliberate: MOSEK's **CONSTRUCT_SOL** feature (enabled via `MSK_IPAR_MIO_CONSTRUCT_SOL = MSK_ON`) takes the injected integer hints and solves an internal LP to find the optimal continuous variable values that go with those on/off decisions.  CONSTRUCT_SOL often finds better P-levels than the heuristic's P-scaling, because it optimises continuously rather than using a uniform scale factor.
   - After injection, the code verifies the putxx took effect by reading the values back via `task.getxx()` and comparing.
   - An estimated warm-start objective is computed from MOSEK's cost vector to confirm the injected solution is reasonable.

4. **MIP solve**: MOSEK searches for the profit-maximising schedule.  It uses branch-and-bound (an algorithm that systematically tries combinations of on/off decisions).  The solver is given a **10-minute time limit** and a **1% optimality gap tolerance** – it stops when it finds a solution within 1% of the theoretical best, or when time runs out (whichever comes first).  A **MIO_SEED of 7** controls the random exploration path of the branch-and-bound tree for reproducibility.

5. **Termination check**: The model accepts three termination conditions: `optimal` (proven optimal within gap), `maxTimeLimit` (time expired but a feasible solution exists), and `feasible` (a feasible solution found but not proven optimal).

6. **Shadow-price LP**: After the MIP, fix all on/off decisions and re-solve as a pure LP to extract dual values on the coal constraints (twice: once with DOW, once without).

7. **Result extraction**: Read all variable values back into the DataFrame.  Compute detailed PnL columns: electricity revenue, running cost, start-up cost, OFF costs, DOW revenue.

8. **Audit**: A multi-level verification ensures the extracted results match the solver's answer:
   - **Objective check**: verify that the sum of hourly PnL equals the objective value (`Obj/PnL delta`).
   - **Component reconciliation**: sum each PnL component (revenue, costs, DOW, OFF) independently and compare to the objective.
   - **Per-hour audit**: find the 12 worst hours where PnL does not match the component sum.
   - **Pyomo component audit**: re-evaluate every Pyomo expression from the model and compare against the DataFrame column sums.

9. **Reporting**: Write everything to a multi-sheet Excel workbook:
   - **Results** sheet – hourly detail with all PnL components, coal consumption, CO₂ emissions, and merchant/DOW splits.
   - **Monthly** sheet – aggregated plant-level totals.
   - **Monthly_A / Monthly_B** – per-block monthly summaries.

### 8.2  Configuration parameters

| Parameter | Default | Effect |
|-----------|---------|--------|
| `USE_COAL_CONSTRAINS` | True | Enable/disable monthly coal caps |
| `USE_DOW_OPPORTUNITY_COSTS` | True | Include DOW steam in P_eff and DOW revenues/OFF costs (True) or treat as pure merchant (False) |
| `MOSEK_MIO_TOL_REL_GAP` | 1% | Acceptable gap between best solution and theoretical optimum |
| `MOSEK_MIO_MAX_TIME` | 600 s | Maximum solver time |
| `MIO_SEED` | 7 | Random seed for branch-and-bound exploration (reproducibility) |
| `CONSTRUCT_SOL` | ON | MOSEK constructs an LP solution from integer hints before branching |
| `MIN_UP` | 8 h | Minimum hours a block must stay on after starting |
| `MIN_DOWN` | 6 h | Minimum hours a block must stay off after stopping |
| `INITIAL_ON` | {"A": 0, "B": 1} | Starting state of each block (0 = off, 1 = on) |
| `OWN_CONSUMPTION` | 10 MW | Auxiliary power drawn when offline |
| `DOW_OFF_CONSUMPTION` | 130 MW | DOW consumer's electric backup load when plant is offline |
| `DOW_OPPORTUNITY_REVENUE` | 188 EUR/MW | Revenue rate for DOW steam delivery |
| `DOW_OFF_COMPENSATION` | 6.9 EUR/MWh | Partial compensation paid to DOW consumer when plant is offline |
| `DUAL_BLOCK_BOOST` | 5 MW | Extra capacity when both blocks run simultaneously |
| `START_MARGIN_MIN` | 0 EUR | Additional margin added to every start-up cost |
| `USE_SIMPLE_STARTUP_RAMP` | True | Simplified startup mode (no detailed tier ramp profiles) |

### 8.3  Summary

The Schkopau model is a **planning engine**.  It takes thousands of hours of electricity prices, fuel costs, emission costs, and plant capabilities, and determines the most profitable operating schedule while obeying physical and contractual limits.  The most impactful of these limits is the **monthly coal cap**, which forces the plant to be selective about when it runs.  The **shadow prices** computed after the main solve quantify exactly how valuable extra coal would be in each month, enabling smarter procurement, scheduling, and commercial decisions.

The model supports two operating modes — **DOW ON** and **DOW OFF** — allowing direct comparison of the plant's value with and without the DOW steam obligation.  A sophisticated warm-start heuristic with coal-aware pruning, P-scaling, and MOSEK's CONSTRUCT_SOL feature ensures the solver starts from a high-quality initial solution, typically finding near-optimal answers within minutes even for horizons spanning thousands of hours.
