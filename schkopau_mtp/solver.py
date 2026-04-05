"""
Solver management for the Schkopau MTP model.

Handles:
  - Solver factory creation (MOSEK / HiGHS)
  - Cache load / save
  - Solve invocation
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Optional, Tuple

import mosek
import pandas as pd

from pyomo.environ import Binary, NonNegativeReals, Reals, SolverFactory, Suffix, Var, value
from pyomo.opt import TerminationCondition

from . import config as cfg


# ====================================================================
#  PUBLIC API
# ====================================================================


def create_solver():
    """Create and configure the MILP solver instance."""
    if cfg.USE_MOSEK:
        solver = SolverFactory("mosek")
    else:
        solver = SolverFactory("highs")

    solver.options["MSK_DPAR_MIO_TOL_REL_GAP"] = cfg.MOSEK_MIO_TOL_REL_GAP
    solver.options["MSK_DPAR_MIO_MAX_TIME"] = cfg.MOSEK_MIO_MAX_TIME
    solver.options["MSK_IPAR_MIO_CONSTRUCT_SOL"] = "MSK_ON"  # use initial variable values
    return solver


def try_load_cache() -> Tuple[Optional[pd.DataFrame], Optional[dict]]:
    """
    Attempt to load a cached solver solution.

    Returns (df, meta) if cache exists and ``USE_CACHED_SOLUTION`` is True,
    otherwise (None, None).
    """
    cache_df_path, cache_meta_path = cfg.get_cache_paths()

    if (
        cfg.USE_CACHED_SOLUTION
        and os.path.exists(cache_df_path)
        and os.path.exists(cache_meta_path)
    ):
        print(f"--- Loading cached df from {cache_df_path}")
        df = pd.read_parquet(cache_df_path)
        with open(cache_meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        print("--- Cached meta:", meta)
        return df, meta

    return None, None


def solve_model(solver, model, *, tee: bool = True):
    """Run the solver, injecting warm-start hints via MOSEK API."""
    # Monkey-patch _apply_solver to inject initial integer solution
    original_apply = solver._apply_solver

    def _patched_apply():
        task = solver._solver_model
        numvar = task.getnumvar()
        xx = [0.0] * numvar
        n_int_set = 0
        n_cont_set = 0

        # Collect variable types from MOSEK
        vartypes = [mosek.variabletype.type_cont] * numvar
        for j in range(numvar):
            vartypes[j] = task.getvartype(j)

        for pyomo_var, mosek_var in solver._pyomo_var_to_solver_var_map.items():
            # Use the fixed value for fixed variables, heuristic .value otherwise
            if pyomo_var.fixed:
                v = value(pyomo_var)
            else:
                v = pyomo_var.value
            if v is not None:
                idx = mosek_var if isinstance(mosek_var, int) else mosek_var.index
                xx[idx] = float(v)
                if vartypes[idx] == mosek.variabletype.type_int:
                    n_int_set += 1
                else:
                    n_cont_set += 1

        if n_int_set > 0:
            task.putxx(mosek.soltype.itg, xx)
            # Force CONSTRUCT_SOL directly on the MOSEK task
            task.putintparam(mosek.iparam.mio_construct_sol,
                             mosek.onoffkey.on)
            print(f"--- Injected warm-start: {n_int_set} integer, "
                  f"{n_cont_set} continuous values")
            # Debug: count how many integer vars are 1
            n_ones = sum(1 for j in range(numvar)
                         if vartypes[j] == mosek.variabletype.type_int
                         and abs(xx[j] - 1.0) < 0.01)
            print(f"--- Integer vars set to 1: {n_ones} / {n_int_set}")
        return original_apply()

    solver._apply_solver = _patched_apply
    try:
        return solver.solve(model, tee=tee)
    finally:
        solver._apply_solver = original_apply


def save_cache(df: pd.DataFrame, obj_val: Optional[float]) -> None:
    """Persist the current solution DataFrame and metadata to disk."""
    cache_df_path, cache_meta_path = cfg.get_cache_paths()
    meta_out = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "objective_value": obj_val,
        "cache_tag": cfg.CACHE_TAG,
    }
    df.to_parquet(cache_df_path, index=False)
    with open(cache_meta_path, "w", encoding="utf-8") as f:
        json.dump(meta_out, f, indent=2)
    print(f"--- Cached solution saved to {cache_df_path}")


def check_termination(results, skip_solve: bool) -> TerminationCondition:
    """
    Extract termination condition; raise if it is neither optimal
    nor maxTimeLimit.
    """
    if skip_solve:
        return TerminationCondition.optimal

    term = results.solver.termination_condition
    print("--- Solver termination:", term)
    if term not in (TerminationCondition.optimal, TerminationCondition.maxTimeLimit, TerminationCondition.feasible):
        raise RuntimeError(f"Solver ended with {term}")
    return term


def extract_coal_shadow_prices(m) -> dict:
    """Fix integers after MIP solve, re-solve as LP, return coal constraint duals.

    Returns
    -------
    dict of (year, month) -> shadow_price [EUR/t]
        Positive value = how much coal price should increase to naturally
        reach the monthly limit without the constraint.
        Zero when the constraint is not binding.
    """
    if not hasattr(m, "coal_monthly_limit"):
        return {}

    print("--- Extracting coal shadow prices (LP re-solve) ...")

    # Fix all integer/binary variables to their MIP solution values
    # AND relax their domain to continuous so MOSEK treats the re-solve as LP.
    fixed_vars: list = []  # (var_component, index, original_domain)
    for v in m.component_objects(Var, active=True):
        for idx in v:
            vd = v[idx]
            if vd.is_integer() or vd.is_binary():
                orig_domain = vd.domain
                if not vd.fixed:
                    vd.fix(round(value(vd)))
                    fixed_vars.append((v, idx, orig_domain, True))
                else:
                    # Already fixed — still need to relax domain
                    fixed_vars.append((v, idx, orig_domain, False))
                vd.domain = NonNegativeReals

    # Add dual suffix so Pyomo imports LP duals
    m.dual = Suffix(direction=Suffix.IMPORT)

    # Re-solve as LP (all integers now fixed + relaxed → pure LP)
    lp_solver = SolverFactory("mosek")
    lp_solver.solve(m, tee=False)

    # Extract duals for each coal month constraint
    shadow_prices: dict = {}
    for ym in m.coal_months:
        dual_val = m.dual.get(m.coal_monthly_limit[ym], 0.0)
        # MOSEK returns positive dual for a binding ≤ constraint in a max problem.
        # Shadow price = dual = marginal objective gain per extra ton of coal [EUR/t].
        shadow_prices[ym] = dual_val

    # Cleanup: restore domains, unfix variables, remove dual suffix
    for v, idx, orig_domain, was_unfixed in fixed_vars:
        v[idx].domain = orig_domain
        if was_unfixed:
            v[idx].unfix()
    m.del_component(m.dual)

    for ym, sp in sorted(shadow_prices.items()):
        print(f"    Coal price add-on {ym[0]}-{ym[1]:02d}: {sp:+.2f} EUR/t"
              f"  {'(binding)' if abs(sp) > 0.01 else '(not binding)'}")

    return shadow_prices
