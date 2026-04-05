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
from pyomo.environ import value

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
