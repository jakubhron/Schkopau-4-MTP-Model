"""
Configuration constants for the Schkopau MTP dispatch optimisation model.

All hard-coded parameters, file paths, date ranges, and solver
settings live here so that they can be changed in one place.
"""

from __future__ import annotations

import glob
import os
from datetime import datetime

import pandas as pd

# ============================================================
#                   VERSION
# ============================================================
VERSION = "Schkopau_base_01"

# ============================================================
#                   FILE PATHS
# ============================================================
_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_FOLDER = "Inputs_EOD_20_04_2026"


def _find_input_file(folder: str) -> str:
    """Auto-discover the single input .xlsx in the given folder."""
    folder_path = os.path.join(_PROJECT_DIR, folder)
    _EXCLUDE_PREFIXES = ("~$", "KYOS")
    _EXCLUDE_SUBSTRINGS = ("_results_", "_compare_")
    candidates = [
        f for f in glob.glob(os.path.join(folder_path, "*.xlsx"))
        if not any(os.path.basename(f).startswith(p) for p in _EXCLUDE_PREFIXES)
        and not any(s in os.path.basename(f) for s in _EXCLUDE_SUBSTRINGS)
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise FileNotFoundError(f"No input .xlsx found in {folder_path}")
    raise FileNotFoundError(
        f"Multiple input .xlsx files in {folder_path}, cannot auto-select:\n"
        + "\n".join(f"  - {os.path.basename(c)}" for c in candidates)
    )


INPUT_FILE = _find_input_file(INPUT_FOLDER)

_now = datetime.now().strftime("%Y-%m-%d_%H-%M")
_input_stem = os.path.splitext(os.path.basename(INPUT_FILE))[0]

# ============================================================
#                   COAL CONSTRAINTS
# ============================================================
USE_COAL_CONSTRAINS = True  # enforce monthly coal volume limits from input tab
COAL_TOLERANCE = 0.005        # allow coal limit to be exceeded by this fraction (e.g. 0.02 = 2%)

_restricted_tag = "restricted_" if USE_COAL_CONSTRAINS else ""
OUTPUT_FILE = os.path.join(os.path.dirname(INPUT_FILE), f"{_input_stem}_results_{_restricted_tag}{_now}.xlsx")

# ============================================================
#                   DATE RANGE
# ============================================================
START_DATE = pd.Timestamp("2026-04-21 00:00")
END_DATE = pd.Timestamp("2026-12-31 23:00")

# ============================================================
#                   SOLVER CACHE
# ============================================================
USE_CACHED_SOLUTION = False
CACHE_TAG = "chrono_AP1_cache_v3_2026-03-17_09-52" if USE_CACHED_SOLUTION else f"chrono_AP1_cache_v3_{_now}"
CACHE_DIR = r"./_solver_cache"
SKIP_SOLVE_AND_EXTRACT = False

# ============================================================
#                   SOLVER SETTINGS
# ============================================================
USE_MOSEK = True
MOSEK_MIO_TOL_REL_GAP = "0.03"    # 3 % MIP gap
MOSEK_MIO_MAX_TIME = "2500"         # max 25 minutes

# ============================================================
#                   PLANT CONSTRAINTS
# ============================================================
BLOCKS = ["A", "B"]      # power-plant blocks to optimise jointly
DOW_BLOCK = "A"           # block whose Pmax is reduced by DOW
DUAL_BLOCK_BOOST = 5.0    # Pmin/Pmax increase when both blocks online [MW]

BIG_M = 500              # tight Big-M (≥ Pmax + boost ≈ 445 MW)
MIN_UP = 8              # min-up time [h]
MIN_DOWN = 6            # min-down time [h]
START_MARGIN_MIN = 0     # minimum margin hurdle / start [EUR]
INITIAL_ON = {"A": 0, "B": 1}  # initial unit commitment state per block
MAX_RAMP_HOURS = 3       # maximum startup ramp duration [h]

# ============================================================
#                   STARTUP RAMP AND SOLVE MODE
# ============================================================
# --- Solve mode ---
# Controls startup-ramp fidelity and warm-start staging strategy.
#   "simple"            → single-stage, simple ramp (Pmin/Pmax only, fast)
#   "full"              → single-stage, full multi-hour ramp (accurate, slow)
#   "staged_ramp"       → 2-stage: simple ramp → full ramp (recommended)
# Coal constraints are toggled independently via USE_COAL_CONSTRAINS.
SOLVE_MODE = "staged_ramp"

# Number of iterative re-linearization passes in Stage 2.
# 0 = no iteration (single Stage 2 solve with Stage 1 hint).
# Each pass re-linearises DUO at the previous Stage 2 P_eff values.
RELINEARIZE_ITERS = 0

# Internal flag toggled by main.py during staged solves.  Do not set directly.
USE_SIMPLE_STARTUP_RAMP = SOLVE_MODE == "simple"

# ============================================================
#                   ECONOMIC PARAMETERS
# ============================================================
OWN_CONSUMPTION = 10.0   # house power [MW]
DEFAULT_GRIDFEE = 23.6   # fallback grid fee [EUR/MWh]
OFFLINE_FIXED_PENALTY_NO_DOW = 3420  # [EUR/h] fixed plant-off penalty when USE_DOW_OPPORTUNITY_COSTS=False

# ============================================================
#                   DOW OPPORTUNITY COSTS
# ============================================================
USE_DOW_OPPORTUNITY_COSTS = False  # True: include DOW running costs + DOW revenue in PnL
                                   # False: exclude both DOW running costs and DOW revenue
DOW_OPPORTUNITY_REVENUE = 188.0    # [EUR/MW DOW] — extra revenue per MW DOW (only when USE_DOW_OPPORTUNITY_COSTS=True)
DOW_OFF_CONSUMPTION = 130.0        # [MW] — extra grid consumption from DOW when both blocks offline
DOW_OFF_COMPENSATION = 6.9         # [EUR/MWh] — DOW compensation reducing grid cost on DOW portion


# ============================================================
#                   MONTHLY LAYOUT (for Excel reporting)
# ============================================================
MONTH_NAMES = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}

# ============================================================
#                   COST CURVE
# ============================================================
# Single linear total cost: cost_slope * P_eff + cost_fixed
# Computed in data_loader._compute_cost_curve().


def get_cache_paths() -> tuple[str, str]:
    """Return (parquet_path, meta_json_path) for the solver cache."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    df_path = os.path.join(CACHE_DIR, f"cache_{CACHE_TAG}.parquet")
    meta_path = os.path.join(CACHE_DIR, f"cache_{CACHE_TAG}.json")
    return df_path, meta_path
