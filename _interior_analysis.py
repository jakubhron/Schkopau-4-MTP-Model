"""Find interior P_eff hours to identify the marginal hour for shadow price."""
import pandas as pd

dr = pd.read_parquet(r'_solver_cache/cache_chrono_AP1_cache_v3_2026-04-24_13-58.parquet')
jul = dr[dr['month_num'] == 7]

print("Looking for INTERIOR hours in July MIP solution (Pmin < P_eff < Pmax, on=1):")
print()

interior = []
pmin_hours = []
pmax_hours = []

for idx, row in jul.iterrows():
    for blk in ['A', 'B']:
        on = int(row[f'on_model_{blk}'])
        if on != 1:
            continue
        pmin = float(row[f'Pmin_{blk}'])
        pmax = float(row[f'Pmax_{blk}'])
        p_eff = float(row[f'P_eff_{blk}'])
        price = float(row['Price'])
        cost_slope = float(row[f'cost_slope_{blk}'])
        coal_slope = float(row[f'coal_slope_{blk}'])
        margin = (price - cost_slope) / coal_slope

        rec = dict(
            blk=blk, date=str(row['Date'])[:10],
            hour=int(row['Hour']),
            price=price, p_eff=p_eff, pmin=pmin, pmax=pmax,
            margin=margin, cost_slope=cost_slope, coal_slope=coal_slope,
        )

        if p_eff > pmin + 1 and p_eff < pmax - 1:
            interior.append(rec)
        elif abs(p_eff - pmin) < 2:
            pmin_hours.append(rec)
        else:
            pmax_hours.append(rec)

print(f"Hours at Pmax: {len(pmax_hours)}")
print(f"Hours at Pmin: {len(pmin_hours)}")
print(f"Interior hours: {len(interior)}")
print()

if interior:
    idf = pd.DataFrame(interior).sort_values(['date', 'hour'])
    print(f"Found {len(idf)} interior hours:")
    for _, r in idf.iterrows():
        print(f"  {r['blk']}  {r['date']} h{r['hour']:2d}  "
              f"Price={r['price']:6.1f}  P_eff={r['p_eff']:5.0f}  "
              f"Pmin={r['pmin']:3.0f}  Pmax={r['pmax']:3.0f}  "
              f"margin={r['margin']:6.2f} EUR/t")
    print()
    print("If no ramp interactions, shadow ≈ margin at these interior hours.")
    print("MOSEK reports: 4.89 EUR/t")
else:
    print("NO interior hours in MIP — all at Pmin or Pmax!")
    print("The LP re-solve will create interior hours by relaxing the constraint.")
    print()

# What margin corresponds to shadow = 4.89?
for blk, cs, csl in [('A', 86.82, 0.9023), ('B', 79.26, 0.9064)]:
    price_at_shadow = cs + 4.89 * csl
    print(f"Block {blk}: shadow=4.89 → Price = {cs} + 4.89*{csl:.4f} = {price_at_shadow:.1f} EUR/MWh")

# Are there hours near that price?
print()
print("Hours near those prices that are ON:")
for idx, row in jul.iterrows():
    price = float(row['Price'])
    for blk in ['A', 'B']:
        on = int(row[f'on_model_{blk}'])
        if on != 1:
            continue
        cs = float(row[f'cost_slope_{blk}'])
        csl = float(row[f'coal_slope_{blk}'])
        target = cs + 4.89 * csl
        if abs(price - target) < 2:
            p_eff = float(row[f'P_eff_{blk}'])
            pmin = float(row[f'Pmin_{blk}'])
            pmax = float(row[f'Pmax_{blk}'])
            margin = (price - cs) / csl
            print(f"  {blk}  {str(row['Date'])[:10]} h{int(row['Hour']):2d}  "
                  f"Price={price:6.1f}  target={target:.1f}  P_eff={p_eff:5.0f}  "
                  f"Pmin={pmin:3.0f}  Pmax={pmax:3.0f}  margin={margin:6.2f}")

# Also: show the coal budget properly
# The coal limit must come from the Excel file
# Let's compute what coal limit would make the shadow = 4.89
# i.e., what budget makes the marginal hour have margin = 4.89

print()
print("=" * 70)
print("REVERSE ENGINEERING: what coal limit gives shadow = 4.89?")
print("=" * 70)

records = []
for idx, row in jul.iterrows():
    for blk in ['A', 'B']:
        on = int(row[f'on_model_{blk}'])
        if on != 1:
            continue
        pmin = float(row[f'Pmin_{blk}'])
        pmax = float(row[f'Pmax_{blk}'])
        price = float(row['Price'])
        cost_slope = float(row[f'cost_slope_{blk}'])
        coal_slope = float(row[f'coal_slope_{blk}'])
        coal_fixed = float(row[f'coal_fixed_{blk}'])
        both_on = int(row[f'on_model_{"B" if blk == "A" else "A"}'])
        duo_adj = float(row.get(f'duo_coal_adj_{blk}', 0.0)) if both_on else 0.0

        margin = (price - cost_slope) / coal_slope
        coal_pmin = coal_slope * pmin + coal_fixed + duo_adj
        extra_coal = (pmax - pmin) * coal_slope

        records.append(dict(
            margin=margin, coal_pmin=coal_pmin, extra_coal=extra_coal,
            blk=blk, price=price,
        ))

df = pd.DataFrame(records).sort_values('margin', ascending=False).reset_index(drop=True)

# Total coal at Pmin (base cost)
base_coal = df['coal_pmin'].sum()

# Find where margin crosses 4.89
cum = 0.0
for i, row in df.iterrows():
    if row['margin'] <= 4.89:
        needed_limit = base_coal + cum
        print(f"At margin=4.89, need coal limit = {base_coal:.0f} (base) + {cum:.0f} (upscale) = {needed_limit:.0f} tonnes")
        print(f"  = {needed_limit / 1000:.1f} kt")
        break
    cum += row['extra_coal']

# What if margin crosses 15.4?
cum2 = 0.0
for i, row in df.iterrows():
    if row['margin'] <= 15.4:
        needed_limit2 = base_coal + cum2
        print(f"At margin=15.4, need coal limit = {base_coal:.0f} (base) + {cum2:.0f} (upscale) = {needed_limit2:.0f} tonnes")
        print(f"  = {needed_limit2 / 1000:.1f} kt")
        break
    cum2 += row['extra_coal']

# What's the TOTAL coal if all at Pmax?
total_at_pmax = sum(r['coal_pmin'] + r['extra_coal'] for _, r in df.iterrows())
print(f"\nTotal coal if all at Pmax: {total_at_pmax:.0f} tonnes = {total_at_pmax/1000:.1f} kt")
print(f"Total coal from parquet (actual consumption): {jul['coal_exact'].sum():.0f} tonnes = {jul['coal_exact'].sum()/1000:.1f} kt")
print(f"Base coal (all at Pmin): {base_coal:.0f} tonnes = {base_coal/1000:.1f} kt")
