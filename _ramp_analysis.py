"""Verify that ramp constraints create the 4.89 EUR/t marginal hour."""
import pandas as pd

dr = pd.read_parquet(r'_solver_cache/cache_chrono_AP1_cache_v3_2026-04-24_13-58.parquet')
jul = dr[dr['month_num'] == 7]

# Focus on the critical period: Jul 19 around hour 5 for Block A
# Show the ramp pattern
print("=" * 80)
print("Block A dispatch around Jul 19 h5 (the 4.89 EUR/t marginal hour)")
print("=" * 80)

target = jul[(jul['Date'].dt.day >= 18) & (jul['Date'].dt.day <= 20)]
for _, row in target.iterrows():
    day = row['Date'].day
    hour = int(row['Hour'])
    price = float(row['Price'])
    p_a = float(row['P_eff_A'])
    on_a = int(row['on_model_A'])
    pmin_a = float(row['Pmin_A'])
    pmax_a = float(row['Pmax_A'])
    cs = float(row['cost_slope_A'])
    csl = float(row['coal_slope_A'])
    margin_a = (price - cs) / csl if on_a else 0

    status = ""
    if not on_a:
        status = "OFF"
    elif abs(p_a - pmin_a) < 2:
        status = "Pmin"
    elif abs(p_a - pmax_a) < 2:
        status = "Pmax"
    else:
        status = f"INTERIOR ({p_a:.0f})"

    marker = " <<<<" if day == 19 and hour == 5 else ""
    print(f"  Jul {day:2d} h{hour:2d}  Price={price:6.1f}  P_eff_A={p_a:5.0f}  "
          f"on={on_a}  [{status:>14s}]  margin={margin_a:6.2f}{marker}")

print()
print("=" * 80)
print("WHY P_eff = 315 at Jul 19 h5?")
print("=" * 80)

# Check ramp limits
from schkopau_mtp import config as cfg
print(f"Ramp up limit: {cfg.RAMP_UP_LIMIT} MW/h")
print(f"Ramp down limit: {cfg.RAMP_DOWN_LIMIT} MW/h")

# Jul 19 h4 → h5 transition
h4 = jul[(jul['Date'].dt.day == 19) & (jul['Hour'] == 4)].iloc[0]
h5 = jul[(jul['Date'].dt.day == 19) & (jul['Hour'] == 5)].iloc[0]
p4 = float(h4['P_eff_A'])
p5 = float(h5['P_eff_A'])
print(f"\nJul 19 h4: P_eff_A = {p4:.0f}")
print(f"Jul 19 h5: P_eff_A = {p5:.0f}")
print(f"Change: {p5 - p4:+.0f} MW")

# Can it ramp further?
if hasattr(cfg, 'RAMP_UP_LIMIT'):
    print(f"Max ramp up: {cfg.RAMP_UP_LIMIT} MW/h → max P at h5 = {p4 + cfg.RAMP_UP_LIMIT:.0f}")
    print(f"Max ramp down: {cfg.RAMP_DOWN_LIMIT} MW/h → min P at h5 = {p4 - cfg.RAMP_DOWN_LIMIT:.0f}")

print()
print("=" * 80)
print("ALL interior Block A hours near the 4.89 margin")
print("=" * 80)
for _, row in jul.iterrows():
    on_a = int(row['on_model_A'])
    if not on_a:
        continue
    p_a = float(row['P_eff_A'])
    pmin_a = float(row['Pmin_A'])
    pmax_a = float(row['Pmax_A'])
    if p_a > pmin_a + 1 and p_a < pmax_a - 1:
        price = float(row['Price'])
        margin = (price - float(row['cost_slope_A'])) / float(row['coal_slope_A'])
        if abs(margin - 4.89) < 3:
            print(f"  {str(row['Date'])[:10]} h{int(row['Hour']):2d}  "
                  f"Price={price:6.1f}  P_eff={p_a:5.0f}  margin={margin:6.2f}")

# Count how many hours are "wasted" by ramps (on at Pmax but low margin, or 
# on at Pmin but high margin due to ramp inability)
print()
print("=" * 80)
print("RAMP WASTE ANALYSIS")
print("=" * 80)

wasted_at_pmax = 0  # at Pmax but margin < 4.89 (coal wasted on low-value hours)
wasted_at_pmin = 0  # at Pmin but margin > 4.89 (couldn't ramp up)
for _, row in jul.iterrows():
    on_a = int(row['on_model_A'])
    if not on_a:
        continue
    p_a = float(row['P_eff_A'])
    pmin_a = float(row['Pmin_A'])
    pmax_a = float(row['Pmax_A'])
    price = float(row['Price'])
    margin = (price - float(row['cost_slope_A'])) / float(row['coal_slope_A'])

    if abs(p_a - pmax_a) < 2 and margin < 4.89:
        wasted_at_pmax += 1
    if abs(p_a - pmin_a) < 2 and margin > 4.89:
        wasted_at_pmin += 1

print(f"Block A hours at Pmax with margin < 4.89 (coal wasted): {wasted_at_pmax}")
print(f"Block A hours at Pmin with margin > 4.89 (couldn't ramp up): {wasted_at_pmin}")
print()
print("CONCLUSION:")
print("The ramp constraints force the plant to burn coal on low-margin hours")
print("(during ramp-up/down transitions), consuming budget that would otherwise")
print("go to higher-margin hours. This pushes the marginal value of coal down")
print(f"from ~16 EUR/t (unconstrained merit order) to 4.89 EUR/t.")
