import pandas as pd, numpy as np, shutil, tempfile, os
folder = r'C:\Users\jakub.hron\OneDrive - EP Commodities, a.s\Documents\Schkopau_mtp\Inputs_EOD_20_04_2026'
kyos_file = folder + r'\KYOS.xlsx'
our_file  = folder + r'\Data1 EPNLintr26 High availability_20_04_2026_results_restricted_2026-04-21_10-14.xlsx'
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
tmp_fd, tmp = tempfile.mkstemp(suffix='.xlsx'); os.close(tmp_fd)
shutil.copy2(our_file, tmp)
res = pd.read_excel(tmp, sheet_name='Results')
os.unlink(tmp)
res['Date'] = pd.to_datetime(res['Date']) + pd.to_timedelta(res['Hour'], unit='h')
merged = kyos.merge(res, on='Date', how='left')
apr = merged[(merged['Date'] >= '2026-04-20') & (merged['Date'].dt.month==4) & (merged['Date'].dt.year==2026)].copy()

# --- Block B cost/MWh deep dive ---
# KYOS side
k_on_B = (apr['K_P_B'] > 1).astype(float)
k_hours_B = int(k_on_B.sum())
k_mwh_B = apr['K_P_B'].sum()
k_avg_P_B = apr.loc[k_on_B==1, 'K_P_B'].mean()

# OUR side
o_on_B = (apr['on_model_B'] > 0.5).astype(float)
o_hours_B = int(o_on_B.sum())
o_mwh_B = apr['P_eff_B'].sum()
o_avg_P_B = apr.loc[o_on_B==1, 'P_eff_B'].mean()

# Cost curves
tc_pmin_B = apr['Total generation costs at Pmin [€/MWh]_B']
tc_pmax_B = apr['Total generation costs at Pmax [€/MWh]_B']
pmin_B = apr['Pmin_B']
pmax_B = apr['Pmax_B']
Cmin_B = tc_pmin_B * pmin_B
Cmax_B = tc_pmax_B * pmax_B
denom = (pmax_B - pmin_B).replace(0.0, np.nan)
slope_B = ((Cmax_B - Cmin_B) / denom).fillna(0.0)
fixed_B = Cmin_B - slope_B * pmin_B

k_rc_B = (slope_B * apr['K_P_B'] + fixed_B * k_on_B).sum()
o_rc_B = apr['run_costs_B'].sum()

print("=== Block B Cost/MWh breakdown ===")
print(f"  KYOS hours ON B:    {k_hours_B}")
print(f"  OUR  hours ON B:    {o_hours_B}")
print(f"  KYOS total MWh B:   {k_mwh_B:,.1f}")
print(f"  OUR  total MWh B:   {o_mwh_B:,.1f}")
print(f"  KYOS avg P when ON: {k_avg_P_B:,.1f} MW")
print(f"  OUR  avg P when ON: {o_avg_P_B:,.1f} MW")
print(f"  KYOS run cost B:    {k_rc_B:,.0f} EUR")
print(f"  OUR  run cost B:    {o_rc_B:,.0f} EUR")
print(f"  KYOS cost/MWh B:    {k_rc_B/k_mwh_B:,.2f} EUR/MWh")
print(f"  OUR  cost/MWh B:    {o_rc_B/o_mwh_B:,.2f} EUR/MWh")
print()

# Show cost curve: cost(P) = slope*P + fixed  -> cost/MWh = slope + fixed/P
# Higher P -> fixed cost spread over more MWh -> lower cost/MWh
avg_slope = slope_B.mean()
avg_fixed = fixed_B.mean()
print(f"  Avg cost slope B:   {avg_slope:,.2f} EUR/MWh (marginal)")
print(f"  Avg cost fixed B:   {avg_fixed:,.0f} EUR/h")
print(f"  -> cost/MWh at {k_avg_P_B:.0f} MW (KYOS avg): {avg_slope + avg_fixed/k_avg_P_B:.2f}")
print(f"  -> cost/MWh at {o_avg_P_B:.0f} MW (OUR avg):  {avg_slope + avg_fixed/o_avg_P_B:.2f}")
print()

# Compare hourly: when both are on, how do power levels differ?
both_on = (k_on_B == 1) & (o_on_B == 1)
print(f"  Hours both ON:       {int(both_on.sum())}")
if both_on.any():
    print(f"  KYOS avg P (both on): {apr.loc[both_on, 'K_P_B'].mean():.1f} MW")
    print(f"  OUR  avg P (both on): {apr.loc[both_on, 'P_eff_B'].mean():.1f} MW")
    diff = apr.loc[both_on, 'P_eff_B'] - apr.loc[both_on, 'K_P_B']
    print(f"  Avg P diff (Ours-KYOS): {diff.mean():+.1f} MW")

# Hours KYOS on but OUR off, and vice versa
k_on_o_off = (k_on_B == 1) & (o_on_B == 0)
o_on_k_off = (o_on_B == 1) & (k_on_B == 0)
print(f"  Hours KYOS on, OUR off: {int(k_on_o_off.sum())}")
print(f"  Hours OUR on, KYOS off: {int(o_on_k_off.sum())}")
if o_on_k_off.any():
    print(f"    OUR P when KYOS off:  {apr.loc[o_on_k_off, 'P_eff_B'].mean():.1f} MW (typically Pmin -> expensive/MWh)")

