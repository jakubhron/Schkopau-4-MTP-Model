"""Investigate the 4.89 vs ~16 EUR/t shadow price discrepancy for July."""
import pandas as pd

dr = pd.read_parquet(r'_solver_cache/cache_chrono_AP1_cache_v3_2026-04-24_13-58.parquet')
jul = dr[dr['month_num'] == 7]

print("=" * 70)
print("DUO coal slope analysis for July")
print("=" * 70)

# DUO vs mono coal slopes
for blk in ['A', 'B']:
    mono = jul[f'coal_slope_{blk}'].iloc[0]
    duo = jul[f'coal_slope_duo_{blk}'].iloc[0]
    print(f"  coal_slope_{blk}     = {mono:.6f} t/MWh")
    print(f"  coal_slope_duo_{blk} = {duo:.6f} t/MWh  (delta = {duo - mono:+.6f})")
    mono_f = jul[f'coal_fixed_{blk}'].iloc[0]
    duo_f = jul[f'coal_fixed_duo_{blk}'].iloc[0]
    print(f"  coal_fixed_{blk}     = {mono_f:.4f} t/h")
    print(f"  coal_fixed_duo_{blk} = {duo_f:.4f} t/h  (delta = {duo_f - mono_f:+.4f})")
    
    # Also cost slopes
    cost_mono = jul[f'cost_slope_{blk}'].iloc[0]
    cost_duo = jul[f'cost_slope_duo_{blk}'].iloc[0]
    print(f"  cost_slope_{blk}     = {cost_mono:.4f} EUR/MWh")
    print(f"  cost_slope_duo_{blk} = {cost_duo:.4f} EUR/MWh  (delta = {cost_duo - cost_mono:+.4f})")
    
    # duo_cost_adj
    dca = jul[f'duo_cost_adj_{blk}'].iloc[0]
    print(f"  duo_cost_adj_{blk}   = {dca:.4f} EUR/h")
    print()

# How many both-on hours
both_on = (jul['on_model_A'] == 1) & (jul['on_model_B'] == 1)
print(f"Hours with both ON: {both_on.sum()}/{len(jul)}")
print()

# Now: build proper merit order accounting for DUO adjustments
# When both blocks are ON and we increase P_eff for one block by 1 MWh:
#   - Objective changes by: (Price - cost_slope) + 0 [duo_cost_adj doesn't depend on P_eff]
#     Wait — let me check the model. duo_cost_adj IS just a fixed term per hour.
#     Actually... the DUO adjustment changes the EFFECTIVE cost/coal slopes.
#     Let me look at how coal constraint is formulated.

# In model_builder.py the coal constraint is:
# sum_t( coal_slope[b,t]*P_eff[b,t] + coal_fixed[b,t]*on[b,t] + duo_coal_adj[b,t]*both_on[t] )
#
# duo_coal_adj is a CONSTANT per hour (depends on Pnom, not on P_eff).
# So when the LP relaxes, both_on[t] is still fixed (it's computed from fixed on[b,t]).
# That means duo_coal_adj doesn't change the marginal coal per MWh.
# The marginal coal per extra MWh of P_eff is just coal_slope[b,t].
#
# BUT duo_coal_adj DOES change the total coal consumed, and thus the remaining budget!

# Let me compute total coal consumed at Pmin levels (with DUO fixed terms):
total_coal_limit = jul['coal_exact'].iloc[0]  # All rows same
print(f"Coal budget for July: {total_coal_limit:.0f} tonnes")

# Coal consumed at minimum (all ON blocks at Pmin):
coal_at_pmin = 0.0
records = []
for idx, row in jul.iterrows():
    for blk in ['A', 'B']:
        on = int(row[f'on_model_{blk}'])
        if on != 1:
            continue
        other_blk = 'B' if blk == 'A' else 'A'
        other_on = int(row[f'on_model_{other_blk}'])

        pmin = float(row[f'Pmin_{blk}'])
        pmax = float(row[f'Pmax_{blk}'])
        coal_slope = float(row[f'coal_slope_{blk}'])
        coal_fixed = float(row[f'coal_fixed_{blk}'])
        price = float(row['Price'])
        cost_slope = float(row[f'cost_slope_{blk}'])

        # Coal at Pmin = coal_slope * Pmin + coal_fixed (from the "on" portion)
        coal_pmin = coal_slope * pmin + coal_fixed
        
        # Extra coal for DUO
        duo_coal_adj = 0.0
        if other_on == 1:
            duo_coal_adj = float(row.get(f'duo_coal_adj_{blk}', 0.0))
        
        coal_at_pmin += coal_pmin + duo_coal_adj

        # Merit: marginal profit per marginal coal = (Price - cost_slope) / coal_slope
        margin = (price - cost_slope) / coal_slope if coal_slope > 0 else 0
        extra_coal = (pmax - pmin) * coal_slope  # Coal for going Pmin -> Pmax

        records.append(dict(
            blk=blk, hour=int(row['Hour']),
            date=str(row['Date'])[:10],
            price=price, margin=margin,
            coal_slope=coal_slope,
            extra_coal=extra_coal,
            both_on=other_on,
            pmin=pmin, pmax=pmax,
            duo_coal_adj=duo_coal_adj,
        ))

budget_for_upscale = total_coal_limit - coal_at_pmin
print(f"Coal at Pmin (all ON blocks): {coal_at_pmin:.0f} tonnes")
print(f"Budget for upscaling: {budget_for_upscale:.0f} tonnes")
print()

# Check: what was the budget WITHOUT duo_coal_adj?
coal_at_pmin_no_duo = sum(r['pmin'] * r['coal_slope'] + 
                          jul[f"coal_fixed_{r['blk']}"].iloc[0]
                          for r in records)
print(f"Coal at Pmin WITHOUT DUO: {coal_at_pmin_no_duo:.0f} tonnes")
print(f"Budget WITHOUT DUO: {total_coal_limit - coal_at_pmin_no_duo:.0f} tonnes")
print(f"DUO eats: {coal_at_pmin - coal_at_pmin_no_duo:.0f} tonnes of budget")
print()

# Sort by merit order (descending margin)
df = pd.DataFrame(records).sort_values('margin', ascending=False).reset_index(drop=True)

# Fill budget
cum = 0.0
cutoff_idx = None
for i, row in df.iterrows():
    cum += row['extra_coal']
    if cum >= budget_for_upscale:
        cutoff_idx = i
        break

if cutoff_idx is not None:
    r = df.loc[cutoff_idx]
    print(f"Cutoff at rank {cutoff_idx + 1}/{len(df)}")
    print(f"  margin = {r['margin']:.2f} EUR/t")
    print(f"  Price = {r['price']:.1f}, cost_slope = {r['price'] - r['margin'] * r['coal_slope']:.2f}")
    print(f"  coal_slope = {r['coal_slope']:.6f}")
    print(f"  Block {r['blk']}, {r['date']} h{r['hour']}")
    print(f"  both_on = {r['both_on']}")
    print()
    
    # Show hours around cutoff
    print("Hours around cutoff:")
    start = max(0, cutoff_idx - 5)
    end = min(len(df), cutoff_idx + 6)
    for j in range(start, end):
        row = df.loc[j]
        marker = " <<<< CUTOFF" if j == cutoff_idx else ""
        print(f"  {j+1:5d}  {row['blk']}  {row['date']} h{row['hour']:2.0f}  "
              f"Price={row['price']:6.1f}  margin={row['margin']:6.2f}  "
              f"both_on={row['both_on']}{marker}")
else:
    print("Budget not exhausted — all hours at Pmax!")

print()
print("=" * 70)
print("MOSEK shadow price = 4.89 EUR/t")
print(f"Our simulation cutoff = {df.loc[cutoff_idx]['margin']:.2f} EUR/t" if cutoff_idx else "N/A")
print("=" * 70)

# Let's also check: what if we look at what duo_coal_adj values are?
print()
print("DUO coal adjustment values (sample):")
for blk in ['A', 'B']:
    col = f'duo_coal_adj_{blk}'
    if col in jul.columns:
        v = jul[col].iloc[0]
        print(f"  duo_coal_adj_{blk} = {v:.4f} t/h  (per hour, added when both_on=1)")
        # Total DUO coal
        total_duo = both_on.sum() * v
        print(f"  Total DUO coal for {blk}: {both_on.sum()} hours * {v:.4f} = {total_duo:.0f} tonnes")
