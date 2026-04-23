import pandas as pd, numpy as np, shutil, tempfile, os, sys

OWN_CONSUMPTION = 12.0
OFFLINE_FIXED_PENALTY_NO_DOW = 3420

folder = r'C:\Users\jakub.hron\OneDrive - EP Commodities, a.s\Documents\Schkopau_mtp\Inputs_EOD_20_04_2026'
kyos_file = folder + r'\KYOS.xlsx'
our_file  = folder + r'\Data1 EPNLintr26 High availability_20_04_2026_results_restricted_2026-04-23_14-15.xlsx'

# --- Read KYOS ---
tmp_fd_k, tmp_k = tempfile.mkstemp(suffix='.xlsx'); os.close(tmp_fd_k)
shutil.copy2(kyos_file, tmp_k)
ka = pd.read_excel(tmp_k, sheet_name='block_A')
kb = pd.read_excel(tmp_k, sheet_name='block_B')
os.unlink(tmp_k)
ka['Date'] = pd.to_datetime(dict(year=ka['Year'], month=ka['Month'], day=ka['Day'])) + pd.to_timedelta(ka['Hour'], unit='h')
kb['Date'] = pd.to_datetime(dict(year=kb['Year'], month=kb['Month'], day=kb['Day'])) + pd.to_timedelta(kb['Hour'], unit='h')
kyos = ka[['Date','Intrinsic']].rename(columns={'Intrinsic':'K_P_A'}).merge(
       kb[['Date','Intrinsic']].rename(columns={'Intrinsic':'K_P_B'}), on='Date')
kyos['K_P_A'] = kyos['K_P_A'].clip(lower=0)
kyos['K_P_B'] = kyos['K_P_B'].clip(lower=0)
kyos['K_P'] = kyos['K_P_A'] + kyos['K_P_B']

# --- Read our results ---
tmp_fd, tmp = tempfile.mkstemp(suffix='.xlsx'); os.close(tmp_fd)
shutil.copy2(our_file, tmp)
res = pd.read_excel(tmp, sheet_name='Results')
os.unlink(tmp)
# Date column stores date-only (no time); add Hour to reconstruct full datetime
res['Date'] = pd.to_datetime(res['Date']) + pd.to_timedelta(res['Hour'], unit='h')

merged = kyos.merge(res, on='Date', how='left')

# April 2026 (from 20th onward)
apr_k = merged[(merged['Date'] >= '2026-04-20') & (merged['Date'].dt.month==4) & (merged['Date'].dt.year==2026)].copy()
apr_o = res[(res['Date'] >= '2026-04-20') & (res['Date'].dt.month==4) & (res['Date'].dt.year==2026)].copy()

# ---------- Build cost curves from columns available in Results sheet ----------
# linear cost model:  cost(P) [€/h] = cost_slope [€/MWh] * P [MW] + cost_fixed [€/h] * on
# The same formula used by the optimisation model (see data_loader._compute_cost_curves).
for b in ('A', 'B'):
    tc_pmin = apr_k[f'Total generation costs at Pmin [€/MWh]_{b}']
    tc_pmax = apr_k[f'Total generation costs at Pmax [€/MWh]_{b}']
    pmin    = apr_k[f'Pmin_{b}']
    pmax    = apr_k[f'Pmax_{b}']
    Cmin = tc_pmin * pmin          # €/h at Pmin
    Cmax = tc_pmax * pmax          # €/h at Pmax
    denom = (pmax - pmin).replace(0.0, np.nan)
    slope = ((Cmax - Cmin) / denom).fillna(0.0)
    fixed = Cmin - slope * pmin
    apr_k[f'cost_slope_{b}'] = slope
    apr_k[f'cost_fixed_{b}']  = fixed

# ---------- Coal curves (same linear formula as cost curves) ----------
for b in ('A', 'B'):
    coal_pmin = apr_k[f'Coal conversion factor at Pmin [t/MWh]_{b}']  # t/MWh
    coal_pmax = apr_k[f'Coal conversion factor at Pmax [t/MWh]_{b}']  # t/MWh
    pmin      = apr_k[f'Pmin_{b}']
    pmax      = apr_k[f'Pmax_{b}']
    C_coal_min = coal_pmin * pmin          # t/h at Pmin
    C_coal_max = coal_pmax * pmax          # t/h at Pmax
    denom = (pmax - pmin).replace(0.0, np.nan)
    coal_slope = ((C_coal_max - C_coal_min) / denom).fillna(0.0)
    coal_fixed = C_coal_min - coal_slope * pmin
    apr_k[f'coal_slope_t_{b}'] = coal_slope
    apr_k[f'coal_fixed_t_{b}'] = coal_fixed

# ---------- KYOS on/off & run costs ----------
apr_k['on_A'] = (apr_k['K_P_A'] > 1).astype(float)
apr_k['on_B'] = (apr_k['K_P_B'] > 1).astype(float)
apr_k['plant_off'] = ((apr_k['on_A'] + apr_k['on_B']) == 0).astype(float)

apr_k['rc_A'] = apr_k['cost_slope_A'] * apr_k['K_P_A'] + apr_k['cost_fixed_A'] * apr_k['on_A']
apr_k['rc_B'] = apr_k['cost_slope_B'] * apr_k['K_P_B'] + apr_k['cost_fixed_B'] * apr_k['on_B']
apr_k['run_costs_k'] = apr_k['rc_A'] + apr_k['rc_B']

# Coal consumption [t/h]
apr_k['coal_A_k'] = apr_k['coal_slope_t_A'] * apr_k['K_P_A'] + apr_k['coal_fixed_t_A'] * apr_k['on_A']
apr_k['coal_B_k'] = apr_k['coal_slope_t_B'] * apr_k['K_P_B'] + apr_k['coal_fixed_t_B'] * apr_k['on_B']
apr_k['coal_k']   = apr_k['coal_A_k'] + apr_k['coal_B_k']

# Cost per MWh [€/MWh] = run_cost / P  (NaN when off)
apr_k['cost_per_mwh_k_A'] = (apr_k['rc_A'] / apr_k['K_P_A'].replace(0, np.nan))
apr_k['cost_per_mwh_k_B'] = (apr_k['rc_B'] / apr_k['K_P_B'].replace(0, np.nan))

# Spot revenue
apr_k['profit_spot_k'] = apr_k['K_P'] * apr_k['Price']

# OFF costs (DOW=False mode: own consumption + fixed penalty)
apr_k['off_costs_k'] = (apr_k['plant_off'] * OWN_CONSUMPTION * (apr_k['Price'] + apr_k['Grid fee'])
                        + apr_k['plant_off'] * OFFLINE_FIXED_PENALTY_NO_DOW)

# Startups — detect 0→1 transitions in KYOS schedule and compute tiered start costs
# Tier boundaries: very_hot 0-5h, hot 5-10h, warm 10-60h, cold 60-100h, vcold >=100h
# Read actual costs from Starts tab
from schkopau_mtp.data_loader import _read_starts_tab as _rst
_starts_data = _rst()
_tier_costs = {}
for _b in ['A', 'B']:
    _tiers = {t['name']: t for t in _starts_data.get(_b, [])}
    _tier_costs[_b] = {
        'very_hot': _tiers.get('very_hot', {}).get('cost', 0.0),
        'hot':      _tiers.get('hot',      {}).get('cost', 25_510.0),
        'warm':     _tiers.get('warm',     {}).get('cost', 38_291.0),
        'cold':     _tiers.get('cold',     {}).get('cost', 39_910.0),
        'vcold':    _tiers.get('vcold',    {}).get('cost', 60_251.0),
    }

def _classify_start(off_hours):
    """Return tier name given number of consecutive off-hours before startup."""
    if off_hours < 5:
        return 'very_hot'
    elif off_hours < 10:
        return 'hot'
    elif off_hours < 60:
        return 'warm'
    elif off_hours < 100:
        return 'cold'
    else:
        return 'vcold'

for b in ['A', 'B']:
    on = apr_k['on_' + b].reset_index(drop=True)
    su = ((on.diff() > 0) & (on == 1)).astype(float)
    su.iloc[0] = 0.0
    apr_k['su_' + b] = su.values

    # Compute per-startup cost based on downtime duration
    sc = np.zeros(len(apr_k))
    off_count = 0
    for i in range(len(on)):
        if on.iloc[i] < 0.5:
            off_count += 1
        else:
            if su.iloc[i] > 0.5:  # startup event
                tier = _classify_start(off_count)
                sc[i] = _tier_costs[b][tier]
            off_count = 0
    apr_k[f'start_cost_k_{b}'] = sc

n_su_A = int(apr_k['su_A'].sum())
n_su_B = int(apr_k['su_B'].sum())

# Start cost is already computed per event with proper tier classification
kyos_start = apr_k['start_cost_k_A'].sum() + apr_k['start_cost_k_B'].sum()

# PnLs
k_profit = apr_k['profit_spot_k'].sum()
k_run    = apr_k['run_costs_k'].sum()
k_off    = apr_k['off_costs_k'].sum()
k_pnl    = k_profit - k_run - kyos_start - k_off

o_profit = apr_o['profit_spot'].sum() if 'profit_spot' in apr_o.columns else 0
o_run    = apr_o['run_costs'].sum()   if 'run_costs'   in apr_o.columns else 0
o_start  = apr_o['start_cost'].sum()  if 'start_cost'  in apr_o.columns else 0
o_off    = apr_o['OFF_costs'].sum()   if 'OFF_costs'   in apr_o.columns else 0
o_pnl    = apr_o['PnL'].sum()         if 'PnL'         in apr_o.columns else 0

o_starts_A = int((apr_o['startup_A'] > 0.5).sum()) if 'startup_A' in apr_o.columns else 0
o_starts_B = int((apr_o['startup_B'] > 0.5).sum()) if 'startup_B' in apr_o.columns else 0
o_on_A = int((apr_o['on_model_A'] > 0.5).sum()) if 'on_model_A' in apr_o.columns else 0
o_on_B = int((apr_o['on_model_B'] > 0.5).sum()) if 'on_model_B' in apr_o.columns else 0

# ── derived metrics ───────────────────────────────────────────────────────────
k_mwh_A  = apr_k['K_P_A'].sum()
k_mwh_B  = apr_k['K_P_B'].sum()
k_mwh    = apr_k['K_P'].sum()
o_mwh_A  = apr_k['P_eff_A'].sum()
o_mwh_B  = apr_k['P_eff_B'].sum()
o_mwh    = apr_k['P_eff'].sum()

k_coal_A = apr_k['coal_A_k'].sum()
k_coal_B = apr_k['coal_B_k'].sum()
k_coal   = apr_k['coal_k'].sum()
o_coal   = apr_k['Coal_t_h'].sum()

k_rc_A   = apr_k['rc_A'].sum()
k_rc_B   = apr_k['rc_B'].sum()
o_rc_A   = apr_k['run_costs_A'].sum()
o_rc_B   = apr_k['run_costs_B'].sum()

# cost/MWh = total run cost / total MWh (weighted avg over running hours)
k_epm_A  = k_rc_A / k_mwh_A if k_mwh_A else float('nan')
k_epm_B  = k_rc_B / k_mwh_B if k_mwh_B else float('nan')
o_epm_A  = o_rc_A / o_mwh_A if o_mwh_A else float('nan')
o_epm_B  = o_rc_B / o_mwh_B if o_mwh_B else float('nan')

W = 28  # column width

def row(label, k_val, o_val, fmt=',', unit=''):
    kv = f'{k_val:{fmt}}' if isinstance(k_val, (int, float)) else str(k_val)
    ov = f'{o_val:{fmt}}' if isinstance(o_val, (int, float)) else str(o_val)
    dv = ''
    if isinstance(k_val, (int, float)) and isinstance(o_val, (int, float)):
        d = o_val - k_val
        dv = f'{d:+{fmt}}'
    print(f'  {label:<26} {kv:>{W}}  {ov:>{W}}  {dv:>{W}}  {unit}')

def sep():
    print('  ' + '─' * (26 + 3*W + 8))

def hdr(title):
    print(f'\n  {"── " + title + " ":-<{26 + 3*W + 8}}')

print()
print(f'  {"":26} {"KYOS":>{W}}  {"OUR":>{W}}  {"Delta (Ours-KYOS)":>{W}}')
sep()

hdr('P&L  [EUR]')
row('Spot revenue',       k_profit,   o_profit,   ',.0f', 'EUR')
row('Run costs',          k_run,      o_run,       ',.0f', 'EUR')
row('Start costs',        kyos_start, o_start,     ',.0f', 'EUR')
row('OFF costs',          k_off,      o_off,       ',.0f', 'EUR')
sep()
row('PnL',                k_pnl,      o_pnl,       ',.0f', 'EUR')

hdr('Dispatch  [MWh]')
row('Generation A',       k_mwh_A,    o_mwh_A,    ',.0f', 'MWh')
row('Generation B',       k_mwh_B,    o_mwh_B,    ',.0f', 'MWh')
row('Generation total',   k_mwh,      o_mwh,      ',.0f', 'MWh')
row('Hours ON  A',        int(apr_k['on_A'].sum()),  o_on_A,    'd',    'h')
row('Hours ON  B',        int(apr_k['on_B'].sum()),  o_on_B,    'd',    'h')
row('Hours OFF (plant)',  int(apr_k['plant_off'].sum()), int(apr_k['plant_off'].sum()) - (o_on_A + o_on_B - int(apr_k['on_A'].sum()) - int(apr_k['on_B'].sum())), 'd', 'h')
row('Starts    A',        n_su_A,      o_starts_A, 'd',    '')
row('Starts    B',        n_su_B,      o_starts_B, 'd',    '')

hdr('Run costs  [EUR/MWh]')
row('Cost/MWh  A',        k_epm_A,    o_epm_A,    ',.2f', 'EUR/MWh')
row('Cost/MWh  B',        k_epm_B,    o_epm_B,    ',.2f', 'EUR/MWh')

hdr('Coal consumption  [t]')
row('Coal  A',            k_coal_A,   float('nan'), ',.0f', 't')
row('Coal  B',            k_coal_B,   float('nan'), ',.0f', 't')
row('Coal  total',        k_coal,     o_coal,       ',.0f', 't')
sep()
print()

# ─── Export to Excel ────────────────────────────────────────────────────────
out_file = folder + r'\KYOS_compare_April2026.xlsx'

# start_cost_k_A and start_cost_k_B already computed above (per-event tiered costs)
apr_k['start_cost_k']   = apr_k['start_cost_k_A'] + apr_k['start_cost_k_B']

# KYOS cumulative PnL (hourly)
apr_k['pnl_k_h'] = (apr_k['profit_spot_k'] - apr_k['run_costs_k']
                    - apr_k['start_cost_k']  - apr_k['off_costs_k'])

# Columns to export — KYOS schedule
kyos_cols = {
    'Date':              apr_k['Date'],
    'Price [€/MWh]':    apr_k['Price'],
    'MC_A [€/MWh]':     apr_k['MC_A'],
    'MC_B [€/MWh]':     apr_k['MC_B'],
    'Pmin_A [MW]':                      apr_k['Pmin_A'],
    'Pmax_A [MW]':                      apr_k['Pmax_A'],
    'Pmin_B [MW]':                      apr_k['Pmin_B'],
    'Pmax_B [MW]':                      apr_k['Pmax_B'],
    'Coal_fac_Pmin_A [t/MWh]':          apr_k['Coal conversion factor at Pmin [t/MWh]_A'],
    'Coal_fac_Pmax_A [t/MWh]':          apr_k['Coal conversion factor at Pmax [t/MWh]_A'],
    'Coal_fac_Pmin_B [t/MWh]':          apr_k['Coal conversion factor at Pmin [t/MWh]_B'],
    'Coal_fac_Pmax_B [t/MWh]':          apr_k['Coal conversion factor at Pmax [t/MWh]_B'],
    'coal_slope_A [t/MWh]':             apr_k['coal_slope_t_A'],
    'coal_fixed_A [t/h]':               apr_k['coal_fixed_t_A'],
    'coal_slope_B [t/MWh]':             apr_k['coal_slope_t_B'],
    'coal_fixed_B [t/h]':               apr_k['coal_fixed_t_B'],
    # ── KYOS dispatch ──
    'KYOS_P_A [MW]':     apr_k['K_P_A'],
    'KYOS_P_B [MW]':     apr_k['K_P_B'],
    'KYOS_P [MW]':       apr_k['K_P'],
    'KYOS_on_A':         apr_k['on_A'],
    'KYOS_on_B':         apr_k['on_B'],
    'KYOS_plant_off':    apr_k['plant_off'],
    'KYOS_start_A':      apr_k['su_A'],
    'KYOS_start_B':      apr_k['su_B'],
    'KYOS_spot_rev [€]': apr_k['profit_spot_k'],
    'KYOS_run_cost [€]': apr_k['run_costs_k'],
    'KYOS_run_cost_A[€]':apr_k['rc_A'],
    'KYOS_run_cost_B[€]':apr_k['rc_B'],
    'KYOS_start_cost[€]':apr_k['start_cost_k'],
    'KYOS_off_cost [€]': apr_k['off_costs_k'],
    'KYOS_PnL_h [€]':    apr_k['pnl_k_h'],
    'KYOS_coal_A [t/h]': apr_k['coal_A_k'],
    'KYOS_coal_B [t/h]': apr_k['coal_B_k'],
    'KYOS_coal [t/h]':   apr_k['coal_k'],
    'KYOS_€/MWh_A':      apr_k['cost_per_mwh_k_A'],
    'KYOS_€/MWh_B':      apr_k['cost_per_mwh_k_B'],
    # ── Our dispatch ──
    'OUR_P_A [MW]':      apr_k['P_eff_A'],
    'OUR_P_B [MW]':      apr_k['P_eff_B'],
    'OUR_P [MW]':        apr_k['P_eff'],
    'OUR_on_A':          apr_k['on_model_A'],
    'OUR_on_B':          apr_k['on_model_B'],
    'OUR_start_A':       apr_k['startup_A'],
    'OUR_start_B':       apr_k['startup_B'],
    'OUR_spot_rev [€]':  apr_k['profit_spot'],
    'OUR_run_cost [€]':  apr_k['run_costs'],
    'OUR_run_cost_A[€]': apr_k['run_costs_A'],
    'OUR_run_cost_B[€]': apr_k['run_costs_B'],
    'OUR_start_cost[€]': apr_k['start_cost'],
    'OUR_off_cost [€]':  apr_k['OFF_costs'],
    'OUR_PnL_h [€]':     apr_k['PnL'],
    'OUR_coal_A [t/h]':  apr_k['Coal_kMT_h_Merchant'] * 1000,
    'OUR_coal [t/h]':    apr_k['Coal_t_h'],
    'OUR_€/MWh_A':       (apr_k['run_costs_A'] / apr_k['P_eff_A'].replace(0, np.nan)),
    'OUR_€/MWh_B':       (apr_k['run_costs_B'] / apr_k['P_eff_B'].replace(0, np.nan)),
}

export = pd.DataFrame(kyos_cols)

# Summary row
summary = {
    'Date': 'TOTAL',
    'Price [€/MWh]': '',
    'MC_A [€/MWh]': '',
    'MC_B [€/MWh]': '',
    'Pmin_A [MW]': '',
    'Pmax_A [MW]': '',
    'Pmin_B [MW]': '',
    'Pmax_B [MW]': '',
    'Coal_fac_Pmin_A [t/MWh]': '',
    'Coal_fac_Pmax_A [t/MWh]': '',
    'Coal_fac_Pmin_B [t/MWh]': '',
    'Coal_fac_Pmax_B [t/MWh]': '',
    'coal_slope_A [t/MWh]': '',
    'coal_fixed_A [t/h]': '',
    'coal_slope_B [t/MWh]': '',
    'coal_fixed_B [t/h]': '',
    'KYOS_P_A [MW]': '',
    'KYOS_P_B [MW]': '',
    'KYOS_P [MW]': '',
    'KYOS_on_A':       int(apr_k['on_A'].sum()),
    'KYOS_on_B':       int(apr_k['on_B'].sum()),
    'KYOS_plant_off':  int(apr_k['plant_off'].sum()),
    'KYOS_start_A':    n_su_A,
    'KYOS_start_B':    n_su_B,
    'KYOS_spot_rev [€]': k_profit,
    'KYOS_run_cost [€]': k_run,
    'KYOS_run_cost_A[€]': apr_k['rc_A'].sum(),
    'KYOS_run_cost_B[€]': apr_k['rc_B'].sum(),
    'KYOS_start_cost[€]': kyos_start,
    'KYOS_off_cost [€]':  k_off,
    'KYOS_PnL_h [€]':     k_pnl,
    'KYOS_coal_A [t/h]': apr_k['coal_A_k'].sum(),
    'KYOS_coal_B [t/h]': apr_k['coal_B_k'].sum(),
    'KYOS_coal [t/h]':   apr_k['coal_k'].sum(),
    'KYOS_€/MWh_A':      apr_k['rc_A'].sum() / apr_k['K_P_A'].replace(0, np.nan).sum(),
    'KYOS_€/MWh_B':      apr_k['rc_B'].sum() / apr_k['K_P_B'].replace(0, np.nan).sum(),
    'OUR_P_A [MW]': '',
    'OUR_P_B [MW]': '',
    'OUR_P [MW]': '',
    'OUR_P_B [MW]': '',
    'OUR_P [MW]': '',
    'OUR_on_A':        o_on_A,
    'OUR_on_B':        o_on_B,
    'OUR_start_A':     o_starts_A,
    'OUR_start_B':     o_starts_B,
    'OUR_spot_rev [€]': o_profit,
    'OUR_run_cost [€]': o_run,
    'OUR_run_cost_A[€]': apr_k['run_costs_A'].sum(),
    'OUR_run_cost_B[€]': apr_k['run_costs_B'].sum(),
    'OUR_start_cost[€]': o_start,
    'OUR_off_cost [€]':  o_off,
    'OUR_PnL_h [€]':     o_pnl,
    'OUR_coal_A [t/h]':  apr_k['Coal_kMT_h_Merchant'].sum() * 1000,
    'OUR_coal [t/h]':    apr_k['Coal_t_h'].sum(),
    'OUR_€/MWh_A':       apr_k['run_costs_A'].sum() / apr_k['P_eff_A'].replace(0, np.nan).sum(),
    'OUR_€/MWh_B':       apr_k['run_costs_B'].sum() / apr_k['P_eff_B'].replace(0, np.nan).sum(),
}
export = pd.concat([export, pd.DataFrame([summary])], ignore_index=True)

# ── Coal daily sheet ─────────────────────────────────────────────────────────
apr_k['Day'] = apr_k['Date'].dt.normalize()
coal_daily = apr_k.groupby('Day').agg(
    Price_avg       = ('Price',     'mean'),
    KYOS_coal_A_t   = ('coal_A_k',  'sum'),
    KYOS_coal_B_t   = ('coal_B_k',  'sum'),
    KYOS_coal_t     = ('coal_k',    'sum'),
    KYOS_gen_A_MWh  = ('K_P_A',     'sum'),
    KYOS_gen_B_MWh  = ('K_P_B',     'sum'),
    KYOS_gen_MWh    = ('K_P',       'sum'),
    OUR_coal_t      = ('Coal_t_h',  'sum'),
    OUR_gen_A_MWh   = ('P_eff_A',   'sum'),
    OUR_gen_B_MWh   = ('P_eff_B',   'sum'),
    OUR_gen_MWh     = ('P_eff',     'sum'),
).reset_index()
coal_daily['Delta_coal_t']   = coal_daily['OUR_coal_t']  - coal_daily['KYOS_coal_t']
coal_daily['Delta_gen_MWh']  = coal_daily['OUR_gen_MWh'] - coal_daily['KYOS_gen_MWh']
# coal intensity t/MWh
coal_daily['KYOS_t_per_MWh'] = coal_daily['KYOS_coal_t'] / coal_daily['KYOS_gen_MWh'].replace(0, np.nan)
coal_daily['OUR_t_per_MWh']  = coal_daily['OUR_coal_t']  / coal_daily['OUR_gen_MWh'].replace(0, np.nan)

# totals row
totals = {c: coal_daily[c].sum() if coal_daily[c].dtype.kind in 'fiu' else 'TOTAL'
          for c in coal_daily.columns}
totals['Day'] = 'TOTAL'
totals['Price_avg'] = coal_daily['Price_avg'].mean()
totals['KYOS_t_per_MWh'] = coal_daily['KYOS_coal_t'].sum() / coal_daily['KYOS_gen_MWh'].sum()
totals['OUR_t_per_MWh']  = coal_daily['OUR_coal_t'].sum()  / coal_daily['OUR_gen_MWh'].sum()
coal_daily = pd.concat([coal_daily, pd.DataFrame([totals])], ignore_index=True)

def _autowidth(ws):
    for col in ws.columns:
        max_len = max((len(str(cell.value)) for cell in col if cell.value is not None), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 24)

with pd.ExcelWriter(out_file, engine='openpyxl') as writer:
    export.to_excel(writer, sheet_name='KYOS_vs_Ours', index=False)
    ws = writer.sheets['KYOS_vs_Ours']
    ws.freeze_panes = 'B2'
    _autowidth(ws)

    coal_daily.to_excel(writer, sheet_name='Coal_daily', index=False)
    wc = writer.sheets['Coal_daily']
    wc.freeze_panes = 'B2'
    _autowidth(wc)

print(f'\nExported: {out_file}')
