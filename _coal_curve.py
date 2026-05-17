"""
Shadow price analysis: what is the constant-value range?

The LP shadow price = (Price - MC) / coal_factor at the MARGINAL hour.
It is CONSTANT for any extra coal that can be absorbed by the same set
of "interior" hours (hours where Pmin < P < Pmax).

The shadow price only changes when:
  - Enough extra coal is added that a currently-interior hour hits Pmax
    (basis change → shadow drops to the next-worse hour's value)
  - Or coal is reduced enough that an interior hour hits Pmin
"""
import pandas as pd
import numpy as np
import openpyxl

fr = r'Inputs_EOD_20_04_2026/Data1 EPNLintr26 High availability_20_04_2026_results_restricted_2026-04-24_13-58.xlsx'

# Use openpyxl read_only to bypass Excel lock
wb = openpyxl.load_workbook(fr, read_only=True, data_only=True)
ws = wb['Results']
rows = list(ws.iter_rows(values_only=True))
wb.close()
header = rows[0]
dr = pd.DataFrame(rows[1:], columns=header)
# Convert numeric columns
for col in dr.columns:
    try:
        dr[col] = pd.to_numeric(dr[col])
    except (ValueError, TypeError):
        pass

shadow_vals = {6: 18.48, 7: 4.89, 8: 13.57}

for mo_num, mo_name in [(6, 'Jun'), (7, 'Jul'), (8, 'Aug')]:
    mr = dr[dr['month_num'] == mo_num].copy()
    shadow = shadow_vals[mo_num]

    # For each ON block-hour, classify: at Pmin, at Pmax, or interior
    at_pmin = []
    at_pmax = []
    interior = []

    for idx in mr.index:
        price = mr.loc[idx, 'Price']
        for blk in ['A', 'B']:
            on = mr.loc[idx, f'on_model_{blk}']
            if on != 1:
                continue
            p = mr.loc[idx, f'P_eff_{blk}']
            pmin = mr.loc[idx, f'Pmin_{blk}']
            pmax = mr.loc[idx, f'Pmax_{blk}']
            mc = mr.loc[idx, f'MC_{blk}']
            cf = mr.loc[idx, f'Coal conversion factor at Pmax [t/MWh]_{blk}']

            margin_per_ton = (price - mc) / cf if cf > 0 else 0

            rec = dict(blk=blk, price=price, mc=mc, cf=cf, p=p,
                       pmin=pmin, pmax=pmax, margin_per_ton=margin_per_ton,
                       headroom_up=(pmax - p) * cf,
                       headroom_down=(p - pmin) * cf)

            if p <= pmin + 1:
                at_pmin.append(rec)
            elif p >= pmax - 1:
                at_pmax.append(rec)
            else:
                interior.append(rec)

    # Sort interior by margin
    interior.sort(key=lambda x: x['margin_per_ton'])

    # The shadow price should equal margin_per_ton for ALL interior hours
    # (at optimality, they should be equalized)
    int_margins = [r['margin_per_ton'] for r in interior]

    print(f"===== {mo_name} (LP shadow = {shadow:.2f} EUR/t) =====")
    print(f"  ON hours: at_Pmin={len(at_pmin)}  interior={len(interior)}  at_Pmax={len(at_pmax)}")

    if interior:
        print(f"  Interior hours margin/ton: min={min(int_margins):.2f}  "
              f"max={max(int_margins):.2f}  mean={np.mean(int_margins):.2f}  "
              f"median={np.median(int_margins):.2f}")

        # Headroom at interior hours: how many extra tons can be absorbed
        # before any interior hour hits Pmax?
        total_headroom_up = sum(r['headroom_up'] for r in interior)
        min_headroom_up = min(r['headroom_up'] for r in interior)
        total_headroom_down = sum(r['headroom_down'] for r in interior)

        print(f"  Interior hours total headroom UP: {total_headroom_up:,.0f} t "
              f"(min single hour: {min_headroom_up:,.0f} t)")
        print(f"  Interior hours total headroom DOWN: {total_headroom_down:,.0f} t")

        # The shadow stays constant while extra coal goes to interior hours
        # Each extra ton is spread across all interior hours proportionally
        # The basis changes when any one interior hour hits Pmax
        # With N interior hours, 1 extra ton → 1/N extra tons per hour
        # First basis change at: N × min_headroom_up extra tons
        n_int = len(interior)
        basis_change_at = n_int * min_headroom_up
        print(f"  Shadow price stays at {shadow:.2f} EUR/t for approx "
              f"{basis_change_at:,.0f} extra tons")
        print(f"  ({n_int} interior hours × {min_headroom_up:.0f} t min headroom)")
    else:
        print("  No interior hours — all at Pmin or Pmax")

    # Marginal price = price at which shadow = (price - mc) / cf
    # => price = mc + shadow × cf
    # (using avg mc and cf for reference)
    avg_mc_A = mr['MC_A'].mean()
    avg_cf_A = mr['Coal conversion factor at Pmax [t/MWh]_A'].mean()
    marginal_price = avg_mc_A + shadow * avg_cf_A
    print(f"  Marginal price threshold: {marginal_price:.1f} EUR/MWh "
          f"(MC_A={avg_mc_A:.1f} + {shadow:.2f} × {avg_cf_A:.3f})")

    # Summary table: value of first N extra tons
    print()
    print(f"  Value of extra coal (constant within basis):")
    for n in [1, 5, 10, 50, 100, 1000]:
        total_val = n * shadow
        print(f"    +{n:>5,d} t  →  {total_val:>10,.0f} EUR  "
              f"({shadow:.2f} EUR/t each)")

    # At what point does value start declining?
    # List the Pmax-bound hours sorted by margin (these would be "released"
    # into interior status as more coal becomes available, eventually)
    pmax_margins = sorted([r['margin_per_ton'] for r in at_pmax], reverse=True)
    if pmax_margins:
        print(f"\n  Pmax-bound hours ({len(at_pmax)}): margin range "
              f"[{pmax_margins[-1]:.1f}, {pmax_margins[0]:.1f}] EUR/t")

    pmin_margins = sorted([r['margin_per_ton'] for r in at_pmin], reverse=True)
    if pmin_margins:
        # These are hours at Pmin — they have lower margin
        # When coal budget shrinks, these would lose output first
        print(f"  Pmin-bound hours ({len(at_pmin)}): margin range "
              f"[{pmin_margins[-1]:.1f}, {pmin_margins[0]:.1f}] EUR/t")

    # Price distribution in each group
    print(f"\n  Average prices:")
    if at_pmax:
        avg_price_pmax = np.mean([r['price'] for r in at_pmax])
        print(f"    At Pmax: {avg_price_pmax:.1f} EUR/MWh ({len(at_pmax)} hours)")
    if interior:
        avg_price_int = np.mean([r['price'] for r in interior])
        print(f"    Interior: {avg_price_int:.1f} EUR/MWh ({len(interior)} hours)")
    if at_pmin:
        avg_price_pmin = np.mean([r['price'] for r in at_pmin])
        print(f"    At Pmin: {avg_price_pmin:.1f} EUR/MWh ({len(at_pmin)} hours)")

    print()
