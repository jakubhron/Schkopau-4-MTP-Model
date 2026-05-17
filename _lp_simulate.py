"""Simulate LP re-solve: what happens when P is re-optimized with fixed on/off?"""
import pandas as pd
import numpy as np

dr = pd.read_parquet(r'_solver_cache/cache_chrono_AP1_cache_v3_2026-04-24_13-58.parquet')
jul = dr[dr['month_num'] == 7].copy()

# For each ON hour, compute margin per ton of coal
records = []
for idx, row in jul.iterrows():
    for blk in ['A', 'B']:
        on = int(row[f'on_model_{blk}'])
        if on != 1:
            continue
        price = float(row['Price'])
        mc = float(row[f'MC_{blk}'])
        cf = float(row[f'Coal conversion factor at Pmax [t/MWh]_{blk}'])
        pmin = float(row[f'Pmin_{blk}'])
        pmax = float(row[f'Pmax_{blk}'])
        p_mip = float(row[f'P_eff_{blk}'])
        margin_per_ton = (price - mc) / cf if cf > 0 else 0
        records.append(dict(
            blk=blk, hour=int(row['Hour']),
            date=str(row['Date'])[:10],
            price=price, mc=mc, cf=cf,
            pmin=pmin, pmax=pmax, p_mip=p_mip,
            margin_per_ton=margin_per_ton,
        ))

df = pd.DataFrame(records)
df = df.sort_values('margin_per_ton', ascending=False).reset_index(drop=True)

df['coal_if_pmax'] = df['pmax'] * df['cf']
df['coal_if_pmin'] = df['pmin'] * df['cf']
df['extra_coal_for_pmax'] = df['coal_if_pmax'] - df['coal_if_pmin']

# Total coal budget = actual coal consumed in MIP solution
total_coal_budget = jul['coal_exact'].sum()
total_coal_at_pmin = df['coal_if_pmin'].sum()
total_coal_at_pmax = df['coal_if_pmax'].sum()
coal_above_pmin = total_coal_budget - total_coal_at_pmin

print(f"Total coal budget (from MIP): {total_coal_budget:,.0f} t")
print(f"Coal if ALL ON hours at Pmin: {total_coal_at_pmin:,.0f} t")
print(f"Coal if ALL ON hours at Pmax: {total_coal_at_pmax:,.0f} t")
print(f"Available above Pmin: {coal_above_pmin:,.0f} t")
print(f"Total ON block-hours: {len(df)}")
print()

# LP greedy: give Pmax to highest-margin hours first, Pmin to lowest
remaining = coal_above_pmin
assignments = []

for i, row in df.iterrows():
    extra_needed = row['extra_coal_for_pmax']
    if remaining >= extra_needed:
        assignments.append('Pmax')
        remaining -= extra_needed
    elif remaining > 0:
        frac = remaining / extra_needed
        p_level = row['pmin'] + frac * (row['pmax'] - row['pmin'])
        assignments.append(f'Interior({p_level:.0f}MW)')
        remaining = 0
    else:
        assignments.append('Pmin')

df['lp_assignment'] = assignments

pmax_count = sum(1 for a in assignments if a == 'Pmax')
interior_count = sum(1 for a in assignments if a.startswith('Interior'))
pmin_count = sum(1 for a in assignments if a == 'Pmin')

print(f"LP re-solve allocation:")
print(f"  At Pmax: {pmax_count} block-hours")
print(f"  Interior: {interior_count} block-hours")
print(f"  At Pmin: {pmin_count} block-hours")
print()

# Find marginal hour
interior_rows = df[df['lp_assignment'].str.startswith('Interior')]
if len(interior_rows) > 0:
    marg = interior_rows.iloc[0]
    print(f"MARGINAL HOUR: {marg['blk']} on {marg['date']} h{marg['hour']}")
    print(f"  Price={marg['price']:.1f}  MC={marg['mc']:.1f}")
    print(f"  margin = {marg['margin_per_ton']:.2f} EUR/t  ← THIS is the shadow price")
    print(f"  LP assigned: {marg['lp_assignment']}")
    print()

# Where does July 1 hour 7 end up?
jul1_h7 = df[(df['date'] == '2026-07-01') & (df['hour'] == 7)]
print("July 1 hour 7 in LP allocation:")
for _, r in jul1_h7.iterrows():
    print(f"  {r['blk']}: margin={r['margin_per_ton']:.1f} EUR/t  "
          f"MIP_P={r['p_mip']:.0f}  LP_assignment={r['lp_assignment']}")
print()

# Show 10 hours around the Pmax/Pmin boundary
print("Hours around the LP cutoff (margin boundary):")
boundary_idx = None
for i, a in enumerate(assignments):
    if a == 'Pmin':
        boundary_idx = i
        break

if boundary_idx is not None:
    start = max(0, boundary_idx - 5)
    end = min(len(df), boundary_idx + 5)
    for i in range(start, end):
        r = df.iloc[i]
        marker = " <<<< MARGINAL" if r['lp_assignment'].startswith('Interior') else ""
        print(f"  {r['blk']} {r['date']} h{r['hour']:>2d}  "
              f"Price={r['price']:>6.1f}  MC={r['mc']:.1f}  "
              f"margin={r['margin_per_ton']:>6.1f} EUR/t  -> {r['lp_assignment']}{marker}")
