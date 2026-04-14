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
    m._timestamps = idx["Date"].to_dict()  # t -> datetime

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

    # DUO cost/coal parameters (dual-block operation — different efficiencies)
    _has_duo = cost_meta.get("has_duo", False)
    m._has_duo = _has_duo
    if _has_duo:
        # Delta = DUO - Mono (typically negative → cheaper during dual operation)
        _cost_slope_delta: dict = {}
        _cost_fixed_delta: dict = {}
        _coal_slope_delta: dict = {}
        _coal_fixed_delta: dict = {}
        for b in cfg.BLOCKS:
            duo_slope_col = f"cost_slope_duo_{b}"
            duo_fixed_col = f"cost_fixed_duo_{b}"
            mono_slope = idx[f"cost_slope_{b}"].to_dict()
            mono_fixed = idx[f"cost_fixed_{b}"].to_dict()
            if duo_slope_col in idx.columns:
                duo_slope = idx[duo_slope_col].to_dict()
                duo_fixed = idx[duo_fixed_col].to_dict()
                for t in range(T_len):
                    _cost_slope_delta[(b, t)] = float(duo_slope[t]) - float(mono_slope[t])
                    _cost_fixed_delta[(b, t)] = float(duo_fixed[t]) - float(mono_fixed[t])
            else:
                for t in range(T_len):
                    _cost_slope_delta[(b, t)] = 0.0
                    _cost_fixed_delta[(b, t)] = 0.0

        m.cost_slope_delta = Param(m.B, m.T, initialize=_cost_slope_delta)
        m.cost_fixed_delta = Param(m.B, m.T, initialize=_cost_fixed_delta)

    # Per-block Pmax_eff upper bounds (scalar per block)
    _pmax_eff = {b: cost_meta[f"Pmax_eff_{b}"] for b in cfg.BLOCKS}
    m._pmax_eff = _pmax_eff  # expose for warm-start UB clip

    # Starts tab data (tiered costs & ramp profiles per block)
    _starts = cost_meta.get("starts", {})

    # Pre-compute per-block tiered costs and ramp profiles (5 tiers)
    _hot_cost: dict = {}
    _warm_cost: dict = {}
    _cold_cost: dict = {}
    _vcold_cost: dict = {}
    _ramp: dict = {}  # {block: {tier_name: [ramp_mw_h1, ...]}}
    for b in cfg.BLOCKS:
        tiers = {t["name"]: t for t in _starts.get(b, [])}
        _hot_cost[b] = tiers.get("hot", {}).get("cost", 25_510.0)
        _warm_cost[b] = tiers.get("warm", {}).get("cost", 38_291.0)
        _cold_cost[b] = tiers.get("cold", {}).get("cost", 39_910.0)
        _vcold_cost[b] = tiers.get("vcold", {}).get("cost", 60_251.0)
        _ramp[b] = {
            "very_hot": tiers.get("very_hot", {}).get("ramp", [262, 397, 440]),
            "hot": tiers.get("hot", {}).get("ramp", [170, 262, 440]),
            "warm": tiers.get("warm", {}).get("ramp", [216, 433, 440]),
            "cold": tiers.get("cold", {}).get("ramp", [203, 235, 440]),
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

    # When USE_DOW=False, P_eff=P so UB matches Pmax; when True, P_eff=P+DOW so UB=Pmax
    m.P_eff = Var(m.B, m.T, bounds=lambda m, b, t: (0, _pmax_eff[b]))
    m.run_costs = Var(m.B, m.T, bounds=(0, None))

    # Plant-level coupling variables (indexed by T only)
    m.both_on = Var(m.T, bounds=(0, 1))     # 1 iff both blocks online
    m.plant_off = Var(m.T, bounds=(0, 1))   # 1 iff both blocks offline

    # DUO variable: P_eff_duo = P_eff × both_on (McCormick linearisation)
    if _has_duo:
        m.P_eff_duo = Var(m.B, m.T, bounds=lambda m, b, t: (0, _pmax_eff[b]))

    # Tiered startup variables (indexed by B × T)
    m.hot_start = Var(m.B, m.T, within=Binary)       # 1 if startup after 5–10h off
    m.cold_start = Var(m.B, m.T, within=Binary)      # 1 if startup after 60–100h off
    m.vcold_start = Var(m.B, m.T, within=Binary)     # 1 if startup after ≥100h off
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
    if _has_duo:
        _add_duo_mccormick_constraints(m, _pmax_eff)
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

        # DUO coal deltas (difference between DUO and Mono coal curves)
        if _has_duo:
            _coal_slope_delta: dict = {}
            _coal_fixed_delta: dict = {}
            for b in cfg.BLOCKS:
                duo_col = f"coal_slope_duo_{b}"
                if duo_col in idx.columns:
                    mono_s = idx[f"coal_slope_{b}"].to_dict()
                    duo_s = idx[duo_col].to_dict()
                    mono_f = idx[f"coal_fixed_{b}"].to_dict()
                    duo_f = idx[f"coal_fixed_duo_{b}"].to_dict()
                    for t in range(T_len):
                        _coal_slope_delta[(b, t)] = float(duo_s[t]) - float(mono_s[t])
                        _coal_fixed_delta[(b, t)] = float(duo_f[t]) - float(mono_f[t])
                else:
                    for t in range(T_len):
                        _coal_slope_delta[(b, t)] = 0.0
                        _coal_fixed_delta[(b, t)] = 0.0
            m.coal_slope_delta = Param(m.B, m.T, initialize=_coal_slope_delta)
            m.coal_fixed_delta = Param(m.B, m.T, initialize=_coal_fixed_delta)

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

            _duo = _has_duo

            def coal_monthly_rule(m, y, mo):
                hours = _month_hours[(y, mo)]
                expr = sum(
                    m.coal_slope[b, t] * m.P_eff[b, t]
                    + m.coal_fixed[b, t] * m.on[b, t]
                    for b in m.B for t in hours
                )
                # DUO adjustment: when both blocks ON, use DUO coal curves
                if _duo:
                    expr += sum(
                        m.coal_slope_delta[b, t] * m.P_eff_duo[b, t]
                        + m.coal_fixed_delta[b, t] * m.both_on[t]
                        for b in m.B for t in hours
                    )
                return expr <= m.coal_limit_t[y, mo]

            m.coal_monthly_limit = Constraint(m.coal_months, rule=coal_monthly_rule)

            print(f"--- Coal constraints: {len(_active_months)} months active")

    # --------------------------------------------------------
    #  Objective
    # --------------------------------------------------------
    _add_objective(m, _hot_cost, _warm_cost, _cold_cost, _vcold_cost)

    return m


def warm_start_heuristic(m) -> None:
    """Set initial variable values via greedy price-vs-cost heuristic.

    This gives MOSEK a feasible starting point so it can begin pruning
    branches immediately instead of searching blindly.
    """
    T_list = sorted(m.T)
    B_list = sorted(m.B)
    T_len = len(T_list)

    # --- Phase 1: turn ON all available hours ---
    on_hint = {}
    for b in B_list:
        on_b = [0] * T_len
        for t in T_list:
            unavail = float(value(m.unavailibility[b, t]))
            if unavail >= 0.5:
                on_b[t] = 0
            else:
                on_b[t] = 1

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
        n_on = sum(on_b)
        n_avail = sum(1 for t in T_list if float(value(m.unavailibility[b, t])) < 0.5)
        n_unavail = T_len - n_avail
        if n_avail > 0:
            print(f"    Phase 1 block {b}: {n_on} ON / {T_len} total "
                  f"({n_avail} avail, {n_unavail} unavail = "
                  f"{n_avail/T_len*100:.0f}% availability)")
        else:
            print(f"    Phase 1 block {b}: fully unavailable ({T_len} hours)")

        # Print contiguous unavailability periods
        if n_unavail > 0:
            unavail_ts = [t for t in T_list if float(value(m.unavailibility[b, t])) >= 0.5]
            periods = []
            start = unavail_ts[0]
            prev = unavail_ts[0]
            for t in unavail_ts[1:]:
                if t == prev + 1:
                    prev = t
                else:
                    periods.append((start, prev))
                    start = t
                    prev = t
            periods.append((start, prev))
            for s, e in periods:
                dt_s = m._timestamps[s]
                dt_e = m._timestamps[e]
                print(f"      Block {b} is unavailable from "
                      f"{dt_s.strftime('%d.%m.%Y %H:%M')} - "
                      f"{dt_e.strftime('%d.%m.%Y %H:%M')}")

    # --- Phase 1b: respect monthly coal constraints ---
    if hasattr(m, "coal_monthly_limit") and hasattr(m, "_month_hours"):
        # Coal rate at minimum dispatch level — when both blocks are ON,
        # p_lower forces P ≥ Pmin + boost, so account for that.
        coal_rate_min: dict = {}
        for b in B_list:
            for t in T_list:
                pmin = float(value(m.Pmin[b, t]))
                both = 1 if all(on_hint[bb][t] == 1 for bb in B_list) else 0
                boost = cfg.DUAL_BLOCK_BOOST * both
                if not cfg.USE_DOW_OPPORTUNITY_COSTS:
                    dow = 0.0
                elif b == cfg.DOW_BLOCK:
                    dow = float(value(m.DOW[t]))
                else:
                    # Secondary block carries DOW only when primary is OFF
                    dow = float(value(m.DOW[t])) * (1 - both)
                p_eff_min = pmin + boost + dow
                rate = (float(value(m.coal_slope[b, t])) * p_eff_min
                        + float(value(m.coal_fixed[b, t])))
                coal_rate_min[(b, t)] = max(rate, 0.0)

        _month_t = m._month_hours

        for ym in sorted(_month_t):
            if ym not in {tuple(k) for k in m.coal_months}:
                continue
            limit = float(value(m.coal_limit_t[ym]))
            hours = _month_t[ym]

            # Compute block availability: fraction of block-hours that are available
            avail_hours = sum(
                1 for b in B_list for t in hours
                if float(value(m.unavailibility[b, t])) < 0.5
            )
            max_hours = len(B_list) * len(hours)
            avail_pct = avail_hours / max_hours if max_hours > 0 else 1.0
            # Use full coal limit for Pmin pass (Phase 1b+ handles Pmax scaling)
            effective_limit = limit

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
                m.cold_start[b, t].value = 1 if 60 <= off_count_at_start < 100 else 0
                m.vcold_start[b, t].value = 1 if off_count_at_start >= 100 else 0
            else:
                m.hot_start[b, t].value = 0
                m.cold_start[b, t].value = 0
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
                    cold = round(m.cold_start[b, t].value or 0)
                    vcold = round(m.vcold_start[b, t].value or 0)
                    if hot:
                        tier = "hot"
                    elif vcold:
                        tier = "vcold"
                    elif cold:
                        tier = "cold"
                    else:
                        tier = "warm"
                    ramp_profile = _ramp_data.get(b, {}).get(tier, [])
                    for h in range(cfg.MAX_RAMP_HOURS):
                        tt = t + h
                        if tt >= T_len:
                            break
                        # Startup hour is pinned to Pmin by startup_requires_pmin_*;
                        # skip heuristic ramp overwrite to avoid hint conflicts.
                        if h == 0:
                            continue
                        if h < len(ramp_profile):
                            both = 1 if all(on_hint[bb][tt] == 1 for bb in B_list) else 0
                            pmax_tt = float(value(m.Pmax[b, tt]))
                            dow_tt = float(value(m.DOW[tt]))
                            # p_upper: DOW always deducted from Pmax
                            if b == cfg.DOW_BLOCK:
                                p_cap = (pmax_tt - dow_tt) + cfg.DUAL_BLOCK_BOOST * both
                            else:
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

    # --- Phase 2: scale P up from Pmin toward Pmax within coal budget ---
    if hasattr(m, "coal_monthly_limit") and hasattr(m, "_month_hours"):
        _primary = cfg.DOW_BLOCK
        for ym in sorted(m._month_hours):
            if ym not in {tuple(k) for k in m.coal_months}:
                continue
            limit = float(value(m.coal_limit_t[ym]))
            hours = m._month_hours[ym]

            # Coal at current P (= Pmin)
            coal_at_pmin = 0.0
            for b in B_list:
                for t in hours:
                    on_val = round(m.on[b, t].value or 0)
                    if on_val == 1:
                        p_val = m.P[b, t].value or 0.0
                        dow = float(value(m.DOW[t]))
                        both = round(m.both_on[t].value or 0)
                        if cfg.USE_DOW_OPPORTUNITY_COSTS:
                            if b == _primary:
                                p_eff = p_val + dow
                            else:
                                p_eff = p_val + dow * (1 - both)
                        else:
                            p_eff = p_val  # P_eff = P when DOW flag off
                        coal_at_pmin += (float(value(m.coal_slope[b, t])) * p_eff
                                         + float(value(m.coal_fixed[b, t])))

            coal_headroom = limit - coal_at_pmin
            if coal_headroom <= 0:
                continue

            # Marginal coal per MW increase: coal_slope (same for all P on the linear curve)
            # Collect on-slots with room to grow
            grow_slots = []
            for b in B_list:
                for t in hours:
                    on_val = round(m.on[b, t].value or 0)
                    if on_val != 1:
                        continue
                    # Skip startup/pre-shutdown hours (P must stay at Pmin)
                    if round(m.startup[b, t].value or 0) == 1:
                        continue
                    if t + 1 < T_len and round(m.shutdown[b, t + 1].value or 0) == 1:
                        continue
                    p_val = m.P[b, t].value or 0.0
                    both = round(m.both_on[t].value or 0)
                    if not cfg.USE_DOW_OPPORTUNITY_COSTS:
                        # DOW still deducted from Pmax (physical capacity reserved)
                        dow = float(value(m.DOW[t])) if b == _primary else float(value(m.DOW[t])) * (1 - both)
                        pmax = float(value(m.Pmax[b, t])) - dow + cfg.DUAL_BLOCK_BOOST * both
                    elif b == _primary:
                        pmax = (float(value(m.Pmax[b, t])) - float(value(m.DOW[t]))) + cfg.DUAL_BLOCK_BOOST * both
                    else:
                        pmax = float(value(m.Pmax[b, t])) - float(value(m.DOW[t])) * (1 - both) + cfg.DUAL_BLOCK_BOOST * both
                    room = max(pmax - p_val, 0.0)
                    if room > 0:
                        slope = float(value(m.coal_slope[b, t]))
                        grow_slots.append((b, t, room, slope))

            if not grow_slots:
                continue

            # Distribute headroom proportionally across slots
            total_marginal_coal = sum(room * slope for _, _, room, slope in grow_slots)
            if total_marginal_coal <= 0:
                continue
            scale = min(coal_headroom / total_marginal_coal, 1.0)
            for b, t, room, slope in grow_slots:
                m.P[b, t].value += room * scale

    # Set P_eff and run_costs so the full xx vector is consistent
    _primary = cfg.DOW_BLOCK
    for b in B_list:
        for t in T_list:
            on_val = round(m.on[b, t].value or 0)
            p_val = m.P[b, t].value or 0.0
            dow = float(value(m.DOW[t]))
            both_on_val = m.both_on[t].value or 0

            # P_eff mirrors peff_def_rule
            if cfg.USE_DOW_OPPORTUNITY_COSTS:
                if b == _primary:
                    p_eff = p_val + dow * on_val
                else:
                    p_eff = p_val + dow * (on_val - both_on_val)
            else:
                p_eff = p_val  # P_eff = P when DOW flag off
            # Cap P_eff to its variable bound; back-adjust P if needed
            peff_ub = m._pmax_eff[b]
            if p_eff > peff_ub:
                p_eff = peff_ub
                m.P[b, t].value = p_eff  # P = P_eff when capped
            m.P_eff[b, t].value = p_eff

            # P_eff_duo = P_eff × both_on (McCormick product)
            both_int = round(both_on_val)
            if m._has_duo:
                m.P_eff_duo[b, t].value = p_eff * both_int

            # run_costs = cost_slope * P_eff + cost_fixed * on
            #           + cost_slope_delta * P_eff_duo + cost_fixed_delta * both_on
            rc = (float(value(m.cost_slope[b, t])) * p_eff
                  + float(value(m.cost_fixed[b, t])) * on_val)
            if m._has_duo:
                rc += (float(value(m.cost_slope_delta[b, t])) * p_eff * both_int
                       + float(value(m.cost_fixed_delta[b, t])) * both_int)
            m.run_costs[b, t].value = rc

    # --- Final coal clamp: catch residual overshoots after P_eff finalisation ---
    if hasattr(m, "coal_monthly_limit") and hasattr(m, "_month_hours"):
        _primary = cfg.DOW_BLOCK
        for ym in sorted(m._month_hours):
            if ym not in {tuple(k) for k in m.coal_months}:
                continue
            limit = float(value(m.coal_limit_t[ym]))
            hours = m._month_hours[ym]

            coal_total = 0.0
            slot_coal = []
            for b in B_list:
                for t in hours:
                    on_val = round(m.on[b, t].value or 0)
                    if on_val == 1:
                        p_eff = m.P_eff[b, t].value or 0.0
                        both = round(m.both_on[t].value or 0)
                        c = (float(value(m.coal_slope[b, t])) * p_eff
                             + float(value(m.coal_fixed[b, t])))
                        if m._has_duo:
                            c += (float(value(m.coal_slope_delta[b, t])) * p_eff * both
                                  + float(value(m.coal_fixed_delta[b, t])) * both)
                        coal_total += c
                        p_val = m.P[b, t].value or 0.0
                        pmin = float(value(m.Pmin[b, t]))
                        both = round(m.both_on[t].value or 0)
                        p_floor = pmin + cfg.DUAL_BLOCK_BOOST * both
                        room = max(p_val - p_floor, 0.0)
                        if room > 0:
                            slot_coal.append((b, t, room, float(value(m.coal_slope[b, t]))))

            if coal_total <= limit + 1.0:
                continue

            overshoot = coal_total - limit
            total_reducible = sum(room * slope for _, _, room, slope in slot_coal)
            if total_reducible <= 0:
                print(f"    WARN: coal clamp {ym[0]}-{ym[1]:02d}: overshoot {overshoot:.0f}t "
                      f"but no slots to shrink")
                continue

            scale_back = min(overshoot / total_reducible, 1.0)
            for b, t, room, slope in slot_coal:
                dp = room * scale_back
                new_p = (m.P[b, t].value or 0.0) - dp
                m.P[b, t].value = new_p
                # Recompute P_eff
                on_val = round(m.on[b, t].value or 0)
                dow = float(value(m.DOW[t]))
                both_v = round(m.both_on[t].value or 0)
                if cfg.USE_DOW_OPPORTUNITY_COSTS:
                    if b == _primary:
                        p_eff = new_p + dow * on_val
                    else:
                        p_eff = new_p + dow * (on_val - both_v)
                else:
                    p_eff = new_p
                peff_ub = m._pmax_eff[b]
                if p_eff > peff_ub:
                    p_eff = peff_ub
                m.P_eff[b, t].value = p_eff
                if m._has_duo:
                    m.P_eff_duo[b, t].value = p_eff * both_v
                rc = (float(value(m.cost_slope[b, t])) * p_eff
                      + float(value(m.cost_fixed[b, t])) * on_val)
                if m._has_duo:
                    rc += (float(value(m.cost_slope_delta[b, t])) * p_eff * both_v
                           + float(value(m.cost_fixed_delta[b, t])) * both_v)
                m.run_costs[b, t].value = rc

            new_coal = sum(
                (float(value(m.coal_slope[b, t])) * (m.P_eff[b, t].value or 0.0)
                 + float(value(m.coal_fixed[b, t])) * round(m.on[b, t].value or 0)
                 + (float(value(m.coal_slope_delta[b, t])) * (m.P_eff_duo[b, t].value or 0.0)
                    + float(value(m.coal_fixed_delta[b, t])) * round(m.both_on[t].value or 0)
                    if m._has_duo else 0.0))
                for b in B_list for t in hours
            )
            print(f"    Coal clamp {ym[0]}-{ym[1]:02d}: "
                  f"reduced {coal_total:.0f}t → {new_coal:.0f}t "
                  f"(limit {limit:.0f}t, scale-back {scale_back:.1%})")

    # --- Comprehensive warm-start constraint violation check ---
    from pyomo.environ import Constraint as _Con
    viol_counts: dict = {}
    viol_examples: dict = {}
    for con_obj in m.component_objects(_Con, active=True):
        name = con_obj.name
        n_viol = 0
        worst_viol = 0.0
        worst_idx = None
        for idx in con_obj:
            c = con_obj[idx]
            try:
                body = value(c.body)
            except Exception:
                continue
            lb = value(c.lower) if c.lower is not None else None
            ub = value(c.upper) if c.upper is not None else None
            viol = 0.0
            if lb is not None and body < lb - 1e-6:
                viol = lb - body
            if ub is not None and body > ub + 1e-6:
                viol = max(viol, body - ub)
            if viol > 0:
                n_viol += 1
                if viol > worst_viol:
                    worst_viol = viol
                    worst_idx = idx
        if n_viol > 0:
            viol_counts[name] = n_viol
            viol_examples[name] = (worst_idx, worst_viol)

    if viol_counts:
        print(f"    DIAG: {len(viol_counts)} constraint blocks violated:")
        for name in sorted(viol_counts, key=lambda n: -viol_counts[n]):
            n = viol_counts[name]
            idx, wv = viol_examples[name]
            print(f"      {name}: {n} violations (worst={wv:.4f} at {idx})")
    else:
        print(f"    DIAG: ALL constraints satisfied")

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

    _use_dow = cfg.USE_DOW_OPPORTUNITY_COSTS

    def peff_def_rule(m, b, t):
        # When USE_DOW=True:  P_eff = P + DOW (DOW coal included, DOW deducted from Pmax)
        # When USE_DOW=False: P_eff = P       (DOW deducted from Pmax, lower running costs)
        if not _use_dow:
            return m.P_eff[b, t] == m.P[b, t]
        if b == _primary:
            dow_add = m.DOW[t] * m.on[b, t]
        else:
            dow_add = m.DOW[t] * (m.on[b, t] - m.both_on[t])
        return m.P_eff[b, t] == m.P[b, t] + dow_add

    m.peff_def = Constraint(m.B, m.T, rule=peff_def_rule)

    _duo = m._has_duo

    def cost_rule(m, b, t):
        expr = m.cost_slope[b, t] * m.P_eff[b, t] + m.cost_fixed[b, t] * m.on[b, t]
        if _duo:
            # DUO adjustment: Δslope × P_eff_duo + Δfixed × both_on
            expr += m.cost_slope_delta[b, t] * m.P_eff_duo[b, t]
            expr += m.cost_fixed_delta[b, t] * m.both_on[t]
        return m.run_costs[b, t] == expr

    m.cost_def = Constraint(m.B, m.T, rule=cost_rule)


def _add_duo_mccormick_constraints(m, _pmax_eff) -> None:
    """McCormick envelope: P_eff_duo = P_eff × both_on (linearised)."""

    def duo_ub1(m, b, t):
        return m.P_eff_duo[b, t] <= m.P_eff[b, t]

    def duo_ub2(m, b, t):
        return m.P_eff_duo[b, t] <= _pmax_eff[b] * m.both_on[t]

    def duo_lb(m, b, t):
        return m.P_eff_duo[b, t] >= m.P_eff[b, t] - _pmax_eff[b] * (1 - m.both_on[t])

    m.duo_ub1 = Constraint(m.B, m.T, rule=duo_ub1)
    m.duo_ub2 = Constraint(m.B, m.T, rule=duo_ub2)
    m.duo_lb = Constraint(m.B, m.T, rule=duo_lb)


def _add_power_bounds(m) -> None:
    _boost = cfg.DUAL_BLOCK_BOOST
    _primary = cfg.DOW_BLOCK

    def p_lower(m, b, t):
        # Pmin relaxed during startup ramp hours (in_ramp=1)
        return m.P[b, t] >= m.Pmin[b, t] * m.on[b, t] + _boost * m.both_on[t] - cfg.BIG_M * m.in_ramp[b, t]

    m.p_lower = Constraint(m.B, m.T, rule=p_lower)

    _use_dow = cfg.USE_DOW_OPPORTUNITY_COSTS

    def p_upper(m, b, t):
        # DOW is always deducted from Pmax (physical capacity reserved for DOW)
        # When USE_DOW=True:  P <= (Pmax-DOW)*on  →  P_eff=P+DOW <= Pmax
        # When USE_DOW=False: P <= (Pmax-DOW)*on  →  P_eff=P     <= Pmax-DOW
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

    Tier boundaries from the Starts tab:
      - hot   (5–10h off):  on[b, t-10] = 1  ↔  hot start
      - warm  (10–60h off): default (neither hot nor cold nor vcold)
      - cold  (60–100h off): no warm-zone checkpoint = 1, but some cold-zone = 1
      - vcold (≥100h off):  all checkpoints up to 100h = 0

    Note: very_hot (0–5h off) is structurally impossible with MIN_DOWN=6h.
    """
    # Warm-zone checkpoints: spaced by MIN_UP, covering 0–60h
    _warm_checks = list(range(cfg.MIN_UP, 61, cfg.MIN_UP))  # [8, 16, 24, 32, 40, 48, 56]
    _n_warm = len(_warm_checks)

    # Cold-zone checkpoints: covering 60–100h
    _cold_checks = list(range(64, 101, cfg.MIN_UP))  # [64, 72, 80, 88, 96]
    _n_cold = len(_cold_checks)

    # All checkpoints up to 100h (warm + cold zones)
    _all_checks = _warm_checks + _cold_checks
    _n_all = len(_all_checks)

    # --- Tier hierarchy (each ≤ startup) ---
    def hot_le_startup(m, b, t):
        return m.hot_start[b, t] <= m.startup[b, t]

    m.hot_le_startup = Constraint(m.B, m.T, rule=hot_le_startup)

    def cold_le_startup(m, b, t):
        return m.cold_start[b, t] <= m.startup[b, t]

    m.cold_le_startup = Constraint(m.B, m.T, rule=cold_le_startup)

    def vcold_le_startup(m, b, t):
        return m.vcold_start[b, t] <= m.startup[b, t]

    m.vcold_le_startup = Constraint(m.B, m.T, rule=vcold_le_startup)

    def tier_exclusivity(m, b, t):
        return m.hot_start[b, t] + m.cold_start[b, t] + m.vcold_start[b, t] <= m.startup[b, t]

    m.tier_exclusivity = Constraint(m.B, m.T, rule=tier_exclusivity)

    # --- Hot detection via lookback at t-10 ---
    def prevent_hot(m, b, t):
        """hot_start only if on[b, t-10] = 1 (block was on 10h ago)."""
        k = max(0, t - 10)
        return m.hot_start[b, t] <= m.on[b, k]

    m.prevent_hot = Constraint(m.B, m.T, rule=prevent_hot)

    def force_hot(m, b, t):
        """Force hot when startup, on[t-10]=1, and not cold/vcold."""
        k = max(0, t - 10)
        return m.hot_start[b, t] >= m.startup[b, t] + m.on[b, k] - 1 - m.cold_start[b, t] - m.vcold_start[b, t]

    m.force_hot = Constraint(m.B, m.T, rule=force_hot)

    # --- Cold detection: no warm-zone checkpoint=1, but some cold-zone=1 ---
    def prevent_cold_warm(m, b, t):
        """Prevent cold if any warm-zone checkpoint shows block was on recently."""
        lookbacks = [m.on[b, max(0, t - k)] for k in _warm_checks]
        return _n_warm * m.cold_start[b, t] + sum(lookbacks) <= _n_warm

    m.prevent_cold_warm = Constraint(m.B, m.T, rule=prevent_cold_warm)

    def prevent_cold_vcold(m, b, t):
        """Prevent cold if no cold-zone checkpoint shows block was recently on."""
        lookbacks = [m.on[b, max(0, t - k)] for k in _cold_checks]
        return _n_cold * m.cold_start[b, t] <= sum(lookbacks)

    m.prevent_cold_vcold = Constraint(m.B, m.T, rule=prevent_cold_vcold)

    # Force cold: for each cold-zone checkpoint, if it fires and no warm fires → cold
    m.force_cold = ConstraintList()
    for b in m.B:
        for t in m.T:
            warm_lbs = [m.on[b, max(0, t - k)] for k in _warm_checks]
            for k in _cold_checks:
                m.force_cold.add(
                    m.cold_start[b, t] >= m.startup[b, t] + m.on[b, max(0, t - k)] - sum(warm_lbs) - 1
                )

    # --- Vcold detection: all checkpoints up to 100h = 0 ---
    def prevent_vcold(m, b, t):
        """Prevent vcold if any checkpoint shows block was on recently."""
        lookbacks = [m.on[b, max(0, t - k)] for k in _all_checks]
        return _n_all * m.vcold_start[b, t] + sum(lookbacks) <= _n_all

    m.prevent_vcold = Constraint(m.B, m.T, rule=prevent_vcold)

    def force_vcold(m, b, t):
        """Force vcold when startup and all checkpoints up to 100h show off."""
        lookbacks = [m.on[b, max(0, t - k)] for k in _all_checks]
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
        cold_ramp = ramp_data[b]["cold"]
        vcold_ramp = ramp_data[b]["vcold"]

        for t in m.T:
            pmax_bt = float(value(m.Pmax[b, t]))

            # Worst-case Pmax for ramp LB cap: Pmax_orig - DOW
            # (block may serve DOW when starting alone)
            pmax_dow = pmax_bt - float(value(m.DOW[t]))

            for h in range(_H):
                # Startup hour is pinned by startup_requires_pmin_* constraints;
                # ramp envelopes start from h=1 to avoid contradictory bounds.
                if h == 0:
                    continue
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
                if h < len(cold_ramp):
                    cl_ramp.add(
                        m.P[b, t] <= cold_ramp[h] + _boost * m.both_on[t]
                        + cfg.BIG_M * (1 - m.cold_start[b, s])
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
                # Warm LB: active when startup=1 AND not hot/cold/vcold
                if h < len(warm_ramp):
                    lb_w = min(warm_ramp[h], pmax_dow)
                    cl_ramp_lb2.add(
                        m.P[b, t] >= lb_w
                        - cfg.BIG_M * (1 - m.startup[b, s]
                                       + m.hot_start[b, s]
                                       + m.cold_start[b, s]
                                       + m.vcold_start[b, s])
                    )
                # Cold LB: active when cold_start=1
                if h < len(cold_ramp):
                    lb_c = min(cold_ramp[h], pmax_dow)
                    cl_ramp_lb2.add(
                        m.P[b, t] >= lb_c
                        - cfg.BIG_M * (1 - m.cold_start[b, s])
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

    # Pre-compute per-(b,t) tight Big-M values (replaces global BIG_M=500)
    # UB needs M >= Pmax - Pmin; LB needs M >= Pmin + boost (on=0 case)
    _M_ub = {}  # tight M for upper-bound constraints
    _M_lb = {}  # tight M for lower-bound constraints
    for b in m.B:
        for t in m.T:
            pmax_bt = float(value(m.Pmax[b, t]))
            pmin_bt = float(value(m.Pmin[b, t]))
            _M_ub[(b, t)] = pmax_bt - pmin_bt + 1.0
            _M_lb[(b, t)] = pmin_bt + _boost + 1.0

    _M_ub_max = max(_M_ub.values()) if _M_ub else cfg.BIG_M
    _M_lb_max = max(_M_lb.values()) if _M_lb else cfg.BIG_M
    print(f"--- Startup/shutdown Big-M tightened: UB={_M_ub_max:.0f}, "
          f"LB={_M_lb_max:.0f} (was {cfg.BIG_M})")

    def shutdown_requires_pmin_lb(m, b, t):
        if t == 0:
            return Constraint.Skip
        return m.P[b, t - 1] >= m.Pmin[b, t - 1] + _boost * m.both_on[t - 1] - _M_lb[(b, t - 1)] * (1 - m.shutdown[b, t])

    def shutdown_requires_pmin_ub(m, b, t):
        if t == 0:
            return Constraint.Skip
        return m.P[b, t - 1] <= m.Pmin[b, t - 1] + _boost * m.both_on[t - 1] + _M_ub[(b, t - 1)] * (1 - m.shutdown[b, t])

    m.shutdown_requires_pmin_lb = Constraint(m.B, m.T, rule=shutdown_requires_pmin_lb)
    m.shutdown_requires_pmin_ub = Constraint(m.B, m.T, rule=shutdown_requires_pmin_ub)

    # Startup hour: P must equal Pmin (unit starts at minimum load)
    def startup_requires_pmin_lb(m, b, t):
        return m.P[b, t] >= m.Pmin[b, t] * m.on[b, t] + _boost * m.both_on[t] - _M_lb[(b, t)] * (1 - m.startup[b, t])

    def startup_requires_pmin_ub(m, b, t):
        return m.P[b, t] <= m.Pmin[b, t] * m.on[b, t] + _boost * m.both_on[t] + _M_ub[(b, t)] * (1 - m.startup[b, t])

    m.startup_requires_pmin_lb = Constraint(m.B, m.T, rule=startup_requires_pmin_lb)
    m.startup_requires_pmin_ub = Constraint(m.B, m.T, rule=startup_requires_pmin_ub)


# ----------------------------------------------------------------
#  Objective
# ----------------------------------------------------------------


def _add_objective(m, hot_cost, warm_cost, cold_cost, vcold_cost) -> None:
    # Pre-compute incremental costs per block (warm is baseline)
    _hot_delta = {b: hot_cost[b] - warm_cost[b] for b in cfg.BLOCKS}
    _cold_delta = {b: cold_cost[b] - warm_cost[b] for b in cfg.BLOCKS}
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
                    + _cold_delta[b] * m.cold_start[b, t]
                    + _vcold_delta[b] * m.vcold_start[b, t]
                )

                expr += (
                    profit_spot
                    - run_costs
                    - start_cost
                )

            # OFF_costs when BOTH blocks offline.
            # In DOW mode, 130 MW is charged only on grid fee (not market price).
            OFF_costs = m.plant_off[t] * cfg.OWN_CONSUMPTION * (
                m.price[t] + m.gridfee[t]
            )
            if cfg.USE_DOW_OPPORTUNITY_COSTS:
                OFF_costs += m.plant_off[t] * cfg.DOW_OFF_CONSUMPTION * m.gridfee[t]
                OFF_costs -= m.plant_off[t] * cfg.DOW_OFF_CONSUMPTION * cfg.DOW_OFF_COMPENSATION
            else:
                OFF_costs += m.plant_off[t] * cfg.OFFLINE_FIXED_PENALTY_NO_DOW
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
