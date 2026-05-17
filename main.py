"""
Schkopau MTP – Power plant dispatch optimisation.

This is the top-level entry point.  It orchestrates:
  1. Data loading and preparation
  2. Pyomo model construction
  3. Solving (with optional caching)
  4. Result extraction and PnL computation
  5. Excel reporting

Usage
-----
    python main.py
"""

from __future__ import annotations

import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import mosek
from pyomo.environ import Var, value

from schkopau_mtp import config as cfg
from schkopau_mtp.data_loader import load_and_prepare
from schkopau_mtp.model_builder import build_model, warm_start_heuristic
from schkopau_mtp.reporting import write_excel
from schkopau_mtp.results import extract_results, run_audit
from schkopau_mtp.solver import (
    check_termination,
    create_solver,
    extract_coal_shadow_prices,
    save_cache,
    solve_model,
    try_load_cache,
)


def _copy_integer_hint(src, dst) -> None:
    """Copy integer/binary variable values from one model instance to another.

    Variables that are *fixed* in the source are skipped — they are model
    constants (e.g. ``in_ramp`` fixed to 0 in simple-ramp models), not
    decision hints, and should not override values already set by the
    warm-start heuristic in the destination model.
    """
    for v_src in src.component_objects(Var, active=True):
        if not hasattr(dst, v_src.name):
            continue
        v_dst = getattr(dst, v_src.name)
        for idx in v_src:
            if idx not in v_dst:
                continue
            vd_src = v_src[idx]
            # Skip model constants – fixed in source means the value is
            # structurally determined (e.g. in_ramp=0 in simple-ramp mode),
            # not a meaningful MIP decision to transfer.
            if getattr(vd_src, "fixed", False):
                continue
            is_integer_like = bool(
                getattr(vd_src, "is_binary", lambda: False)()
                or getattr(vd_src, "is_integer", lambda: False)()
            )
            if not is_integer_like:
                continue

            vv = vd_src.value
            if vv is None:
                continue
            if not v_dst[idx].fixed:
                v_dst[idx].value = round(float(vv))


def _resync_in_ramp(m) -> None:
    """Re-derive ``in_ramp`` hint values from the injected startup schedule.

    After ``_copy_integer_hint`` from a simple-ramp model the ``in_ramp``
    variables in the full-ramp destination either:
    * were set to 0 by the warm-start heuristic (for heuristic startup windows
      that don't match the injected schedule), or
    * were already 0 because the source model had them fixed to 0.

    Either way the hint is inconsistent: ``startup[b,t]=1`` but
    ``in_ramp[b,t..t+H-1]=0`` makes MOSEK's CONSTRUCT_SOL LP infeasible at
    ramp hours (``p_lower`` forces P ≥ Pmin while ``startup_ramp_ub`` forces
    P ≤ ramp_level < Pmin).  This function resets ``in_ramp`` so it is
    consistent with the current startup schedule, giving MOSEK a fully
    feasible hint and a high-quality initial solution.
    """
    if not hasattr(m, "in_ramp"):
        return
    B_list = sorted(m.B)
    T_list = sorted(m.T)
    T_len = len(T_list)
    _H = cfg.MAX_RAMP_HOURS

    # Clear first
    for b in B_list:
        for t in T_list:
            if not m.in_ramp[b, t].fixed:
                m.in_ramp[b, t].value = 0

    # Set ramp windows from startup events
    n_startups = 0
    for b in B_list:
        for t in T_list:
            su = round(m.startup[b, t].value or 0)
            if su == 1:
                n_startups += 1
                for h in range(_H):
                    if t + h < T_len and not m.in_ramp[b, t + h].fixed:
                        m.in_ramp[b, t + h].value = 1
    print(f"    _resync_in_ramp: set {n_startups} startup ramp window(s) "
          f"(MAX_RAMP_HOURS={_H})")


def _fix_tiers_from_hint(m, m_hint, *, window: int = 24) -> None:
    """Fix on/startup/tier binaries from Stage 1, leaving ±window around transitions.

    Stage 1 tells us where blocks turn on/off.  This function fixes:
    - on[b,t] to the Stage 1 value at hours far from any on↔off transition
    - startup[b,t] = 0 at hours far from any Stage 1 startup
    - hot_start/cold_start/vcold_start = 0 at the same hours

    Only hours within ±window of a transition boundary remain free.
    """
    B_list = sorted(m.B)
    T_list = sorted(m.T)
    T_len = len(T_list)

    # Collect on-values and find transition hours per block
    on_vals: dict[str, list[int]] = {}
    transition_hours: dict[str, set[int]] = {b: set() for b in B_list}
    startup_hours: dict[str, set[int]] = {b: set() for b in B_list}

    for b in B_list:
        vals = []
        for t in T_list:
            v = getattr(m_hint.on[b, t], "value", None)
            vals.append(round(float(v)) if v is not None else 0)
        on_vals[b] = vals

        # Find transitions: on→off or off→on
        for t in range(1, T_len):
            if vals[t] != vals[t - 1]:
                transition_hours[b].add(t)
                transition_hours[b].add(t - 1)

        # Collect startups
        for t in T_list:
            sv = getattr(m_hint.startup[b, t], "value", None)
            if sv is not None and round(float(sv)) == 1:
                startup_hours[b].add(t)

    n_fixed_on = 0
    n_fixed_startup = 0
    n_fixed_tier = 0

    for b in B_list:
        tr_times = transition_hours[b]
        su_times = startup_hours[b]
        vals = on_vals[b]

        for t in T_list:
            # Check if t is within ±window of any transition
            near_transition = any(abs(t - tr) <= window for tr in tr_times)

            if not near_transition:
                # Fix on[b,t] to Stage 1 value
                if not m.on[b, t].fixed:
                    m.on[b, t].fix(vals[t])
                    n_fixed_on += 1

            # Fix startup/tiers: use startup proximity (narrower criterion)
            near_startup = any(abs(t - s) <= window for s in su_times)
            if not near_startup:
                if not m.startup[b, t].fixed:
                    m.startup[b, t].fix(0)
                    n_fixed_startup += 1
                for tier_name in ("hot_start", "cold_start", "vcold_start"):
                    tier_var = getattr(m, tier_name, None)
                    if tier_var is not None and not tier_var[b, t].fixed:
                        tier_var[b, t].fix(0)
                        n_fixed_tier += 1

    n_transitions = sum(len(v) for v in transition_hours.values())
    n_startups_total = sum(len(v) for v in startup_hours.values())
    print(f"    _fix_tiers_from_hint: {n_transitions} transition points, "
          f"{n_startups_total} startups, window=+/-{window}h")
    print(f"      Fixed on[b,t] at {n_fixed_on} / {2 * T_len} slots "
          f"({2 * T_len - n_fixed_on} remain free)")
    print(f"      Fixed startup=0 at {n_fixed_startup} slots")
    print(f"      Fixed tier vars=0 at {n_fixed_tier} slots")


def _duo_cost_sum(m) -> float:
    """Sum of DUO cost adjustments across all blocks and hours (from solved model)."""
    if not getattr(m, "_has_duo", False):
        return 0.0
    total = 0.0
    for b in m.B:
        for t in m.T:
            adj = float(value(m.duo_cost_adj[b, t]))
            bo = value(m.both_on[t])
            bo = float(bo) if bo is not None else 0.0
            total += adj * bo
    return total


def _duo_coal_sum(m) -> float:
    """Sum of DUO coal adjustments across all blocks and hours (from solved model)."""
    if not getattr(m, "duo_coal_adj", None):
        return 0.0
    total = 0.0
    for b in m.B:
        for t in m.T:
            adj = float(value(m.duo_coal_adj[b, t]))
            bo = value(m.both_on[t])
            bo = float(bo) if bo is not None else 0.0
            total += adj * bo
    return total


def _run_coal_sensitivity(df, cost_meta, m_baseline,
                          lp_dual_shadows: dict | None = None) -> dict:
    """Run LP sensitivity with perturbed coal limits (integers fixed from baseline).

    Fixes all integer/binary variables at the baseline MIP solution,
    perturbs coal_limit_t by +delta tonnes per month, and re-solves as LP.
    Compares per-month PnL against the baseline to derive the marginal
    value of coal (EUR / t).

    This avoids MIP noise entirely — only the continuous dispatch shifts.

    Parameters
    ----------
    lp_dual_shadows : dict, optional
        {(year, month): EUR/t} LP dual shadow prices.  Used to warn when
        a sensitivity delta returns zero but the LP dual is positive
        (meaning the delta is too large for continuous dispatch to absorb).

    Returns
    -------
    dict
        ``{delta_kt: {(year, month): pnl_per_tonne}}``
    """
    from pyomo.core import Var
    from pyomo.environ import Binary, NonNegativeReals, SolverFactory

    if not cfg.USE_COAL_CONSTRAINS or not cfg.COAL_SENSITIVITY_DELTAS:
        return {}

    if not hasattr(m_baseline, "coal_monthly_limit"):
        return {}

    coal_limits_orig = cost_meta.get("coal_limits", {})
    if not coal_limits_orig:
        return {}

    # Save original coal limit param values
    orig_limit_t = {ym: float(value(m_baseline.coal_limit_t[ym]))
                    for ym in m_baseline.coal_months}

    # Fix all integer/binary variables and relax domains → pure LP
    print("--- Coal sensitivity: fixing integers from baseline for LP re-solves ...")
    fixed_vars: list = []
    for v in m_baseline.component_objects(Var, active=True):
        for idx in v:
            vd = v[idx]
            if vd.is_integer() or vd.is_binary():
                orig_domain = vd.domain
                if not vd.fixed:
                    vd.fix(round(value(vd)))
                    fixed_vars.append((v, idx, orig_domain, True))
                else:
                    fixed_vars.append((v, idx, orig_domain, False))
                vd.domain = NonNegativeReals

    # ── Forward extension: unfix `on` at hours right after each shutdown ──
    # This allows the LP to prolong running blocks when extra coal is available.
    # startup stays fixed at 0 (correct: extending ≠ new start).
    extend_K = getattr(cfg, 'COAL_SENSITIVITY_EXTEND_HOURS', 0)
    unfixed_on_count = 0
    if extend_K > 0:
        T_set = set(m_baseline.T)
        for b in m_baseline.B:
            T_list = sorted(m_baseline.T)
            for i in range(len(T_list) - 1):
                t_cur = T_list[i]
                t_nxt = T_list[i + 1]
                on_cur = round(value(m_baseline.on[b, t_cur]))
                on_nxt = round(value(m_baseline.on[b, t_nxt]))
                if on_cur == 1 and on_nxt == 0:
                    # Shutdown boundary → allow forward extension
                    for k in range(1, extend_K + 1):
                        t_ext = t_cur + k
                        if t_ext not in T_set:
                            break
                        if round(value(m_baseline.on[b, t_ext])) == 1:
                            break  # hit another ON block
                        m_baseline.on[b, t_ext].unfix()
                        m_baseline.on[b, t_ext].domain = Binary
                        m_baseline.on[b, t_ext].setlb(0)
                        m_baseline.on[b, t_ext].setub(1)
                        unfixed_on_count += 1
        print(f"--- Block extension enabled: {unfixed_on_count} on-variables "
              f"unfixed (up to {extend_K}h past each shutdown)")

    lp_solver = SolverFactory("mosek")
    sensitivity: dict = {}

    # ── Re-solve baseline MIP with unfixed on-vars at delta=0 ──
    # This ensures the baseline accounts for the same degrees of freedom
    # (unfixed extension on-vars) as the perturbed solves, so the
    # sensitivity measures only the value of extra coal, not re-dispatch gains.
    if unfixed_on_count > 0:
        lp_solver.options["MSK_IPAR_MIO_CONSTRUCT_SOL"] = "MSK_ON"
        lp_solver.options["MSK_DPAR_MIO_TOL_REL_GAP"] = "0.0001"
        lp_solver.options["MSK_DPAR_MIO_MAX_TIME"] = "300"
        print("--- Re-solving baseline MIP with extension on-vars unfixed (delta=0) ...")
        lp_solver.solve(m_baseline, tee=False)

        # Fix extension on-vars at delta=0 solution → pure LP for all deltas.
        # This eliminates MIP noise (non-monotonic shadow prices) while keeping
        # the delta=0 re-dispatch (extensions turned on/off optimally at current
        # coal limits).
        ext_newly_on = 0
        for b in m_baseline.B:
            for t in m_baseline.T:
                vd = m_baseline.on[b, t]
                if not vd.fixed:
                    val = round(value(vd))
                    if val == 1:
                        ext_newly_on += 1
                    vd.fix(val)
                    vd.domain = NonNegativeReals
        print(f"    Baseline re-solved ({ext_newly_on} extension hours turned on)")
        print(f"    Extension on-vars fixed at delta=0 -> pure LP for sensitivity")

        # ── Correct startup/tier variables to match the updated on-pattern ──
        # Extensions may have (a) bridged an entire OFF gap so the subsequent
        # startup is now spurious (block never went offline), or (b) shortened
        # a gap across a tier boundary so a cheaper tier now applies.
        #
        # in_ramp is continuous and linked to startup via in_ramp_lb/ub
        # constraints → it self-corrects during the LP re-solve once startup
        # is fixed to the right value.
        # shutdown is continuous and self-corrects via the su_sd_balance
        # constraint: startup - shutdown = on[t] - on[t-1].
        # Only startup (Binary) and hot/cold/vcold_start (Binary) need
        # explicit correction here.
        T_sorted = sorted(m_baseline.T)
        corrections_log: list[str] = []
        for b in m_baseline.B:
            # off_count = consecutive OFF hours up to and including the
            # current hour.  Use 200 as a sentinel when the pre-horizon
            # state is unknown (same convention as warm_start_heuristic).
            off_count = 0 if cfg.INITIAL_ON.get(b, 0) else 200
            for i, t in enumerate(T_sorted):
                on_prev = (cfg.INITIAL_ON.get(b, 0)
                           if i == 0
                           else round(value(m_baseline.on[b, T_sorted[i - 1]])))
                on_cur = round(value(m_baseline.on[b, t]))
                true_startup = (on_cur == 1 and on_prev == 0)

                if on_cur == 0:
                    off_count += 1
                else:
                    off_at_start = off_count
                    off_count = 0

                su_val = round(value(m_baseline.startup[b, t]))

                if su_val == 1 and not true_startup:
                    # Extension bridged the gap: block never went offline.
                    # Clear the spurious startup and all tier flags.
                    # Effect: startup_requires_pmin Big-M deactivates (P freed),
                    # in_ramp_ub forces in_ramp → 0 (ramp envelope deactivates),
                    # shutdown self-corrects to 0 via su_sd_balance.
                    m_baseline.startup[b, t].fix(0)
                    m_baseline.hot_start[b, t].fix(0)
                    m_baseline.cold_start[b, t].fix(0)
                    m_baseline.vcold_start[b, t].fix(0)
                    corrections_log.append(
                        f"      {b} t={t}: startup cleared (gap bridged by extension)")

                elif true_startup:
                    # Real startup — check whether the extension shortened the
                    # gap enough to cross a tier boundary.
                    cor_hot   = 1 if off_at_start < 10 else 0
                    cor_cold  = 1 if 60 <= off_at_start < 100 else 0
                    cor_vcold = 1 if off_at_start >= 100 else 0

                    old_hot   = round(value(m_baseline.hot_start[b, t]))
                    old_cold  = round(value(m_baseline.cold_start[b, t]))
                    old_vcold = round(value(m_baseline.vcold_start[b, t]))

                    if (cor_hot != old_hot or cor_cold != old_cold
                            or cor_vcold != old_vcold):
                        m_baseline.hot_start[b, t].fix(cor_hot)
                        m_baseline.cold_start[b, t].fix(cor_cold)
                        m_baseline.vcold_start[b, t].fix(cor_vcold)

                        def _tier(h, c, v):
                            return 'hot' if h else ('cold' if c else ('vcold' if v else 'warm'))

                        corrections_log.append(
                            f"      {b} t={t}: tier "
                            f"{_tier(old_hot, old_cold, old_vcold)}"
                            f"→{_tier(cor_hot, cor_cold, cor_vcold)}"
                            f" (off={off_at_start}h)")

        if corrections_log:
            print(f"    Startup/tier corrections applied ({len(corrections_log)}):")
            for msg in corrections_log:
                print(msg)
        else:
            print("    No startup/tier corrections needed.")

        # Re-solve as pure LP to get clean baseline (MIP continuous solution
        # may differ from LP optimum due to B&B tolerances).
        # Attach dual Suffix so we get LP duals from this same solve.
        from pyomo.environ import Suffix
        if not hasattr(m_baseline, 'dual'):
            m_baseline.dual = Suffix(direction=Suffix.IMPORT)
        lp_solver.solve(m_baseline, tee=False)
        print("    LP baseline re-solved after fixing extensions")

        print("\n    Sensitivity baseline LP duals (reference for finite-difference):")
        for ym in sorted(coal_limits_orig):
            if ym not in m_baseline.coal_months:
                continue
            dual_val = m_baseline.dual.get(m_baseline.coal_monthly_limit[ym], 0.0)
            print(f"      {ym[0]}-{ym[1]:02d}: {dual_val:+.2f} EUR/t")
    if hasattr(m_baseline, 'dual'):
        m_baseline.del_component(m_baseline.dual)

    # Capture baseline P values for diagnostic comparison
    base_P = {}
    for b in m_baseline.B:
        for t in m_baseline.T:
            base_P[b, t] = float(value(m_baseline.P[b, t]))

    # Baseline objective for clean shadow price computation
    base_obj = float(value(m_baseline.obj))

    for delta_kt in cfg.COAL_SENSITIVITY_DELTAS:
        delta_t = delta_kt * 1000
        print(f"\n--- Coal sensitivity: +{delta_t:.0f}t (+{delta_kt} kt) per month ---")

        pnl_per_t: dict = {}

        for ym in sorted(coal_limits_orig):
            if ym not in m_baseline.coal_months:
                continue
            # Perturb ONLY this month's coal limit
            m_baseline.coal_limit_t[ym] = orig_limit_t[ym] + delta_t

            # Solve LP (integers fixed, only continuous vars)
            lp_solver.solve(m_baseline, tee=False)

            # Shadow price = total objective improvement / delta
            sens_obj = float(value(m_baseline.obj))
            dpnl = sens_obj - base_obj
            pnl_per_t[ym] = dpnl / delta_t if delta_t > 0 else 0.0

            # Count dispatch changes in the perturbed month
            month_hours = m_baseline._month_hours[ym]
            n_changed = 0
            total_dp = 0.0
            changed_details: list[tuple] = []
            for b in m_baseline.B:
                for t in month_hours:
                    dp = float(value(m_baseline.P[b, t])) - base_P[b, t]
                    if abs(dp) > 0.01:
                        n_changed += 1
                        total_dp += dp
                        changed_details.append((b, t, base_P[b, t], float(value(m_baseline.P[b, t])), dp))

            print(f"    {ym[0]}-{ym[1]:02d}: {pnl_per_t[ym]:+.2f} EUR/t  "
                  f"(dPnL={dpnl:+,.0f} EUR, {n_changed} hrs, "
                  f"net {total_dp:+.1f} MWh)")
            if changed_details:
                for b, t, p_base, p_new, dp in sorted(changed_details, key=lambda x: x[1]):
                    dt = df.iloc[t]['Date']
                    print(f"        {b} t={t}  {dt}  P: {p_base:.1f} -> {p_new:.1f}  (d{dp:+.1f} MW)")

            # Restore this month's coal limit
            m_baseline.coal_limit_t[ym] = orig_limit_t[ym]

        sensitivity[delta_kt] = pnl_per_t

    # Restore original coal limits
    for ym in m_baseline.coal_months:
        m_baseline.coal_limit_t[ym] = orig_limit_t[ym]

    # Restore integer domains and unfix
    for v, idx, orig_domain, was_unfixed in fixed_vars:
        v[idx].domain = orig_domain
        if was_unfixed:
            v[idx].unfix()

    return sensitivity


def main() -> None:
    # ----------------------------------------------------------------
    #  MOSEK licence check
    # ----------------------------------------------------------------
    with mosek.Env() as env:
        print("MOSEK version:", env.getversion())

    print("CWD:", os.getcwd())

    # ----------------------------------------------------------------
    #  Step 1 – Load & prepare data
    # ----------------------------------------------------------------
    df, cost_meta = load_and_prepare()
    print(f"Input file: {cfg.INPUT_FILE}")
    print(f"Data loaded: {len(df)} hours, "
          f"{df['Date'].min()} -> {df['Date'].max()}")

    # ----------------------------------------------------------------
    #  Step 2 – Check solver cache
    # ----------------------------------------------------------------
    cached_df, cached_meta = try_load_cache()
    skip_solve = cached_df is not None

    if skip_solve:
        df = cached_df
        cfg.SKIP_SOLVE_AND_EXTRACT = True

    # ----------------------------------------------------------------
    #  Step 3 – Build & solve the model
    # ----------------------------------------------------------------
    m = None
    results = None

    if not skip_solve:
        # Preferred staged flow: first solve with simple startup ramp,
        # then solve the full-ramp model using transferred integer hints.
        if cfg.SOLVE_MODE == "staged_ramp":
            print("--- Staged solve: Stage 1 (SIMPLE startup ramp)")
            cfg.USE_SIMPLE_STARTUP_RAMP = True

            m1 = build_model(df, cost_meta)
            warm_start_heuristic(m1)
            # Stage 1 only needs a good hint — use wider gap + shorter time
            _orig_gap = cfg.MOSEK_MIO_TOL_REL_GAP
            _orig_time = cfg.MOSEK_MIO_MAX_TIME
            cfg.MOSEK_MIO_TOL_REL_GAP = "0.12"
            cfg.MOSEK_MIO_MAX_TIME = "600"
            solver1 = create_solver()
            cfg.MOSEK_MIO_TOL_REL_GAP = _orig_gap
            cfg.MOSEK_MIO_MAX_TIME = _orig_time
            res1 = solve_model(solver1, m1, tee=True)
            check_termination(res1, skip_solve=False)

            print("--- Stage 1 complete")
            df_stage1 = extract_results(df.copy(), m1, cost_meta, skip_solve=False, cached_meta=None)
            run_audit(df_stage1, m1, skip_solve=False, cached_meta=None, cost_meta=cost_meta)

            print("--- Staged solve: Stage 2 (FULL startup ramp) with integer hint transfer")
            cfg.USE_SIMPLE_STARTUP_RAMP = False

            # Re-linearise DUO at Stage 1 P_eff for tighter approximation
            _pnom = {(b, t): float(value(m1.P_eff[b, t]))
                     for b in m1.B for t in m1.T}

            m = build_model(df, cost_meta, pnom_hint=_pnom)
            warm_start_heuristic(m)
            _copy_integer_hint(m1, m)
            _resync_in_ramp(m)  # Re-derive in_ramp from injected startup schedule
            _fix_tiers_from_hint(m, m1, window=24)
            solver = create_solver()
            results = solve_model(solver, m, tee=True)

            # --- Iterative re-linearization with convergence check ---
            # Keep Stage-1 model as the *fixed* reference for tier/on fixings.
            # Re-linearization only updates DUO coefficients (pnom_hint) and
            # warm-starts from the latest solve — the search-space skeleton
            # (which on/startup/tier vars are fixed) stays constant so the
            # warm-start remains feasible for MOSEK.
            _m_fix_ref = m1  # reference model for _fix_tiers_from_hint
            _prev_obj = float(value(m.obj))
            _prev_duo_cost = _duo_cost_sum(m)
            _prev_duo_coal = _duo_coal_sum(m)
            for _relin_iter in range(cfg.RELINEARIZE_MAX_ITERS):
                print(f"\n--- Re-linearization pass {_relin_iter + 1}/{cfg.RELINEARIZE_MAX_ITERS}")
                _pnom = {(b, t): float(value(m.P_eff[b, t]))
                         for b in m.B for t in m.T}
                m_prev = m
                m = build_model(df, cost_meta, pnom_hint=_pnom)
                warm_start_heuristic(m)
                _copy_integer_hint(m_prev, m)
                _resync_in_ramp(m)
                _fix_tiers_from_hint(m, _m_fix_ref, window=24)
                solver = create_solver()
                results = solve_model(solver, m, tee=True)
                del m_prev

                # --- Convergence check ---
                _cur_obj = float(value(m.obj))
                _cur_duo_cost = _duo_cost_sum(m)
                _cur_duo_coal = _duo_coal_sum(m)
                _d_obj = abs(_cur_obj - _prev_obj)
                _d_duo = abs(_cur_duo_cost - _prev_duo_cost)
                _d_coal = abs(_cur_duo_coal - _prev_duo_coal)
                print(f"    |d obj| = {_d_obj:,.0f} EUR  "
                      f"(tol {cfg.RELIN_OBJ_TOL:,.0f})")
                print(f"    |d DUO cost| = {_d_duo:,.0f} EUR  "
                      f"(tol {cfg.RELIN_DUO_COST_TOL:,.0f})")
                print(f"    |d DUO coal| = {_d_coal:,.1f} t")
                if _d_obj < cfg.RELIN_OBJ_TOL and _d_duo < cfg.RELIN_DUO_COST_TOL:
                    print(f"    Converged after {_relin_iter + 1} pass(es).")
                    break
                _prev_obj = _cur_obj
                _prev_duo_cost = _cur_duo_cost
                _prev_duo_coal = _cur_duo_coal

        else:
            m = build_model(df, cost_meta)
            warm_start_heuristic(m)
            solver = create_solver()
            results = solve_model(solver, m, tee=True)

    term = check_termination(results, skip_solve)

    # Coal shadow prices (LP re-solve with fixed binaries)
    coal_shadow_prices = {}
    merchant_shadow_prices = {}

    # When loading from cache, rebuild model and inject cached solution
    # so we can still compute shadow prices and sensitivity.
    if skip_solve and m is None and cfg.USE_COAL_CONSTRAINS and cfg.COAL_SENSITIVITY_DELTAS:
        print("--- Rebuilding model from cache for shadow prices / sensitivity ...")
        _pnom_cache = {}
        for b in cfg.BLOCKS:
            p_col = f"P_eff_{b}"
            if p_col in df.columns:
                for i, t in enumerate(range(len(df))):
                    _pnom_cache[(b, t)] = float(df.iloc[i][p_col])
        m = build_model(df, cost_meta, pnom_hint=_pnom_cache if _pnom_cache else None)

        # Inject cached on values and recompute ALL dependent binaries
        # following the same logic as model_builder warm-start.
        bA, bB = cfg.BLOCKS
        T_list = list(m.T)

        for b in cfg.BLOCKS:
            on_col = f"on_model_{b}"
            if on_col not in df.columns:
                continue

            off_count = 200  # assume cold start initially
            for t in T_list:
                on_val = int(round(df.iloc[t][on_col]))
                if not m.on[b, t].fixed:
                    m.on[b, t].value = on_val

                # startup / shutdown from on-pattern
                prev_on = int(round(df.iloc[t - 1][on_col])) if t > 0 else cfg.INITIAL_ON[b]
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

                # Tier binaries
                if su == 1:
                    m.hot_start[b, t].value = 1 if off_count_at_start < 10 else 0
                    m.cold_start[b, t].value = 1 if 60 <= off_count_at_start < 100 else 0
                    m.vcold_start[b, t].value = 1 if off_count_at_start >= 100 else 0
                else:
                    m.hot_start[b, t].value = 0
                    m.cold_start[b, t].value = 0
                    m.vcold_start[b, t].value = 0

                # P / P_eff — clip to bounds to avoid float-precision warnings
                p_col = f"P_{b}"
                peff_col = f"P_eff_{b}"
                if p_col in df.columns:
                    p_val = float(df.iloc[t][p_col])
                    p_ub = m.P[b, t].ub
                    if p_ub is not None:
                        p_val = min(p_val, p_ub)
                    m.P[b, t].value = max(0.0, p_val)
                if peff_col in df.columns:
                    m.P_eff[b, t].value = float(df.iloc[t][peff_col])

        # Set both_on / plant_off from on values
        for t in T_list:
            onA = int(round(value(m.on[bA, t])))
            onB = int(round(value(m.on[bB, t])))
            m.both_on[t].value = 1 if (onA == 1 and onB == 1) else 0
            m.plant_off[t].value = 1 if (onA == 0 and onB == 0) else 0
        print("--- Model rebuilt and solution injected from cache")

    if m is not None and cfg.USE_COAL_CONSTRAINS and hasattr(m, "coal_monthly_limit"):
        coal_shadow_prices, merchant_shadow_prices = extract_coal_shadow_prices(m)

    # Objective value
    if skip_solve:
        obj_val = (
            float(cached_meta.get("objective_value"))
            if isinstance(cached_meta, dict) and cached_meta.get("objective_value") is not None
            else None
        )
    else:
        obj_val = float(value(m.obj))

    print("\n=== Objective check ===")
    print("value(m.obj):", obj_val)

    # ----------------------------------------------------------------
    #  Step 4 – Extract results & PnL
    # ----------------------------------------------------------------
    df = extract_results(
        df, m, cost_meta,
        skip_solve=skip_solve,
        cached_meta=cached_meta,
    )

    # ----------------------------------------------------------------
    #  Step 5 – Audit
    # ----------------------------------------------------------------
    run_audit(df, m, skip_solve=skip_solve, cached_meta=cached_meta, cost_meta=cost_meta)

    # ----------------------------------------------------------------
    #  Step 5b – Save cache (before sensitivity so solution is safe)
    # ----------------------------------------------------------------
    if not skip_solve:
        save_cache(df, obj_val)

    # Coal sensitivity (LP re-solves with perturbed limits)
    coal_sensitivity = {}
    if m is not None and cfg.USE_COAL_CONSTRAINS:
        coal_sensitivity = _run_coal_sensitivity(df, cost_meta, m, coal_shadow_prices)

    # ----------------------------------------------------------------
    #  Step 7 – Write Excel report
    # ----------------------------------------------------------------
    write_excel(df, cost_meta, cfg.OUTPUT_FILE,
                coal_shadow_prices=coal_shadow_prices,
                merchant_shadow_prices=merchant_shadow_prices,
                coal_sensitivity=coal_sensitivity)


if __name__ == "__main__":
    main()
