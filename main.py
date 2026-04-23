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
          f"{n_startups_total} startups, window=±{window}h")
    print(f"      Fixed on[b,t] at {n_fixed_on} / {2 * T_len} slots "
          f"({2 * T_len - n_fixed_on} remain free)")
    print(f"      Fixed startup=0 at {n_fixed_startup} slots")
    print(f"      Fixed tier vars=0 at {n_fixed_tier} slots")


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

            # --- Iterative re-linearization ---
            for _relin_iter in range(cfg.RELINEARIZE_ITERS):
                print(f"\n--- Re-linearization pass {_relin_iter + 1}/{cfg.RELINEARIZE_ITERS}")
                _pnom = {(b, t): float(value(m.P_eff[b, t]))
                         for b in m.B for t in m.T}
                m_prev = m
                m = build_model(df, cost_meta, pnom_hint=_pnom)
                warm_start_heuristic(m)
                _copy_integer_hint(m_prev, m)
                _resync_in_ramp(m)
                _fix_tiers_from_hint(m, m_prev, window=24)
                solver = create_solver()
                results = solve_model(solver, m, tee=True)
                del m_prev

        else:
            m = build_model(df, cost_meta)
            warm_start_heuristic(m)
            solver = create_solver()
            results = solve_model(solver, m, tee=True)

    term = check_termination(results, skip_solve)

    # Coal shadow prices (LP re-solve with fixed binaries)
    coal_shadow_prices = {}
    merchant_shadow_prices = {}
    if not skip_solve and m is not None and cfg.USE_COAL_CONSTRAINS:
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
    #  Step 6 – Save cache
    # ----------------------------------------------------------------
    if not skip_solve:
        save_cache(df, obj_val)

    # ----------------------------------------------------------------
    #  Step 7 – Write Excel report
    # ----------------------------------------------------------------
    write_excel(df, cost_meta, cfg.OUTPUT_FILE,
                coal_shadow_prices=coal_shadow_prices,
                merchant_shadow_prices=merchant_shadow_prices)


if __name__ == "__main__":
    main()
