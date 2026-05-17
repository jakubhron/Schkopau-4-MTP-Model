"""
Show the difference between cf_Pmax and coal_slope for a specific hour.
Demonstrates why (Price - MC) / cf_Pmax != actual shadow price.
"""
import pandas as pd
import numpy as np

dr = pd.read_parquet(r'_solver_cache/cache_chrono_AP1_cache_v3_2026-04-24_13-58.parquet')
jul = dr[dr['month_num'] == 7].copy()

# Pick July 5 h0 (the marginal hour from LP simulation) and July 1 h7
examples = jul[
    ((jul['Date'].dt.day == 5) & (jul['Hour'] == 0)) |
    ((jul['Date'].dt.day == 1) & (jul['Hour'] == 7))
]

print("="*80)
print("cf_Pmax vs coal_slope: why the shadow price formula differs")
print("="*80)

for _, row in examples.iterrows():
    date = str(row['Date'])[:10]
    hour = int(row['Hour'])
    price = float(row['Price'])

    for blk in ['A', 'B']:
        if int(row[f'on_model_{blk}']) != 1:
            continue

        pmin = float(row[f'Pmin_{blk}'])
        pmax = float(row[f'Pmax_{blk}'])
        mc = float(row[f'MC_{blk}'])
        cf_pmax = float(row[f'Coal conversion factor at Pmax [t/MWh]_{blk}'])
        cf_pmin = float(row[f'Coal conversion factor at Pmin [t/MWh]_{blk}'])

        # Coal at Pmin, Coal at Pmax [t/h]
        coal_at_pmin = cf_pmin * pmin
        coal_at_pmax = cf_pmax * pmax

        # coal_slope = (C_max - C_min) / (Pmax - Pmin)  [t/MWh]
        coal_slope = (coal_at_pmax - coal_at_pmin) / (pmax - pmin) if pmax > pmin else 0

        # coal_fixed = C_min - coal_slope * Pmin  [t/h when ON]
        coal_fixed = coal_at_pmin - coal_slope * pmin

        # In the coal constraint: coal = coal_slope * P_eff + coal_fixed * on
        # Marginal coal per extra MWh = coal_slope (NOT cf_Pmax!)

        # My simplified merit order used: (Price - MC) / cf_Pmax
        merit_simple = (price - mc) / cf_pmax if cf_pmax > 0 else 0

        # Correct formula for shadow: (Price - MC) / coal_slope
        # But wait — the objective uses different cost coefficients than the coal constraint
        # The shadow = objective_margin_per_MWh / coal_slope_per_MWh
        # where objective_margin = Price - (cost_slope * P + cost_fixed * on)'s marginal = Price - cost_slope
        # Let's use Price - MC as approximation for now
        merit_correct = (price - mc) / coal_slope if coal_slope > 0 else 0

        p_eff = float(row[f'P_eff_{blk}'])

        print(f"\n{date} h{hour} Block {blk} (Price={price:.1f}, MC={mc:.1f}, P_eff={p_eff:.0f}MW)")
        print(f"  Pmin={pmin:.0f}MW  Pmax={pmax:.0f}MW")
        print(f"  cf_Pmin  = {cf_pmin:.6f} t/MWh  →  Coal@Pmin = {coal_at_pmin:.1f} t/h")
        print(f"  cf_Pmax  = {cf_pmax:.6f} t/MWh  →  Coal@Pmax = {coal_at_pmax:.1f} t/h")
        print(f"  coal_slope = ({coal_at_pmax:.1f} - {coal_at_pmin:.1f}) / "
              f"({pmax:.0f} - {pmin:.0f}) = {coal_slope:.6f} t/MWh")
        print(f"  coal_fixed = {coal_fixed:.2f} t/h")
        print()
        print(f"  Simplified: (Price-MC) / cf_Pmax = ({price:.1f}-{mc:.1f}) / {cf_pmax:.4f}"
              f" = {merit_simple:.1f} EUR/t")
        print(f"  Correct:    (Price-MC) / coal_slope = ({price:.1f}-{mc:.1f}) / {coal_slope:.4f}"
              f" = {merit_correct:.1f} EUR/t")
        print(f"  Ratio: coal_slope / cf_Pmax = {coal_slope / cf_pmax:.3f}")
        print(f"  → coal_slope is {coal_slope/cf_pmax:.1f}x cf_Pmax "
              f"({'higher' if coal_slope > cf_pmax else 'lower'})")

# Now show the corrected merit order cutoff for July
print()
print("="*80)
print("CORRECTED MERIT ORDER (using coal_slope)")
print("="*80)

records = []
for idx, row in jul.iterrows():
    for blk in ['A', 'B']:
        on = int(row[f'on_model_{blk}'])
        if on != 1:
            continue
        price = float(row['Price'])
        mc = float(row[f'MC_{blk}'])
        cf_pmax = float(row[f'Coal conversion factor at Pmax [t/MWh]_{blk}'])
        cf_pmin = float(row[f'Coal conversion factor at Pmin [t/MWh]_{blk}'])
        pmin = float(row[f'Pmin_{blk}'])
        pmax = float(row[f'Pmax_{blk}'])

        coal_at_pmin = cf_pmin * pmin
        coal_at_pmax = cf_pmax * pmax
        coal_slope = (coal_at_pmax - coal_at_pmin) / (pmax - pmin) if pmax > pmin else 0

        margin_correct = (price - mc) / coal_slope if coal_slope > 0 else 0
        margin_simple = (price - mc) / cf_pmax if cf_pmax > 0 else 0
        extra_coal = (pmax - pmin) * coal_slope  # correct extra coal

        records.append(dict(
            blk=blk, hour=int(row['Hour']),
            date=str(row['Date'])[:10],
            price=price, mc=mc,
            margin_simple=margin_simple,
            margin_correct=margin_correct,
            coal_slope=coal_slope,
            cf_pmax=cf_pmax,
            extra_coal=extra_coal,
            pmin=pmin, pmax=pmax,
        ))

df = pd.DataFrame(records)
df = df.sort_values('margin_correct', ascending=False).reset_index(drop=True)

total_coal = jul['coal_exact'].sum()
# Coal at pmin using coal_slope formula: coal_fixed * on (= coal_at_pmin per hour)
total_pmin_coal_correct = sum(
    float(r[f'Coal conversion factor at Pmin [t/MWh]_{blk}']) * float(r[f'Pmin_{blk}'])
    for _, r in jul.iterrows() for blk in ['A', 'B']
    if int(r[f'on_model_{blk}']) == 1
)
budget = total_coal - total_pmin_coal_correct

cum = 0.0
cutoff_rank = len(df)
cutoff_margin = 0
for i, (_, r) in enumerate(df.iterrows()):
    cum += r['extra_coal']
    if cum >= budget:
        cutoff_rank = i + 1
        cutoff_margin = r['margin_correct']
        cutoff_simple = r['margin_simple']
        cutoff_price = r['price']
        break

print(f"\nJuly with CORRECT coal_slope:")
print(f"  Budget above Pmin: {budget:,.0f} t")
print(f"  Cutoff at rank {cutoff_rank}/{len(df)}")
print(f"  Cutoff margin (correct): {cutoff_margin:.2f} EUR/t  ← closer to MOSEK's 4.89")
print(f"  Cutoff margin (simple):  {cutoff_simple:.2f} EUR/t")
print(f"  Cutoff price: {cutoff_price:.1f} EUR/MWh")

# Show 5 hours around cutoff
print(f"\nHours around cutoff:")
start = max(0, cutoff_rank - 5)
end = min(len(df), cutoff_rank + 5)
for i in range(start, end):
    r = df.iloc[i]
    marker = " <<<< MARGINAL" if i + 1 == cutoff_rank else ""
    assign = "Pmax" if i + 1 < cutoff_rank else ("MARGINAL" if i + 1 == cutoff_rank else "Pmin")
    print(f"  {i+1:>4d}  {r['blk']}  {r['date']} h{r['hour']:>2d}  "
          f"Price={r['price']:>6.1f}  MC={r['mc']:.1f}  "
          f"correct={r['margin_correct']:>6.1f}  simple={r['margin_simple']:>6.1f}  "
          f"-> {assign}{marker}")
