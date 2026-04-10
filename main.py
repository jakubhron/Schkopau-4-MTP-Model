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


def _append_output_suffix(path: str, suffix: str) -> str:
    """Return path with an extra suffix inserted before extension."""
    root, ext = os.path.splitext(path)
    return f"{root}_{suffix}{ext}"


def _copy_integer_hint(src, dst) -> None:
    """Copy integer/binary variable values from one model instance to another."""
    for v_src in src.component_objects(Var, active=True):
        if not hasattr(dst, v_src.name):
            continue
        v_dst = getattr(dst, v_src.name)
        for idx in v_src:
            if idx not in v_dst:
                continue
            vd_src = v_src[idx]
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
        if cfg.USE_STAGED_RAMP_WARMSTART and not cfg.USE_SIMPLE_STARTUP_RAMP:
            print("--- Staged solve: Stage 1 (SIMPLE startup ramp)")
            _orig_simple = cfg.USE_SIMPLE_STARTUP_RAMP
            cfg.USE_SIMPLE_STARTUP_RAMP = True

            m1 = build_model(df, cost_meta)
            warm_start_heuristic(m1)
            solver1 = create_solver()
            res1 = solve_model(solver1, m1, tee=True)
            check_termination(res1, skip_solve=False)

            print("--- Stage 1 complete, writing unrestricted Stage 1 result file")
            df_stage1 = extract_results(df.copy(), m1, cost_meta, skip_solve=False, cached_meta=None)
            run_audit(df_stage1, m1, skip_solve=False, cached_meta=None, cost_meta=cost_meta)
            stage1_output = _append_output_suffix(cfg.OUTPUT_FILE, "unrestricted")
            write_excel(
                df_stage1,
                cost_meta,
                stage1_output,
                coal_shadow_prices={},
                merchant_shadow_prices={},
            )
            print(f"--- Stage 1 file saved: {stage1_output}")

            print("--- Staged solve: Stage 2 (FULL startup ramp) with integer hint transfer")
            cfg.USE_SIMPLE_STARTUP_RAMP = _orig_simple

            m = build_model(df, cost_meta)
            warm_start_heuristic(m)
            _copy_integer_hint(m1, m)
            solver = create_solver()
            results = solve_model(solver, m, tee=True)

        # Secondary staged flow: no-coal first solve, then coal-constrained.
        elif cfg.USE_STAGED_COAL_WARMSTART and cfg.USE_COAL_CONSTRAINS:
            print("--- Staged solve: Stage 1 (without coal constraints)")
            _orig_coal = cfg.USE_COAL_CONSTRAINS
            cfg.USE_COAL_CONSTRAINS = False

            m1 = build_model(df, cost_meta)
            warm_start_heuristic(m1)
            solver1 = create_solver()
            res1 = solve_model(solver1, m1, tee=True)
            check_termination(res1, skip_solve=False)

            print("--- Stage 1 complete, writing unrestricted Stage 1 result file")
            df_stage1 = extract_results(df.copy(), m1, cost_meta, skip_solve=False, cached_meta=None)
            run_audit(df_stage1, m1, skip_solve=False, cached_meta=None, cost_meta=cost_meta)
            stage1_output = _append_output_suffix(cfg.OUTPUT_FILE, "unrestricted")
            write_excel(
                df_stage1,
                cost_meta,
                stage1_output,
                coal_shadow_prices={},
                merchant_shadow_prices={},
            )
            print(f"--- Stage 1 file saved: {stage1_output}")

            print("--- Staged solve: Stage 2 (with coal constraints) using integer hint transfer")
            cfg.USE_COAL_CONSTRAINS = _orig_coal

            m = build_model(df, cost_meta)
            warm_start_heuristic(m)
            _copy_integer_hint(m1, m)
            solver = create_solver()
            results = solve_model(solver, m, tee=True)
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
