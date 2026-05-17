"""Compute shadow price curve for July at different coal budget levels."""
import pandas as pd
import numpy as np

dr = pd.read_parquet(r'_solver_cache/cache_chrono_AP1_cache_v3_2026-04-24_13-58.parquet')
jul = dr[dr['month_num'] == 7]

# Build merit order: all ON hours, ranked by (Price - cost_slope) / coal_slope
records = []
for idx, row in jul.iterrows():
    for blk in ['A', 'B']:
        on = int(row[f'on_model_{blk}'])
        if on != 1:
            continue
        other_blk = 'B' if blk == 'A' else 'A'
        both_on = int(row[f'on_model_{other_blk}'])

        pmin = float(row[f'Pmin_{blk}'])
        pmax = float(row[f'Pmax_{blk}'])
        price = float(row['Price'])
        cost_slope = float(row[f'cost_slope_{blk}'])
        coal_slope = float(row[f'coal_slope_{blk}'])
        coal_fixed = float(row[f'coal_fixed_{blk}'])
        duo_adj = float(row.get(f'duo_coal_adj_{blk}', 0.0)) if both_on else 0.0

        margin = (price - cost_slope) / coal_slope
        coal_at_pmin = coal_slope * pmin + coal_fixed + duo_adj
        extra_coal_to_pmax = (pmax - pmin) * coal_slope

        records.append(dict(
            blk=blk, date=str(row['Date'])[:10], hour=int(row['Hour']),
            price=price, margin=margin,
            coal_slope=coal_slope, coal_at_pmin=coal_at_pmin,
            extra_coal=extra_coal_to_pmax,
            pmin=pmin, pmax=pmax,
        ))

df = pd.DataFrame(records).sort_values('margin', ascending=False).reset_index(drop=True)

# Base coal = total coal when all ON hours run at Pmin
base_coal = df['coal_at_pmin'].sum()

# Reverse-engineer the coal limit from MOSEK's shadow = 4.89
# The limit is where the cumulative extra coal hits the budget
cum_extra = df['extra_coal'].cumsum()

# Find the marginal hour at 4.89 EUR/t
marginal_idx = (df['margin'] - 4.89).abs().idxmin()
coal_limit = base_coal + cum_extra.loc[marginal_idx]
# Fine-tune: at the marginal hour P_eff=315, Pmin=155, Pmax=444
# So partial fill = (315-155)/(444-155) = 0.554 of extra_coal
partial = (315 - 155) / (444 - 155)
coal_limit = base_coal + cum_extra.loc[marginal_idx - 1] + df.loc[marginal_idx, 'extra_coal'] * partial

print(f"Reverse-engineered July coal limit: {coal_limit:,.0f} tonnes ({coal_limit/1000:.1f} kt)")
print(f"Base coal (all at Pmin): {base_coal:,.0f} tonnes")
print(f"Budget for upscaling: {coal_limit - base_coal:,.0f} tonnes")
print()

# Now compute shadow price at different budget levels
# shadow(budget) = margin of the marginal hour when cumulative extra coal = budget - base_coal
def shadow_at_budget(coal_budget):
    """Return shadow price and marginal hour info for a given total coal budget."""
    upscale_budget = coal_budget - base_coal
    if upscale_budget <= 0:
        return df['margin'].iloc[0], "No upscale budget"
    
    cum = 0.0
    for i, row in df.iterrows():
        if cum + row['extra_coal'] >= upscale_budget:
            # This is the marginal hour
            frac = (upscale_budget - cum) / row['extra_coal']
            p_eff = row['pmin'] + frac * (row['pmax'] - row['pmin'])
            info = f"{row['blk']} {row['date']} h{row['hour']:2d}  Price={row['price']:.1f}  P_eff={p_eff:.0f}"
            return row['margin'], info
        cum += row['extra_coal']
    
    return 0.0, "Budget exceeds all hours (unconstrained)"

# Current shadow (baseline)
shadow_0, info_0 = shadow_at_budget(coal_limit)
print(f"{'Delta':>8s}  {'Budget':>10s}  {'Shadow':>8s}  Marginal hour")
print("-" * 90)

for delta in [0, 10, 100, 1000]:
    budget = coal_limit + delta
    shadow, info = shadow_at_budget(budget)
    label = f"+{delta}t" if delta > 0 else "current"
    print(f"{label:>8s}  {budget/1000:>9.1f} kt  {shadow:>7.2f}  {info}")

print()
print("=" * 90)
print("DETAILED: Value of extra coal (cumulative PnL gain)")
print("=" * 90)

# For each delta, compute the total PnL gain from extra coal vs baseline
# PnL gain from hour i going from Pmin to P: = (Price - cost_slope) * (P - Pmin)
# = margin * coal_slope * (P - Pmin) = margin * extra_coal_used

def pnl_at_budget(coal_budget):
    """Total PnL from upscaling with given budget."""
    upscale_budget = coal_budget - base_coal
    if upscale_budget <= 0:
        return 0.0
    cum = 0.0
    pnl = 0.0
    for i, row in df.iterrows():
        if cum + row['extra_coal'] >= upscale_budget:
            used = upscale_budget - cum
            pnl += row['margin'] * used  # EUR per tonne * tonnes
            break
        pnl += row['margin'] * row['extra_coal']
        cum += row['extra_coal']
    return pnl

pnl_baseline = pnl_at_budget(coal_limit)

print()
print(f"{'Delta':>8s}  {'Shadow':>8s}  {'Incr PnL':>12s}  {'Avg EUR/t':>10s}  {'Marginal hour'}")
print("-" * 95)

prev_pnl = pnl_baseline
for delta in [0, 10, 100, 1000]:
    budget = coal_limit + delta
    shadow, info = shadow_at_budget(budget)
    pnl = pnl_at_budget(budget)
    incr_pnl = pnl - pnl_baseline
    avg = incr_pnl / delta if delta > 0 else shadow
    label = f"+{delta}t" if delta > 0 else "current"
    print(f"{label:>8s}  {shadow:>7.2f}  {incr_pnl:>11,.0f} EUR  {avg:>9.2f}  {info}")

# Also show what happens if we REDUCE coal
print()
print("=" * 90)
print("What if we had LESS coal?")
print("=" * 90)
print()
print(f"{'Delta':>8s}  {'Shadow':>8s}  {'PnL loss':>12s}  {'Avg EUR/t':>10s}  {'Marginal hour'}")
print("-" * 95)

for delta in [-1000, -100, -10, 0]:
    budget = coal_limit + delta
    shadow, info = shadow_at_budget(budget)
    pnl = pnl_at_budget(budget)
    pnl_diff = pnl - pnl_baseline
    avg = pnl_diff / delta if delta != 0 else shadow
    label = f"{delta}t" if delta != 0 else "current"
    print(f"{label:>8s}  {shadow:>7.2f}  {pnl_diff:>11,.0f} EUR  {avg:>9.2f}  {info}")
