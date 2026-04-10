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
INPUT_FOLDER = "Inputs_EOD_31_03_2026"


def _find_input_file(folder: str) -> str:
    """Auto-discover the single input .xlsx in the given folder."""
    folder_path = os.path.join(_PROJECT_DIR, folder)
    candidates = [
        f for f in glob.glob(os.path.join(folder_path, "*.xlsx"))
        if "_results_" not in os.path.basename(f)
        and not os.path.basename(f).startswith("~$")
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

_restricted_tag = "restricted_" if USE_COAL_CONSTRAINS else ""
OUTPUT_FILE = os.path.join(os.path.dirname(INPUT_FILE), f"{_input_stem}_results_{_restricted_tag}{_now}.xlsx")

# ============================================================
#                   DATE RANGE
# ============================================================
START_DATE = pd.Timestamp("2026-04-08 00:00")
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
MOSEK_MIO_TOL_REL_GAP = "0.03"    # 1 % MIP gap
MOSEK_MIO_MAX_TIME = "600"         # max 10 minutes

# ============================================================
#                   PLANT / BLOCK SETUP
# ============================================================
BLOCKS = ["A", "B"]      # power-plant blocks to optimise jointly
DOW_BLOCK = "A"           # block whose Pmax is reduced by DOW
DUAL_BLOCK_BOOST = 5.0    # Pmin/Pmax increase when both blocks online [MW]

BIG_M = 500              # tight Big-M (≥ Pmax + boost ≈ 445 MW)
MIN_UP = 8              # min-up time [h]
MIN_DOWN = 6            # min-down time [h]
START_MARGIN_MIN = 0         # minimum margin hurdle / start [EUR]
INITIAL_ON = {"A": 0, "B": 1}  # initial unit commitment state per block
MAX_RAMP_HOURS = 4       # maximum startup ramp duration [h]
USE_SIMPLE_STARTUP_RAMP = False # detailed startup ramp mode (simple mode used in staged Stage 1 only)
USE_STAGED_COAL_WARMSTART = False  # legacy staged approach (no coal first), keep disabled by default
USE_STAGED_RAMP_WARMSTART = True   # Stage 1 simple ramp, Stage 2 full ramp with integer hint transfer

# ============================================================
#                   ECONOMIC PARAMETERS
# ============================================================
OWN_CONSUMPTION = 10.0   # house power [MW]
DEFAULT_GRIDFEE = 23.6   # fallback grid fee [EUR/MWh]
OFFLINE_FIXED_PENALTY_NO_DOW = 3420.0  # [EUR/h] fixed plant-off penalty when USE_DOW_OPPORTUNITY_COSTS=False

# ============================================================
#                   DOW OPPORTUNITY COSTS
# ============================================================
USE_DOW_OPPORTUNITY_COSTS = False   # True: include DOW running costs + DOW revenue in PnL
                                   # False: exclude both DOW running costs and DOW revenue
DOW_OPPORTUNITY_REVENUE = 188.0    # [EUR/MW DOW] — extra revenue per MW DOW (only when USE_DOW_OPPORTUNITY_COSTS=True)
DOW_OFF_CONSUMPTION = 130.0        # [MW] — extra grid consumption from DOW when both blocks offline
DOW_OFF_COMPENSATION = 6.9         # [EUR/MWh] — DOW compensation reducing grid cost on DOW portion
OFFLINE_FIXED_PENALTY_NO_DOW = 3420.0  # [EUR/h] fixed plant-off penalty when USE_DOW_OPPORTUNITY_COSTS=False

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
