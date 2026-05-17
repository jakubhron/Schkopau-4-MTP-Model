"""Show the ranked merit order of ON block-hours and where the coal cutoff falls."""
import pandas as pd
import numpy as np

dr = pd.read_parquet(r'_solver_cache/cache_chrono_AP1_cache_v3_2026-04-24_13-58.parquet')
jul = dr[dr['month_num'] == 7].copy()

# Build ranked list
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
        extra_coal = (pmax - pmin) * cf
        records.append(dict(
            blk=blk, hour=int(row['Hour']),
            date=str(row['Date'])[:10],
            price=price, mc=mc, cf=cf,
            pmin=pmin, pmax=pmax, p_mip=p_mip,
            margin_per_ton=margin_per_ton,
            extra_coal=extra_coal,
        ))

df = pd.DataFrame(records)
df = df.sort_values('margin_per_ton', ascending=False).reset_index(drop=True)

total_coal = jul['coal_exact'].sum()
total_pmin_coal = (df['pmin'] * df['cf']).sum()
budget = total_coal - total_pmin_coal

print(f"JULY: {len(df)} ON block-hours")
print(f"Coal budget = {total_coal:,.0f} t")
print(f"Coal at Pmin for all = {total_pmin_coal:,.0f} t")
print(f"Extra coal available above Pmin = {budget:,.0f} t")
print()
print(f"{'Rank':>4s}  {'Blk':>3s}  {'Date':>10s}  {'Hr':>2s}  "
      f"{'Price':>6s}  {'MC':>5s}  {'Spread':>7s}  {'EUR/t':>7s}  {'LP':>10s}")
print("-" * 75)

cum = 0.0
cutoff_done = False
for i, (_, r) in enumerate(df.iterrows()):
    rank = i + 1
    extra = r['extra_coal']
    cum_after = cum + extra
    spread = r['price'] - r['mc']

    if cum_after <= budget:
        assign = "Pmax"
    elif cum < budget:
        assign = "MARGINAL"
    else:
        assign = "Pmin"

    # Print: first 10, around cutoff (575-590), last 5
    show = False
    if rank <= 10:
        show = True
    elif rank == 11:
        print("  ... (ranks 11-574: all Pmax, prices 165→101 EUR/MWh) ...")
    if 575 <= rank <= 590:
        show = True
    elif rank == 591:
        remaining = len(df) - rank + 1
        print(f"  ... ({remaining} more Pmin hours, prices 93→5 EUR/MWh) ...")
    if rank >= len(df) - 4:
        show = True
    if assign == "MARGINAL":
        show = True

    if show:
        marker = " <<<" if assign == "MARGINAL" else ""
        line = (f"{rank:>4d}  {r['blk']:>3s}  {r['date']:>10s}  "
                f"h{r['hour']:>2d}  {r['price']:>6.1f}  {r['mc']:>5.1f}  "
                f"{spread:>+7.1f}  {r['margin_per_ton']:>7.1f}  {assign:>10s}{marker}")
        print(line)

    cum = cum_after

# === Cross-month comparison ===
print()
print("=" * 75)
print("CROSS-MONTH COMPARISON: where does the coal cutoff fall?")
print("=" * 75)

for mo_num, mo_name in [(6, 'Jun'), (7, 'Jul'), (8, 'Aug')]:
    mo = dr[dr['month_num'] == mo_num]
    recs = []
    for idx, row in mo.iterrows():
        for blk in ['A', 'B']:
            if int(row[f'on_model_{blk}']) != 1:
                continue
            price = float(row['Price'])
            mc = float(row[f'MC_{blk}'])
            cf = float(row[f'Coal conversion factor at Pmax [t/MWh]_{blk}'])
            pmin = float(row[f'Pmin_{blk}'])
            pmax = float(row[f'Pmax_{blk}'])
            margin = (price - mc) / cf if cf > 0 else 0
            extra = (pmax - pmin) * cf
            recs.append(dict(price=price, mc=mc, margin=margin, extra_coal=extra))

    mdf = pd.DataFrame(recs).sort_values('margin', ascending=False).reset_index(drop=True)

    total_coal_mo = mo['coal_exact'].sum()
    pmin_coal_mo = sum(
        float(row[f'Pmin_{blk}']) * float(row[f'Coal conversion factor at Pmax [t/MWh]_{blk}'])
        for _, row in mo.iterrows() for blk in ['A', 'B']
        if int(row[f'on_model_{blk}']) == 1
    )
    budget_mo = total_coal_mo - pmin_coal_mo

    cum = 0.0
    cutoff_rank = len(mdf)
    cutoff_margin = 0
    cutoff_price = 0
    for j, (_, r) in enumerate(mdf.iterrows()):
        cum += r['extra_coal']
        if cum >= budget_mo:
            cutoff_rank = j + 1
            cutoff_margin = r['margin']
            cutoff_price = r['price']
            break

    pct = cutoff_rank / len(mdf) * 100
    top_price = mdf.iloc[0]['price']
    bot_price = mdf.iloc[-1]['price']
    print(f"\n{mo_name}: {len(mdf)} ON block-hours (prices {top_price:.0f}→{bot_price:.0f} EUR/MWh)")
    print(f"  Coal budget above Pmin: {budget_mo:,.0f} t")
    print(f"  Cutoff at rank {cutoff_rank}/{len(mdf)} ({pct:.0f}%)")
    print(f"  Cutoff price = {cutoff_price:.1f} EUR/MWh")
    print(f"  Cutoff margin = {cutoff_margin:.1f} EUR/t  (≈ shadow price)")
    print(f"  → {cutoff_rank} hours at Pmax, {len(mdf)-cutoff_rank} hours at Pmin")
