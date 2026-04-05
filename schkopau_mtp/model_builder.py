"""
Build the Pyomo ConcreteModel for the Schkopau dispatch optimisation.

Joint model for two power-plant blocks (A, B).  All variables, parameters,
constraints and the objective function are defined here.  The function
``build_model()`` is the single public entry point.
"""

from __future__ import annotations

import pandas as pd

from pyomo.environ import (
    Any,
    Binary,
    ConcreteModel,
    Constraint,
    ConstraintList,
    NonNegativeReals,
    Objective,
    Param,
    RangeSet,
    Set,
    Var,
    maximize,
    value,
)

from . import config as cfg


# ====================================================================
#  PUBLIC API
# ====================================================================


def build_model(df: pd.DataFrame, cost_meta: dict) -> ConcreteModel:
    """
    Construct a fully parameterised Pyomo ConcreteModel for blocks A+B.

    Parameters
    ----------
    df : pd.DataFrame
        Prepared hourly data (output of ``data_loader.load_and_prepare``).
        Block-specific columns are suffixed ``_A``, ``_B``.
    cost_meta : dict
        Contains per-block ``Pmax_eff_A``, ``Pmax_eff_B``.

    Returns
    -------
    m : ConcreteModel
    """
    T_len = len(df)
    idx = df.set_index("t")

    m = ConcreteModel()
    m.T = RangeSet(0, T_len - 1)
    m.B = Set(initialize=cfg.BLOCKS)

    # --------------------------------------------------------
    #  Common parameters (indexed by T only)
    # --------------------------------------------------------
    m.price = Param(m.T, initialize=idx["Price"].to_dict())
    m.EUA = Param(m.T, initialize=idx["EUA"].to_dict())

    m.gridfee = Param(m.T, initialize=idx["GRIDFEE"].to_dict())

    m.DOW = Param(m.T, initialize=idx["DOW"].to_dict(), mutable=True)
    m.DOW_rev = Param(m.T, initialize=idx["DOW revenues"].to_dict(), mutable=True)

    # --------------------------------------------------------
    #  Per-block parameters (indexed by B × T)
    # --------------------------------------------------------
    def _bt_dict(col_pattern: str) -> dict:
        """Build {(b, t): value} dict from columns named ``<col_pattern>_<b>``."""
        d: dict = {}
        for b in cfg.BLOCKS:
            col = f"{col_pattern}_{b}"
            series = idx[col].to_dict()
            for t, v in series.items():
                d[(b, t)] = v
        return d

    m.Pmin = Param(m.B, m.T, initialize=_bt_dict("Pmin"))
    m.Pmax = Param(m.B, m.T, initialize=_bt_dict("Pmax"))
    m.unavailibility = Param(m.B, m.T, initialize=_bt_dict("unavailibility"))
    m.cost_slope = Param(m.B, m.T, initialize=_bt_dict("cost_slope"))
    m.cost_fixed = Param(m.B, m.T, initialize=_bt_dict("cost_fixed"))

    # Per-block Pmax_eff upper bounds (scalar per block)
    _pmax_eff = {b: cost_meta[f"Pmax_eff_{b}"] for b in cfg.BLOCKS}

    # Starts tab data (tiered costs & ramp profiles per block)
    _starts = cost_meta.get("starts", {})

    # Pre-compute per-block tiered costs and ramp profiles (3 tiers)
    _hot_cost: dict = {}
    _warm_cost: dict = {}
    _vcold_cost: dict = {}
    _ramp: dict = {}  # {block: {tier_name: [ramp_mw_h1, ...]}}
    for b in cfg.BLOCKS:
        tiers = {t["name"]: t for t in _starts.get(b, [])}
        _hot_cost[b] = tiers.get("hot", {}).get("cost", 47_510.0)
        _warm_cost[b] = tiers.get("warm", {}).get("cost", 38_291.0)
        _vcold_cost[b] = tiers.get("vcold", {}).get("cost", 60_251.0)
        _ramp[b] = {
            "hot": tiers.get("hot", {}).get("ramp", [170, 262, 440]),
            "warm": tiers.get("warm", {}).get("ramp", [240, 440, 440]),
            "vcold": tiers.get("vcold", {}).get("ramp", [30, 180, 397, 440]),
        }

    # Store ramp data on model for warm-start heuristic access
    m._ramp_data = _ramp

    # --------------------------------------------------------
    #  Per-block decision variables (indexed by B × T)
    # --------------------------------------------------------
    m.on = Var(m.B, m.T, within=Binary)
    m.P = Var(m.B, m.T, bounds=lambda m, b, t: (0, float(value(m.Pmax[b, t])) + cfg.DUAL_BLOCK_BOOST))
    m.startup = Var(m.B, m.T, within=Binary)
    m.shutdown = Var(m.B, m.T, bounds=(0, 1))  # continuous – implied binary by equality + on integrality

    m.P_eff = Var(m.B, m.T, bounds=lambda m, b, t: (0, _pmax_eff[b]))
    m.run_costs = Var(m.B, m.T, bounds=(0, None))

    # Plant-level coupling variables (indexed by T only)
    m.both_on = Var(m.T, bounds=(0, 1))     # 1 iff both blocks online
    m.plant_off = Var(m.T, bounds=(0, 1))   # 1 iff both blocks offline

    # Tiered startup variables (indexed by B × T)
    m.hot_start = Var(m.B, m.T, within=Binary)       # 1 if startup after <10h
    m.vcold_start = Var(m.B, m.T, within=Binary)     # 1 if startup after ≥60h
    m.in_ramp = Var(m.B, m.T, bounds=(0, 1))         # 1 during startup ramp hours

    # Optional simplified startup mode: no detailed tier ramp profiles.
    # Keep in_ramp fixed to 0 so p_lower uses plain Pmin bounds.
    if cfg.USE_SIMPLE_STARTUP_RAMP:
        for b in cfg.BLOCKS:
            for t in range(T_len):
                m.in_ramp[b, t].fix(0)

    # Fix initial ON state per block
    for b in cfg.BLOCKS:
        init_unavail = df.loc[0, f"unavailibility_{b}"]
        if init_unavail >= 0.5:
            m.on[b, 0].fix(0)
        else:
            m.on[b, 0].fix(cfg.INITIAL_ON[b])

    # --------------------------------------------------------
    #  Constraints
    # --------------------------------------------------------
    _add_availability_constraints(m)
    _add_coupling_constraints(m)
    _add_cost_constraints(m)
    _add_power_bounds(m)
    _add_startup_shutdown_constraints(m, T_len)
    _add_off_hours_and_tier_constraints(m, T_len)
    if not cfg.USE_SIMPLE_STARTUP_RAMP:
        _add_startup_ramp_constraints(m, T_len, _ramp)
    else:
        print("--- Startup ramp mode: SIMPLE (Pmin/Pmax only)")
    _add_min_up_down_constraints(m, T_len)
    _add_shutdown_ramp_constraints(m)

    # --------------------------------------------------------
    #  Monthly coal consumption constraints
    # --------------------------------------------------------
    coal_limits = cost_meta.get("coal_limits", {})
    if cfg.USE_COAL_CONSTRAINS and coal_limits:
        m.coal_slope = Param(m.B, m.T, initialize=_bt_dict("coal_slope"))
        m.coal_fixed = Param(m.B, m.T, initialize=_bt_dict("coal_fixed"))

        # Group time indices by (year, month)
        _month_hours: dict[tuple[int, int], list[int]] = {}
        for t in range(T_len):
            ym = (int(idx.iloc[t]["year"]), int(idx.iloc[t]["month_num"]))
            _month_hours.setdefault(ym, []).append(t)

        # Store on model for warm-start heuristic access
        m._month_hours = _month_hours

        # Keep only months that have a limit defined
        _active_months = [ym for ym in _month_hours if ym in coal_limits]
        if _active_months:
            m.coal_months = Set(initialize=_active_months, dimen=2)
            m.coal_limit_t = Param(
                m.coal_months,
                initialize={(y, mo): coal_limits[(y, mo)] * 1000.0 for y, mo in _active_months},
            )

            def coal_monthly_rule(m, y, mo):
                hours = _month_hours[(y, mo)]
                return (
                    sum(
                        m.coal_slope[b, t] * m.P_eff[b, t]
                        + m.coal_fixed[b, t] * m.on[b, t]
                        for b in m.B for t in hours
                    )
                    <= m.coal_limit_t[y, mo]
                )

            m.coal_monthly_limit = Constraint(m.coal_months, rule=coal_monthly_rule)
            print(f"--- Coal constraints: {len(_active_months)} months active")

    # --------------------------------------------------------
    #  Objective
    # --------------------------------------------------------
    _add_objective(m, _hot_cost, _warm_cost, _vcold_cost)

    return m


def warm_start_heuristic(m) -> None:
    """Set initial variable values via greedy price-vs-cost heuristic.

    This gives MOSEK a feasible starting point so it can begin pruning
    branches immediately instead of searching blindly.
    """
    T_list = sorted(m.T)
    B_list = sorted(m.B)
    T_len = len(T_list)

    # --- Phase 1: "always-on" baseline schedule per block ---
    on_hint = {}
    for b in B_list:
        on_b = [0] * T_len
        for t in T_list:
            unavail = float(value(m.unavailibility[b, t]))
            if unavail >= 0.5:
                on_b[t] = 0
            else:
                on_b[t] = 1  # default: stay on whenever available

        # Force t=0 to match the model's fixed initial state
        init_unavail = float(value(m.unavailibility[b, 0]))
        if init_unavail >= 0.5:
            on_b[0] = 0
        else:
            on_b[0] = cfg.INITIAL_ON[b]

        # Enforce MIN_DOWN after each forced-off period
        for _pass in range(3):
            i = 0
            while i < T_len:
                if on_b[i] == 0 and i > 0 and on_b[i - 1] == 1:
                    for k in range(cfg.MIN_DOWN):
                        if i + k < T_len:
                            on_b[i + k] = 0
                    i += cfg.MIN_DOWN
                else:
                    i += 1

        on_hint[b] = on_b

    # --- Phase 1b: respect monthly coal constraints ---
    if hasattr(m, "coal_monthly_limit") and hasattr(m, "_month_hours"):
        # Coal rate at Pmin — MOSEK CONSTRUCT_SOL optimises continuous P,
        # so we only need the on/off schedule feasible at minimum coal.
        # DOW only goes to DOW_BLOCK when both blocks are on.
        coal_rate_min: dict = {}
        for b in B_list:
            for t in T_list:
                pmin = float(value(m.Pmin[b, t]))
                dow = float(value(m.DOW[t])) if b == cfg.DOW_BLOCK else 0.0
                p_eff_min = pmin + dow
                rate = (float(value(m.coal_slope[b, t])) * p_eff_min
                        + float(value(m.coal_fixed[b, t])))
                coal_rate_min[(b, t)] = max(rate, 0.0)

        _month_t = m._month_hours

        for ym in sorted(_month_t):
            if ym not in {tuple(k) for k in m.coal_months}:
                continue
            limit = float(value(m.coal_limit_t[ym]))
            hours = _month_t[ym]

            # Use P_eff@Pmin (not just P@Pmin) — DOW adds coal consumption.
            # Also apply 90% safety margin for MIN_UP extension overhead.
            effective_limit = limit * 0.85

            total_coal = sum(
                coal_rate_min[(b, t)] * on_hint[b][t]
                for b in B_list for t in hours
            )
            if total_coal <= effective_limit:
                continue

            # Turn off cheapest (block, hour) slots until coal fits
            slots = []
            for t in hours:
                pr = float(value(m.price[t]))
                for b in B_list:
                    if on_hint[b][t] == 1 and not m.on[b, t].fixed:
                        slots.append((pr, coal_rate_min[(b, t)], b, t))
            slots.sort()

            excess = total_coal - effective_limit
            n_off = 0
            for pr, rate, b, t in slots:
                if excess <= 0:
                    break
                on_hint[b][t] = 0
                excess -= rate
                n_off += 1

            print(f"    Heuristic coal cut {ym[0]}-{ym[1]:02d}: "
                  f"turned off {n_off} slots "
                  f"(coal@Pmin {total_coal:.0f}t > eff.limit {effective_limit:.0f}t)")

    # --- Phase 1c: restore fixed vars, enforce MIN_DOWN and MIN_UP ---
    for b in B_list:
        for t in T_list:
            if m.on[b, t].fixed:
                on_hint[b][t] = round(value(m.on[b, t]))

        for _pass in range(10):
            changed = False

            # Enforce MIN_DOWN: extend off-periods
            i = 0
            while i < T_len:
                if on_hint[b][i] == 0 and i > 0 and on_hint[b][i - 1] == 1:
                    for k in range(cfg.MIN_DOWN):
                        if i + k < T_len and not m.on[b, i + k].fixed:
                            if on_hint[b][i + k] != 0:
                                on_hint[b][i + k] = 0
                                changed = True
                    i += cfg.MIN_DOWN
                else:
                    i += 1

            # Enforce MIN_UP: remove short on-runs (simpler than extending)
            i = 0
            while i < T_len:
                if on_hint[b][i] == 1 and (i == 0 or on_hint[b][i - 1] == 0):
                    j = i
                    while j < T_len and on_hint[b][j] == 1:
                        j += 1
                    if j - i < cfg.MIN_UP:
                        for k in range(i, j):
                            if not m.on[b, k].fixed:
                                on_hint[b][k] = 0
                                changed = True
                    i = max(j, i + 1)
                else:
                    i += 1

            if not changed:
                break

    # --- Phase 2: set ALL integer variable values consistently ---
    for b in B_list:
        on_b = on_hint[b]
        off_count = 0
        if on_b[0] == 0:
            off_count = 200

        for t in T_list:
            on_val = on_b[t]
            unavail = float(value(m.unavailibility[b, t]))
            if unavail >= 0.5:
                on_val = 0

            if not m.on[b, t].fixed:
                m.on[b, t].value = on_val

            # startup / shutdown
            prev_on = on_b[t - 1] if t > 0 else cfg.INITIAL_ON[b]
            su = 1 if on_val == 1 and prev_on == 0 else 0
            sd = 1 if on_val == 0 and prev_on == 1 else 0
            if not m.startup[b, t].fixed:
                m.startup[b, t].value = su
            if not m.shutdown[b, t].fixed:
                m.shutdown[b, t].value = sd

            # Track off-count for tier classification
            if on_val == 0:
                off_count += 1 if t > 0 else 200
            else:
                off_count_at_start = off_count
                off_count = 0

            # tier binaries
            if su == 1:
                m.hot_start[b, t].value = 1 if off_count_at_start < 10 else 0
                m.vcold_start[b, t].value = 1 if off_count_at_start >= 60 else 0
            else:
                m.hot_start[b, t].value = 0
                m.vcold_start[b, t].value = 0

            # P — use Pmin (+ boost if both on) so constraints are satisfied.
            if on_val == 1:
                pmin = float(value(m.Pmin[b, t]))
                both = 1 if all(on_hint[bb][t] == 1 for bb in B_list) else 0
                m.P[b, t].value = pmin + cfg.DUAL_BLOCK_BOOST * both
            else:
                m.P[b, t].value = 0

    if not cfg.USE_SIMPLE_STARTUP_RAMP:
        # in_ramp: separate pass so later startups don't overwrite earlier ones
        for b in B_list:
            # First clear all
            for t in T_list:
                m.in_ramp[b, t].value = 0
            # Then set ramp windows for each startup
            for t in T_list:
                su = round(m.startup[b, t].value or 0)
                if su == 1:
                    for h in range(cfg.MAX_RAMP_HOURS):
                        if t + h < T_len:
                            m.in_ramp[b, t + h].value = 1

        # Fix P during ramp hours to satisfy ramp upper/lower bounds.
        # P must be exactly at the ramp limit for the active tier.
        _ramp_data = getattr(m, '_ramp_data', {})
        for b in B_list:
            for t in T_list:
                su = round(m.startup[b, t].value or 0)
                if su == 1:
                    # Determine tier
                    hot = round(m.hot_start[b, t].value or 0)
                    vcold = round(m.vcold_start[b, t].value or 0)
                    if hot:
                        tier = "hot"
                    elif vcold:
                        tier = "vcold"
                    else:
                        tier = "warm"
                    ramp_profile = _ramp_data.get(b, {}).get(tier, [])
                    for h in range(cfg.MAX_RAMP_HOURS):
                        tt = t + h
                        if tt >= T_len:
                            break
                        if h < len(ramp_profile):
                            both = 1 if all(on_hint[bb][tt] == 1 for bb in B_list) else 0
                            pmax_tt = float(value(m.Pmax[b, tt]))
                            dow_tt = float(value(m.DOW[tt]))
                            # p_upper: P ≤ (Pmax - DOW)*on + boost*both_on (for DOW block)
                            if b == cfg.DOW_BLOCK:
                                p_cap = (pmax_tt - dow_tt) + cfg.DUAL_BLOCK_BOOST * both
                            else:
                                # For non-DOW block: P ≤ Pmax*on - DOW*(on - both_on) + boost*both_on
                                p_cap = pmax_tt - dow_tt * (1 - both) + cfg.DUAL_BLOCK_BOOST * both
                            p_val = min(ramp_profile[h] + cfg.DUAL_BLOCK_BOOST * both, p_cap)
                            m.P[b, tt].value = p_val
    else:
        # Simple mode keeps in_ramp fixed to 0 in build_model().
        # Warm-start P remains at base Pmin/Pmax bounds only.
        pass

    # Plant-level coupling
    for t in T_list:
        on_vals = [on_hint[b][t] for b in B_list]
        m.both_on[t].value = 1 if all(v == 1 for v in on_vals) else 0
        m.plant_off[t].value = 1 if all(v == 0 for v in on_vals) else 0

    # Set P_eff and run_costs so the full xx vector is consistent
    _primary = cfg.DOW_BLOCK
    for b in B_list:
        for t in T_list:
            on_val = round(m.on[b, t].value or 0)
            p_val = m.P[b, t].value or 0.0
            dow = float(value(m.DOW[t]))
            both_on_val = m.both_on[t].value or 0

            # P_eff = P + DOW component (mirrors peff_def_rule)
            if b == _primary:
                p_eff = p_val + dow * on_val
            else:
                p_eff = p_val + dow * (on_val - both_on_val)
            # Cap P_eff to its variable bound; back-adjust P if needed
            peff_ub = m.P_eff[b, t].ub
            if p_eff > peff_ub:
                p_eff = peff_ub
                # Reverse-compute P from P_eff
                if b == _primary:
                    m.P[b, t].value = p_eff - dow * on_val
                else:
                    m.P[b, t].value = p_eff - dow * (on_val - both_on_val)
            m.P_eff[b, t].value = p_eff

            # run_costs = cost_slope * P_eff + cost_fixed * on
            rc = (float(value(m.cost_slope[b, t])) * p_eff
                  + float(value(m.cost_fixed[b, t])) * on_val)
            m.run_costs[b, t].value = rc

    print("--- Warm-start heuristic applied")


# ====================================================================
#  INTERNAL HELPERS
# ====================================================================


# ----------------------------------------------------------------
#  Constraint blocks  (all indexed over B × T)
# ----------------------------------------------------------------


def _add_availability_constraints(m: ConcreteModel) -> None:
    def force_off_when_unavail_rule(m, b, t):
        return m.on[b, t] <= 1 - m.unavailibility[b, t]

    m.force_off_when_unavail = Constraint(m.B, m.T, rule=force_off_when_unavail_rule)


def _add_coupling_constraints(m) -> None:
    """Linearise both_on (AND) and plant_off (NOR) of per-block on vars."""
    _bA, _bB = cfg.BLOCKS

    def both_on_ub_A(m, t):
        return m.both_on[t] <= m.on[_bA, t]
    def both_on_ub_B(m, t):
        return m.both_on[t] <= m.on[_bB, t]
    def both_on_lb(m, t):
        return m.both_on[t] >= m.on[_bA, t] + m.on[_bB, t] - 1

    m.both_on_ub_A = Constraint(m.T, rule=both_on_ub_A)
    m.both_on_ub_B = Constraint(m.T, rule=both_on_ub_B)
    m.both_on_lb = Constraint(m.T, rule=both_on_lb)

    def plant_off_lb(m, t):
        return m.plant_off[t] >= 1 - m.on[_bA, t] - m.on[_bB, t]
    def plant_off_ub_A(m, t):
        return m.plant_off[t] <= 1 - m.on[_bA, t]
    def plant_off_ub_B(m, t):
        return m.plant_off[t] <= 1 - m.on[_bB, t]

    m.plant_off_lb = Constraint(m.T, rule=plant_off_lb)
    m.plant_off_ub_A = Constraint(m.T, rule=plant_off_ub_A)
    m.plant_off_ub_B = Constraint(m.T, rule=plant_off_ub_B)


def _add_cost_constraints(m) -> None:
    _primary = cfg.DOW_BLOCK

    def peff_def_rule(m, b, t):
        # DOW follows actual dispatch:
        #   Primary (A): always gets DOW when on
        #   Other   (B): gets DOW only when on AND primary is off
        if b == _primary:
            dow_add = m.DOW[t] * m.on[b, t]
        else:
            # DOW * on_B * (1 - on_A) = DOW * (on_B - both_on)
            dow_add = m.DOW[t] * (m.on[b, t] - m.both_on[t])
        return m.P_eff[b, t] == m.P[b, t] + dow_add

    m.peff_def = Constraint(m.B, m.T, rule=peff_def_rule)

    def cost_rule(m, b, t):
        return m.run_costs[b, t] == m.cost_slope[b, t] * m.P_eff[b, t] + m.cost_fixed[b, t] * m.on[b, t]

    m.cost_def = Constraint(m.B, m.T, rule=cost_rule)


def _add_power_bounds(m) -> None:
    _boost = cfg.DUAL_BLOCK_BOOST
    _primary = cfg.DOW_BLOCK

    def p_lower(m, b, t):
        # Pmin relaxed during startup ramp hours (in_ramp=1)
        return m.P[b, t] >= m.Pmin[b, t] * m.on[b, t] + _boost * m.both_on[t] - cfg.BIG_M * m.in_ramp[b, t]

    m.p_lower = Constraint(m.B, m.T, rule=p_lower)

    def p_upper(m, b, t):
        # DOW reduces Pmax dynamically based on dispatch:
        #   Primary: always deducted when on
        #   Other:   deducted only when on AND primary is off
        if b == _primary:
            return m.P[b, t] <= (m.Pmax[b, t] - m.DOW[t]) * m.on[b, t] + _boost * m.both_on[t]
        else:
            # Pmax*on - DOW*(on_B - both_on) + boost*both_on
            return m.P[b, t] <= m.Pmax[b, t] * m.on[b, t] - m.DOW[t] * (m.on[b, t] - m.both_on[t]) + _boost * m.both_on[t]

    m.p_upper = Constraint(m.B, m.T, rule=p_upper)


def _add_startup_shutdown_constraints(m, T_len: int) -> None:
    # Tight equality: startup - shutdown = on[t] - on[t-1]
    # Replaces separate >= inequalities; shutdown_link1/2/3 are implied.
    def su_sd_balance(m, b, t):
        if t == 0:
            return m.startup[b, t] - m.shutdown[b, t] == m.on[b, t] - cfg.INITIAL_ON[b]
        return m.startup[b, t] - m.shutdown[b, t] == m.on[b, t] - m.on[b, t - 1]

    m.su_sd_balance = Constraint(m.B, m.T, rule=su_sd_balance)

    def startup_link2(m, b, t):
        return m.startup[b, t] <= m.on[b, t]

    m.startup_link2 = Constraint(m.B, m.T, rule=startup_link2)

    def startup_link3(m, b, t):
        if t == 0:
            return m.startup[b, t] <= 1 - cfg.INITIAL_ON[b]
        return m.startup[b, t] <= 1 - m.on[b, t - 1]

    m.startup_link3 = Constraint(m.B, m.T, rule=startup_link3)


def _add_off_hours_and_tier_constraints(m, T_len: int) -> None:
    """Lookback-based tier classification (no off_hours variable, no Big-M).

    Uses the fact that MIN_UP >= 8 and MIN_DOWN >= 6 to classify tiers
    via direct lookback on on[b, t-k] variables:
      - hot  (off < 10h):  on[b, t-10] = 1  ↔  hot start
      - vcold (off ≥ 60h): use checkpoints spaced MIN_UP apart to detect
                             any on-period in the last 60 hours
    """
    # Vcold checkpoints: spaced by MIN_UP so any on-period ≥ MIN_UP hours
    # is guaranteed to include at least one checkpoint.
    _vc_checks = list(range(cfg.MIN_UP, 61, cfg.MIN_UP))  # [8, 16, 24, 32, 40, 48, 56]
    _n_vc = len(_vc_checks)

    # --- Tier hierarchy ---
    def hot_le_startup(m, b, t):
        return m.hot_start[b, t] <= m.startup[b, t]

    m.hot_le_startup = Constraint(m.B, m.T, rule=hot_le_startup)

    def vcold_le_startup(m, b, t):
        return m.vcold_start[b, t] <= m.startup[b, t]

    m.vcold_le_startup = Constraint(m.B, m.T, rule=vcold_le_startup)

    def tier_exclusivity(m, b, t):
        return m.hot_start[b, t] + m.vcold_start[b, t] <= m.startup[b, t]

    m.tier_exclusivity = Constraint(m.B, m.T, rule=tier_exclusivity)

    # --- Hot detection via lookback at t-10 ---
    def prevent_hot(m, b, t):
        """hot_start only if on[b, t-10] = 1 (block was on 10h ago)."""
        k = max(0, t - 10)
        return m.hot_start[b, t] <= m.on[b, k]

    m.prevent_hot = Constraint(m.B, m.T, rule=prevent_hot)

    def force_hot(m, b, t):
        """Force hot when startup, on[t-10]=1, and not vcold."""
        k = max(0, t - 10)
        return m.hot_start[b, t] >= m.startup[b, t] + m.on[b, k] - 1 - m.vcold_start[b, t]

    m.force_hot = Constraint(m.B, m.T, rule=force_hot)

    # --- Vcold detection via checkpoints ---
    def prevent_vcold(m, b, t):
        """Prevent vcold if any checkpoint shows block was on recently."""
        lookbacks = [m.on[b, max(0, t - k)] for k in _vc_checks]
        return _n_vc * m.vcold_start[b, t] + sum(lookbacks) <= _n_vc

    m.prevent_vcold = Constraint(m.B, m.T, rule=prevent_vcold)

    def force_vcold(m, b, t):
        """Force vcold when startup and all checkpoints show off."""
        lookbacks = [m.on[b, max(0, t - k)] for k in _vc_checks]
        return m.vcold_start[b, t] >= m.startup[b, t] - sum(lookbacks)

    m.force_vcold = Constraint(m.B, m.T, rule=force_vcold)


def _add_startup_ramp_constraints(m, T_len: int, ramp_data: dict) -> None:
    """Multi-hour startup ramp upper bounds + in_ramp indicator."""
    _boost = cfg.DUAL_BLOCK_BOOST
    _H = cfg.MAX_RAMP_HOURS  # 4

    # --- in_ramp indicator: 1 during the first MAX_RAMP_HOURS after startup ---
    cl_ramp_lb = ConstraintList()
    m.in_ramp_lb = cl_ramp_lb
    cl_ramp_ub = ConstraintList()
    m.in_ramp_ub = cl_ramp_ub

    for b in m.B:
        for t in m.T:
            # Lower bounds: in_ramp ≥ startup[b, t-h] for h = 0,...,H-1
            for h in range(_H):
                s = t - h
                if s >= 0:
                    cl_ramp_lb.add(m.in_ramp[b, t] >= m.startup[b, s])

            # Upper bound: in_ramp ≤ sum(startup[b,t-h] for h in 0..H-1)
            terms = [m.startup[b, t - h] for h in range(_H) if t - h >= 0]
            cl_ramp_ub.add(m.in_ramp[b, t] <= sum(terms))

    # --- Ramp upper-bound AND lower-bound constraints per tier ---
    # UB: warm always applies; cold/vcold tighten further
    # LB: tier-exclusive (only the matching tier forces the ramp floor)
    cl_ramp = ConstraintList()
    m.startup_ramp_ub = cl_ramp
    cl_ramp_lb2 = ConstraintList()
    m.startup_ramp_lb2 = cl_ramp_lb2

    for b in m.B:
        hot_ramp = ramp_data[b]["hot"]
        warm_ramp = ramp_data[b]["warm"]
        vcold_ramp = ramp_data[b]["vcold"]

        for t in m.T:
            pmax_bt = float(value(m.Pmax[b, t]))

            # Worst-case Pmax for ramp LB cap: Pmax_orig - DOW
            # (block may serve DOW when starting alone)
            pmax_dow = pmax_bt - float(value(m.DOW[t]))

            for h in range(_H):
                s = t - h  # startup time
                if s < 0:
                    continue

                # --- Upper bounds (each tier tightens if active) ---
                if h < len(hot_ramp):
                    cl_ramp.add(
                        m.P[b, t] <= hot_ramp[h] + _boost * m.both_on[t]
                        + cfg.BIG_M * (1 - m.hot_start[b, s])
                    )
                if h < len(warm_ramp):
                    cl_ramp.add(
                        m.P[b, t] <= warm_ramp[h] + _boost * m.both_on[t]
                        + cfg.BIG_M * (1 - m.startup[b, s])
                    )
                if h < len(vcold_ramp):
                    cl_ramp.add(
                        m.P[b, t] <= vcold_ramp[h] + _boost * m.both_on[t]
                        + cfg.BIG_M * (1 - m.vcold_start[b, s])
                    )

                # --- Lower bounds (tier-exclusive, capped at Pmax - DOW) ---
                # Hot LB: active when hot_start=1
                if h < len(hot_ramp):
                    lb_h = min(hot_ramp[h], pmax_dow)
                    cl_ramp_lb2.add(
                        m.P[b, t] >= lb_h
                        - cfg.BIG_M * (1 - m.hot_start[b, s])
                    )
                # Warm LB: active when startup=1 AND not hot/vcold
                if h < len(warm_ramp):
                    lb_w = min(warm_ramp[h], pmax_dow)
                    cl_ramp_lb2.add(
                        m.P[b, t] >= lb_w
                        - cfg.BIG_M * (1 - m.startup[b, s]
                                       + m.hot_start[b, s]
                                       + m.vcold_start[b, s])
                    )
                # Vcold LB: active when vcold_start=1
                if h < len(vcold_ramp):
                    lb_v = min(vcold_ramp[h], pmax_dow)
                    cl_ramp_lb2.add(
                        m.P[b, t] >= lb_v
                        - cfg.BIG_M * (1 - m.vcold_start[b, s])
                    )


def _add_min_up_down_constraints(m, T_len: int) -> None:
    def min_up(m, b, t):
        if t > T_len - cfg.MIN_UP:
            return Constraint.Skip
        if any(value(m.unavailibility[b, t + k]) >= 0.5 for k in range(cfg.MIN_UP)):
            return Constraint.Skip
        return sum(m.on[b, t + k] for k in range(cfg.MIN_UP)) >= cfg.MIN_UP * m.startup[b, t]

    m.min_up = Constraint(m.B, m.T, rule=min_up)

    def min_down(m, b, t):
        if t == 0 or t > T_len - cfg.MIN_DOWN:
            return Constraint.Skip
        if any(value(m.unavailibility[b, t + k]) >= 0.5 for k in range(cfg.MIN_DOWN)):
            return Constraint.Skip
        return (
            sum(1 - m.on[b, t + k] for k in range(cfg.MIN_DOWN))
            >= cfg.MIN_DOWN * (m.on[b, t - 1] - m.on[b, t])
        )

    m.min_down = Constraint(m.B, m.T, rule=min_down)


def _add_shutdown_ramp_constraints(m) -> None:
    _boost = cfg.DUAL_BLOCK_BOOST

    def shutdown_requires_pmin_lb(m, b, t):
        if t == 0:
            return Constraint.Skip
        return m.P[b, t - 1] >= m.Pmin[b, t - 1] + _boost * m.both_on[t - 1] - cfg.BIG_M * (1 - m.shutdown[b, t])

    def shutdown_requires_pmin_ub(m, b, t):
        if t == 0:
            return Constraint.Skip
        return m.P[b, t - 1] <= m.Pmin[b, t - 1] + _boost * m.both_on[t - 1] + cfg.BIG_M * (1 - m.shutdown[b, t])

    m.shutdown_requires_pmin_lb = Constraint(m.B, m.T, rule=shutdown_requires_pmin_lb)
    m.shutdown_requires_pmin_ub = Constraint(m.B, m.T, rule=shutdown_requires_pmin_ub)

    # Startup hour: P must equal Pmin (unit starts at minimum load)
    def startup_requires_pmin_lb(m, b, t):
        return m.P[b, t] >= m.Pmin[b, t] * m.on[b, t] + _boost * m.both_on[t] - cfg.BIG_M * (1 - m.startup[b, t])

    def startup_requires_pmin_ub(m, b, t):
        return m.P[b, t] <= m.Pmin[b, t] * m.on[b, t] + _boost * m.both_on[t] + cfg.BIG_M * (1 - m.startup[b, t])

    m.startup_requires_pmin_lb = Constraint(m.B, m.T, rule=startup_requires_pmin_lb)
    m.startup_requires_pmin_ub = Constraint(m.B, m.T, rule=startup_requires_pmin_ub)


# ----------------------------------------------------------------
#  Objective
# ----------------------------------------------------------------


def _add_objective(m, hot_cost, warm_cost, vcold_cost) -> None:
    # Pre-compute incremental costs per block (warm is baseline)
    _hot_delta = {b: hot_cost[b] - warm_cost[b] for b in cfg.BLOCKS}
    _vcold_delta = {b: vcold_cost[b] - warm_cost[b] for b in cfg.BLOCKS}

    def obj(m):
        expr = 0
        for t in m.T:
            for b in m.B:
                # Spot revenue per block
                profit_spot = m.P[b, t] * m.price[t]

                run_costs = m.run_costs[b, t]

                # Tiered startup cost + margin hurdle
                start_cost = (
                    (warm_cost[b] + cfg.START_MARGIN_MIN) * m.startup[b, t]
                    + _hot_delta[b] * m.hot_start[b, t]
                    + _vcold_delta[b] * m.vcold_start[b, t]
                )

                expr += (
                    profit_spot
                    - run_costs
                    - start_cost
                )

            # OFF_costs: grid fee only when BOTH blocks offline
            off_consumption = cfg.OWN_CONSUMPTION
            if cfg.USE_DOW_OPPORTUNITY_COSTS:
                off_consumption += cfg.DOW_OFF_CONSUMPTION
            OFF_costs = m.plant_off[t] * off_consumption * (
                m.price[t] + m.gridfee[t]
            )
            if cfg.USE_DOW_OPPORTUNITY_COSTS:
                OFF_costs -= m.plant_off[t] * cfg.DOW_OFF_CONSUMPTION * cfg.DOW_OFF_COMPENSATION
            expr -= OFF_costs

            # DOW revenue attributed once per hour (plant-level)
            # DOW is active when at least one block is running
            _other = [b for b in cfg.BLOCKS if b != cfg.DOW_BLOCK][0]
            dow_on = (
                m.on[cfg.DOW_BLOCK, t] + m.on[_other, t] - m.both_on[t]
            )
            dow_revenue = m.DOW_rev[t] * dow_on
            expr += dow_revenue

        return expr

    m.obj = Objective(rule=obj, sense=maximize)
