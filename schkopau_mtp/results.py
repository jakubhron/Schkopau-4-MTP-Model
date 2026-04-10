"""
Extract optimisation results from the solved Pyomo model back into the
pandas DataFrame, compute PnL components, and run the audit / reconciliation
checks.  Supports joint two-block (A, B) models.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd
from pyomo.environ import value

from . import config as cfg


# ====================================================================
#  PUBLIC API
# ====================================================================


def extract_results(
    df: pd.DataFrame,
    m,
    cost_meta: dict,
    *,
    skip_solve: bool = False,
    cached_meta: Optional[dict] = None,
) -> pd.DataFrame:
    """
    Pull variable values from the solved model into *df* and compute all
    PnL components for each block.
    """
    if not skip_solve:
        df = _extract_decision_variables(df, m)

    df = _compute_pnl(df, cost_meta)

    return df


def run_audit(
    df: pd.DataFrame,
    m,
    *,
    skip_solve: bool = False,
    cached_meta: Optional[dict] = None,
    cost_meta: Optional[dict] = None,
) -> None:
    """Print objective vs PnL reconciliation to stdout."""
    if skip_solve:
        obj_val = (
            float(cached_meta.get("objective_value"))
            if isinstance(cached_meta, dict) and cached_meta.get("objective_value") is not None
            else None
        )
    else:
        obj_val = float(value(m.obj))

    pnl_val = float(df["PnL"].sum()) if "PnL" in df.columns else None

    print("\n" + "=" * 80)
    print("OBJ vs PnL AUDIT")
    print(f"Objective (value(m.obj)) : {obj_val:,.2f}")
    if pnl_val is not None:
        print(f"Sum df['PnL']            : {pnl_val:,.2f}")
        print(f"DELTA obj - PnL          : {obj_val - pnl_val:,.2f}")
    print("=" * 80)

    _print_pnl_reconciliation(df, obj_val, pnl_val)

    if not skip_solve:
        _print_pyomo_component_audit(df, m, obj_val, cost_meta=cost_meta)


# ====================================================================
#  INTERNAL – variable extraction
# ====================================================================


def _round_binary(x) -> int:
    """Safely round a binary solver value to 0/1."""
    v = value(x)
    if v is None:
        return 0
    return int(round(float(v)))


def _extract_decision_variables(df: pd.DataFrame, m) -> pd.DataFrame:
    EPS = 1e-3

    for b in cfg.BLOCKS:
        P_vals = []
        for t in m.T:
            p = value(m.P[b, t])
            if p is None or abs(p) < EPS:
                p = 0.0
            P_vals.append(p)
        df[f"P_{b}"] = P_vals

        df[f"on_model_{b}"] = [_round_binary(m.on[b, t]) for t in m.T]
        df[f"startup_{b}"] = [_round_binary(m.startup[b, t]) for t in m.T]
        df[f"shutdown_flag_{b}"] = [
            _round_binary(m.shutdown[b, t]) if t > 0 else 0 for t in m.T
        ]

    # Plant-level aggregates (sum of blocks)
    df["P"] = sum(df[f"P_{b}"] for b in cfg.BLOCKS)
    df["on_model"] = (sum(df[f"on_model_{b}"] for b in cfg.BLOCKS) > 0).astype(int)
    df["startup"] = (sum(df[f"startup_{b}"] for b in cfg.BLOCKS) > 0).astype(int)
    df["shutdown_flag"] = (sum(df[f"shutdown_flag_{b}"] for b in cfg.BLOCKS) > 0).astype(int)

    return df


# ====================================================================
#  INTERNAL – tiered startup cost helper
# ====================================================================


def _compute_tiered_start_cost(on_series, startup_series, tiers, initial_on=1):
    """Compute startup cost per hour matching the Pyomo 3-tier model.

    Tiers: hot (<10h), warm (10-60h), vcold (≥60h).
    Cost is assigned based on offline duration at startup.
    """
    on_arr = on_series.values
    su_arr = startup_series.values
    n = len(on_arr)
    costs = np.zeros(n)

    if not tiers:
        return pd.Series(costs, index=on_series.index)

    tier_map = {t["name"]: t for t in tiers}
    hot_cost = tier_map.get("hot", {}).get("cost", 0.0)
    warm_cost = tier_map.get("warm", {}).get("cost", 0.0)
    vcold_cost = tier_map.get("vcold", {}).get("cost", warm_cost)
    hot_thresh = 10
    vcold_thresh = 60

    # If the block was ON before the horizon, off_count starts at 0 (just turned on).
    # If it was OFF, we don't know how long — assume very cold (large off_count).
    off_count = 0 if initial_on else vcold_thresh
    for i in range(n):
        if int(round(on_arr[i])) == 0:
            off_count += 1
        else:
            if int(round(su_arr[i])) == 1:
                if off_count >= vcold_thresh:
                    costs[i] = vcold_cost
                elif off_count < hot_thresh:
                    costs[i] = hot_cost
                else:
                    costs[i] = warm_cost
            off_count = 0

    return pd.Series(costs, index=on_series.index)


# ====================================================================
#  INTERNAL – PnL computation
# ====================================================================


def _compute_pnl(df: pd.DataFrame, cost_meta: dict) -> pd.DataFrame:
    """Compute all hourly PnL components matching the Pyomo objective."""
    starts = cost_meta.get("starts", {})

    # Per-block run costs
    total_run_costs = pd.Series(0.0, index=df.index)
    total_start_cost = pd.Series(0.0, index=df.index)
    total_profit_spot = pd.Series(0.0, index=df.index)

    _other_peff = [b for b in cfg.BLOCKS if b != cfg.DOW_BLOCK][0]
    _dow_vals = pd.to_numeric(df.get("DOW", 0.0), errors="coerce").fillna(0.0)

    for b in cfg.BLOCKS:
        on_b = df[f"on_model_{b}"]
        P_b = df[f"P_{b}"]
        # P_eff follows peff_def_rule:
        # USE_DOW=True:  P_eff = P + DOW (DOW coal included)
        # USE_DOW=False: P_eff = P       (DOW deducted from Pmax, lower costs)
        if not cfg.USE_DOW_OPPORTUNITY_COSTS:
            P_eff_b = P_b
        elif b == cfg.DOW_BLOCK:
            P_eff_b = P_b + _dow_vals * on_b
        else:
            _both_on = (df[f"on_model_{cfg.DOW_BLOCK}"] * on_b)
            P_eff_b = P_b + _dow_vals * (on_b - _both_on)
        df[f"P_eff_{b}"] = P_eff_b

        rc = df[f"cost_slope_{b}"] * P_eff_b + df[f"cost_fixed_{b}"] * on_b
        df[f"run_costs_{b}"] = rc
        total_run_costs += rc

        # Tiered startup cost: compute offline duration at each startup
        sc = _compute_tiered_start_cost(on_b, df[f"startup_{b}"], starts.get(b, []),
                                         initial_on=cfg.INITIAL_ON.get(b, 1))
        df[f"start_cost_{b}"] = sc
        total_start_cost += sc

        ps = P_b * df["Price"]
        df[f"profit_spot_{b}"] = ps
        total_profit_spot += ps

    # Plant-level aggregates
    df["run_costs"] = total_run_costs
    df["start_cost"] = total_start_cost
    df["profit_spot"] = total_profit_spot

    # OFF_costs when BOTH blocks offline
    _any_on = sum(df[f"on_model_{b}"] for b in cfg.BLOCKS)
    plant_off = (_any_on == 0).astype(int)
    df["OFF_costs"] = plant_off * cfg.OWN_CONSUMPTION * (df["Price"] + df["GRIDFEE"])
    if cfg.USE_DOW_OPPORTUNITY_COSTS:
        df["OFF_costs"] += plant_off * cfg.DOW_OFF_CONSUMPTION * df["GRIDFEE"]
        df["OFF_costs"] -= plant_off * cfg.DOW_OFF_CONSUMPTION * cfg.DOW_OFF_COMPENSATION
    else:
        df["OFF_costs"] += plant_off * cfg.OFFLINE_FIXED_PENALTY_NO_DOW

    df["P_eff"] = sum(df[f"P_eff_{b}"] for b in cfg.BLOCKS)

    # DOW revenue — active when at least one block is running
    _other = [b for b in cfg.BLOCKS if b != cfg.DOW_BLOCK][0]
    _on_A = df[f"on_model_{cfg.DOW_BLOCK}"]
    _on_B = df[f"on_model_{_other}"]
    dow_on = (_on_A + _on_B - _on_A * _on_B).clip(upper=1)  # at least one on
    df["DOW_revenues_real"] = df["DOW revenues"] * dow_on

    # DOW volumes
    _w = pd.to_numeric(df.get("DOW", 0.0), errors="coerce").fillna(0.0)
    df["DOW_GWhth_h"] = (_w * dow_on) / 1000.0

    df["PnL"] = (
        df["profit_spot"]
        + df["DOW_revenues_real"]
        - df["run_costs"]
        - df["start_cost"]
        - df["OFF_costs"]
    )

    # Per-block PnL (spot revenue - run costs - start costs; OFF_costs/DOW are plant-level)
    for b in cfg.BLOCKS:
        df[f"PnL_{b}"] = df[f"profit_spot_{b}"] - df[f"run_costs_{b}"] - df[f"start_cost_{b}"]

    return df


# ====================================================================
#  INTERNAL – audit / reconciliation
# ====================================================================


def _print_pnl_reconciliation(df, obj_val, pnl_val):
    """Decompose PnL from DataFrame columns and compare to objective."""

    def _sumcol(name):
        return float(df[name].sum()) if name in df.columns else None

    parts: dict = {
        "profit_spot": _sumcol("profit_spot"),
        "DOW_revenues_real": _sumcol("DOW_revenues_real"),
    }
    cost_cols = [
        "run_costs", "OFF_costs", "start_cost",
    ]
    for c in cost_cols:
        s = _sumcol(c)
        if s is not None:
            parts[f"-{c}"] = -s

    parts = {k: v for k, v in parts.items() if v is not None}
    recon = sum(parts.values())

    print(
        "\nReconstructed PnL from parts "
        "(should equal df['PnL'] if PnL is built from same parts):"
    )
    for k in sorted(parts.keys()):
        print(f"  {k:30s} {parts[k]:,.2f}")
    print(f"  {'-' * 30} {'-' * 18}")
    print(f"  {'RECON SUM':30s} {recon:,.2f}")

    if pnl_val is not None:
        print(f"\nDelta (PnL - recon)      : {pnl_val - recon:,.2f}")
    print(f"Delta (obj - recon)      : {obj_val - recon:,.2f}")

    # Per-hour diff
    if pnl_val is not None:
        recon_h = 0.0
        for k in parts.keys():
            col = k[1:] if k.startswith("-") else k
            sign = -1.0 if k.startswith("-") else 1.0
            if col in df.columns:
                recon_h = recon_h + sign * df[col]

        diff_h = df["PnL"] - recon_h
        max_abs = float(diff_h.abs().max())
        print(f"\nMax |PnL - recon| per hour: {max_abs:,.6f}")

        if max_abs > 1e-6:
            tmp = (
                df.loc[:, ["Date"]].copy()
                if "Date" in df.columns
                else df.index.to_frame(index=False)
            )
            tmp["diff_PnL_minus_recon"] = diff_h
            tmp["absdiff"] = diff_h.abs()
            tmp = tmp.sort_values("absdiff", ascending=False).head(12)
            print("\nTop hours where df['PnL'] != sum(parts):")
            print(tmp.to_string(index=False))

    print("\n" + "=" * 80)

    # Objective-equivalent recomputation (dispatch-based DOW)
    _other_r = [b for b in cfg.BLOCKS if b != cfg.DOW_BLOCK][0]
    _on_Ar = df[f"on_model_{cfg.DOW_BLOCK}"]
    _on_Br = df[f"on_model_{_other_r}"]
    _dow_on = (_on_Ar + _on_Br - _on_Ar * _on_Br).clip(upper=1)
    obj_profit_spot = df["P"] * df["Price"]
    obj_dow_revenue = df["DOW revenues"] * _dow_on
    obj_run_costs = df["run_costs"]
    obj_OFF_costs = df["OFF_costs"]
    obj_start_cost = df["start_cost"]

    checks = [
        ("profit_spot", obj_profit_spot, df["profit_spot"]),
        ("DOW_revenues_real", obj_dow_revenue, df["DOW_revenues_real"]),
        ("run_costs", obj_run_costs, df["run_costs"]),
        ("OFF_costs", obj_OFF_costs, df["OFF_costs"]),
        ("start_cost", obj_start_cost, df["start_cost"]),
    ]

    print("\n=== Objective-vs-DF component deltas (sum(obj_term - df_col)) ===")
    total_delta = 0.0
    for name, obj_term, df_col in checks:
        d = float((obj_term - df_col).sum())
        total_delta += d
        print(f"{name:26s} {d:,.2f}")
    print(f"{'TOTAL (should match obj - PnL)':26s} {total_delta:,.2f}")


def _print_pyomo_component_audit(df, m, obj_val_global, cost_meta=None):
    """Compare Pyomo-evaluated term sums vs DataFrame term sums (solve only)."""
    starts = (cost_meta or {}).get("starts", {})

    # Pre-compute per-block tiered costs (3 tiers)
    _warm_cost = {}
    _hot_delta = {}
    _vcold_delta = {}
    for b in cfg.BLOCKS:
        tiers = {t["name"]: t for t in starts.get(b, [])}
        hc = tiers.get("hot", {}).get("cost", 47_510.0)
        wc = tiers.get("warm", {}).get("cost", 38_291.0)
        vc = tiers.get("vcold", {}).get("cost", 60_251.0)
        _warm_cost[b] = wc
        _hot_delta[b] = hc - wc
        _vcold_delta[b] = vc - wc

    def _v(x):
        xv = value(x)
        return 0.0 if xv is None else float(xv)

    py_profit_spot = 0.0
    py_dow_rev = 0.0
    py_run_costs = 0.0
    py_off_costs = 0.0
    py_start = 0.0

    for t in m.T:
        for b in m.B:
            profit_spot = _v(m.P[b, t]) * _v(m.price[t])
            run_costs = _v(m.run_costs[b, t])
            start_cost = (
                _warm_cost[b] * _v(m.startup[b, t])
                + _hot_delta[b] * _v(m.hot_start[b, t])
                + _vcold_delta[b] * _v(m.vcold_start[b, t])
            )

            py_profit_spot += profit_spot
            py_run_costs += run_costs
            py_start += start_cost

        # OFF_costs when both blocks offline
        py_off_costs += _v(m.plant_off[t]) * cfg.OWN_CONSUMPTION * (
            _v(m.price[t]) + _v(m.gridfee[t])
        )
        if cfg.USE_DOW_OPPORTUNITY_COSTS:
            py_off_costs += _v(m.plant_off[t]) * cfg.DOW_OFF_CONSUMPTION * _v(m.gridfee[t])
            py_off_costs -= _v(m.plant_off[t]) * cfg.DOW_OFF_CONSUMPTION * cfg.DOW_OFF_COMPENSATION
        else:
            py_off_costs += _v(m.plant_off[t]) * cfg.OFFLINE_FIXED_PENALTY_NO_DOW

        # DOW (plant-level, dispatch-based)
        _other_py = [b for b in cfg.BLOCKS if b != cfg.DOW_BLOCK][0]
        _on_A_py = _v(m.on[cfg.DOW_BLOCK, t])
        _on_B_py = _v(m.on[_other_py, t])
        plant_on = min(_on_A_py + _on_B_py, 1.0)  # at least one on
        dow_revenue = _v(m.DOW_rev[t]) * plant_on
        py_dow_rev += dow_revenue

    py_recon = (
        py_profit_spot + py_dow_rev
        - py_run_costs - py_off_costs - py_start
    )
    df_recon = float(df["PnL"].sum()) if "PnL" in df.columns else float("nan")
    obj_val_local = float(value(m.obj))

    print("\n=== PYOMO component sums (solve mode) ===")
    print(f"py_profit_spot           : {py_profit_spot:,.2f}")
    print(f"py_dow_revenue           : {py_dow_rev:,.2f}")
    print(f"py_run_costs             : {py_run_costs:,.2f}")
    print(f"py_OFF_costs             : {py_off_costs:,.2f}")
    print(f"py_start_cost            : {py_start:,.2f}")
    print(f"py_reconstructed_obj     : {py_recon:,.2f}")
    print(f"value(m.obj)             : {obj_val_local:,.2f}")
    print(f"delta obj - py_recon      : {obj_val_local - py_recon:,.6f}")
    print(f"df['PnL'].sum()           : {df_recon:,.2f}")
    print(f"delta obj - dfPnL         : {obj_val_local - df_recon:,.6f}")
    print()

    def _ds(name):
        return float(df[name].sum()) if name in df.columns else None

    compare = [
        ("profit_spot", py_profit_spot, _ds("profit_spot")),
        ("DOW_revenues_real", py_dow_rev, _ds("DOW_revenues_real")),
        ("run_costs", py_run_costs, _ds("run_costs")),
        ("OFF_costs", py_off_costs, _ds("OFF_costs")),
        ("start_cost", py_start, _ds("start_cost")),
    ]

    print("=== PYOMO vs DF term deltas (py - df) ===")
    for name, pyv, dfv in compare:
        if dfv is None:
            continue
        print(f"{name:26s} {(pyv - dfv):,.6f}")

    # Objective decomposition
    def tsum(gen):
        acc = 0.0
        for x in gen:
            vx = value(x)
            if vx is None or (isinstance(vx, float) and (math.isnan(vx) or math.isinf(vx))):
                continue
            acc += float(vx)
        return acc

    T = list(m.T)
    B = list(m.B)
    _obj_profit_spot = tsum(m.P[b, t] * m.price[t] for t in T for b in B)

    print(f"\n=== PYOMO objective decomposition (TOTAL over horizon) ===")
    print(f"value(m.obj)           : {float(value(m.obj)):,.2f}")
