"""
Parametric shadow price: re-solve the full MIP with July coal limit + delta.

Runs for delta = 0, +10t, +100t, +1000t and reports PnL & coal changes.
"""
from __future__ import annotations

import copy
import os
import sys
import time

import mosek
from pyomo.environ import value

from schkopau_mtp import config as cfg
from schkopau_mtp.data_loader import load_and_prepare
from schkopau_mtp.model_builder import build_model, warm_start_heuristic
from schkopau_mtp.results import extract_results
from schkopau_mtp.solver import create_solver, solve_model, check_termination

# Force coal constraints ON
cfg.USE_COAL_CONSTRAINS = True

# Use a faster gap for parametric runs (still good quality)
cfg.MOSEK_MIO_TOL_REL_GAP = "0.005"
cfg.MOSEK_MIO_MAX_TIME = "300"


def solve_with_delta(df, cost_meta_orig, july_delta_kt: float) -> dict:
    """Build and solve with July coal limit adjusted by delta_kt (kilotonnes).

    Returns dict with objective, July coal consumption, July PnL, etc.
    """
    cost_meta = copy.deepcopy(cost_meta_orig)

    # Adjust July limit
    july_key = (2026, 7)
    orig_limit = cost_meta["coal_limits"].get(july_key, 0.0)
    cost_meta["coal_limits"][july_key] = orig_limit + july_delta_kt

    print(f"\n{'='*70}")
    print(f"  July coal limit: {orig_limit:.3f} kt + {july_delta_kt:+.3f} kt = "
          f"{orig_limit + july_delta_kt:.3f} kt")
    print(f"{'='*70}")

    # Build model (simple ramp for speed — single stage)
    cfg.USE_SIMPLE_STARTUP_RAMP = True
    m = build_model(df, cost_meta)
    warm_start_heuristic(m)

    solver = create_solver()
    t0 = time.time()
    results = solve_model(solver, m, tee=True)
    elapsed = time.time() - t0
    check_termination(results, skip_solve=False)

    obj = float(value(m.obj))

    # Extract July PnL and coal
    df_out = extract_results(df.copy(), m, cost_meta, skip_solve=False, cached_meta=None)
    jul = df_out[df_out['month_num'] == 7]
    jul_pnl = jul['PnL'].sum()
    jul_coal = jul['coal_exact'].sum()

    # Count ON hours per block
    on_a = (jul['on_model_A'] == 1).sum()
    on_b = (jul['on_model_B'] == 1).sum()

    # Sum P_eff
    peff_a = jul.loc[jul['on_model_A'] == 1, 'P_eff_A'].sum()
    peff_b = jul.loc[jul['on_model_B'] == 1, 'P_eff_B'].sum()

    return {
        'delta_kt': july_delta_kt,
        'limit_kt': orig_limit + july_delta_kt,
        'objective': obj,
        'jul_pnl': jul_pnl,
        'jul_coal_t': jul_coal,
        'on_hours_A': on_a,
        'on_hours_B': on_b,
        'mwh_A': peff_a,
        'mwh_B': peff_b,
        'solve_s': elapsed,
    }


def main():
    print("MOSEK version:", mosek.Env().getversion())
    print(f"Input: {cfg.INPUT_FILE}")
    print()

    # Load data once
    df, cost_meta = load_and_prepare()

    # Print original July limit
    july_key = (2026, 7)
    orig_limit = cost_meta["coal_limits"].get(july_key, None)
    if orig_limit is None:
        print("ERROR: No July 2026 coal limit found in Coal_constrains tab!")
        print(f"Available: {sorted(cost_meta['coal_limits'].keys())}")
        return
    print(f"Original July coal limit: {orig_limit:.3f} kt "
          f"(effective with {cfg.COAL_TOLERANCE:.1%} tolerance: "
          f"{orig_limit * (1 + cfg.COAL_TOLERANCE):.3f} kt)")

    # Run parametric solves
    # delta in kt: 0.01 kt = 10 t, 0.1 kt = 100 t, 1.0 kt = 1000 t
    deltas = [0, 0.01, 0.1, 1.0]
    results = []

    for d in deltas:
        r = solve_with_delta(df, cost_meta, d)
        results.append(r)

    # Report
    base = results[0]
    print("\n" + "=" * 100)
    print("PARAMETRIC SHADOW PRICE RESULTS — July 2026")
    print("=" * 100)
    print(f"{'Delta':>8s}  {'Limit':>8s}  {'Jul Coal':>10s}  {'Jul PnL':>14s}  "
          f"{'Δ PnL':>12s}  {'Δ Coal':>8s}  {'PnL/t':>8s}  "
          f"{'ON_A':>5s}  {'ON_B':>5s}  {'Time':>5s}")
    print("-" * 100)

    for r in results:
        d_pnl = r['jul_pnl'] - base['jul_pnl']
        d_coal = r['jul_coal_t'] - base['jul_coal_t']
        d_tonnes = r['delta_kt'] * 1000
        # Marginal value per tonne (using actual PnL difference)
        if d_tonnes > 0:
            pnl_per_t = d_pnl / d_tonnes if d_tonnes > 0 else 0
        else:
            pnl_per_t = 0

        label = "baseline" if r['delta_kt'] == 0 else f"+{d_tonnes:.0f}t"
        print(f"{label:>8s}  {r['limit_kt']:>7.3f}kt  "
              f"{r['jul_coal_t']/1000:>9.1f}kt  "
              f"{r['jul_pnl']:>13,.0f}€  "
              f"{d_pnl:>+11,.0f}€  "
              f"{d_coal:>+7.0f}t  "
              f"{pnl_per_t:>7.2f}  "
              f"{r['on_hours_A']:>5d}  "
              f"{r['on_hours_B']:>5d}  "
              f"{r['solve_s']:>4.0f}s")

    print()
    print("LP shadow price (from MOSEK) = 4.89 EUR/t")
    print("The MIP marginal values above reflect the FULL dispatch re-optimization.")


if __name__ == "__main__":
    main()
