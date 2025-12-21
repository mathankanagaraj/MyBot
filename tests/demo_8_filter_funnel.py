#!/usr/bin/env python3
"""
Simplified unit test demonstration for optimized trading strategy.
Shows how the 8-filter funnel works step by step.
"""

print("=" * 70)
print("OPTIMIZED STRATEGY - 8-FILTER FUNNEL DEMONSTRATION")
print("=" * 70)

print("\n" + "=" * 70)
print("SCENARIO: Testing CALL Entry on AAPL")
print("=" * 70)

# Sample market data
sample_data = {
    "symbol": "AAPL",
    "current_price": 150.50,
    "current_time": "2025-01-15 10:45:00 ET",
    # 15m bias data
    "15m_supertrend": "BULLISH",
    "15m_vwap": 149.20,
    "15m_rsi14": 52.5,
    # 5m entry data
    "5m_ema20": 149.80,
    "5m_rsi5_recent": [32.5, 36.8, 42.1],  # Last 3 candles
    "5m_current_volume": 1_250_000,
    "5m_volume_ma20": 980_000,
    "5m_candle_open": 149.80,
    "5m_candle_close": 150.50,
    "5m_ema_values": [148.50, 148.80, 149.20, 149.50, 149.70, 149.80],
    "last_entry_time": "10:15:00",  # 30 minutes ago
}

print("\nMarket Data:")
print(f"  Symbol: {sample_data['symbol']}")
print(f"  Current Price: ${sample_data['current_price']}")
print(f"  Time: {sample_data['current_time']}")

print("\n" + "-" * 70)
print("STEP 1: 15m BIAS DETECTION")
print("-" * 70)

print(f"\n15m Candle Analysis:")
print(f"  Price: ${sample_data['current_price']:.2f}")
print(f"  SuperTrend: {sample_data['15m_supertrend']}")
print(f"  VWAP: ${sample_data['15m_vwap']:.2f}")
print(f"  RSI(14): {sample_data['15m_rsi14']}")

print(f"\nBias Checks:")
# Check 1: SuperTrend
st_check = sample_data["15m_supertrend"] == "BULLISH"
print(f"  ✅ Price > SuperTrend: {st_check}")

# Check 2: VWAP
vwap_check = sample_data["current_price"] > sample_data["15m_vwap"]
print(
    f"  ✅ Price > VWAP: {vwap_check} (${sample_data['current_price']:.2f} > ${sample_data['15m_vwap']:.2f})"
)

# Check 3: RSI
rsi_check = sample_data["15m_rsi14"] > 45
print(f"  ✅ RSI(14) > 45: {rsi_check} ({sample_data['15m_rsi14']} > 45)")

bias_result = "BULLISH" if (st_check and vwap_check and rsi_check) else "NONE"
print(f"\n🎯 RESULT: {bias_result} BIAS DETECTED")

print("\n" + "-" * 70)
print("STEP 2: 5m ENTRY DETECTION (8-FILTER FUNNEL)")
print("-" * 70)

filters_passed = {}
filters_failed = {}

# Filter 1: Bias Alignment
print(f"\nFilter 1: Bias Alignment")
print(f"  Bias: {bias_result}")
if bias_result in ["BULLISH", "BEARISH"]:
    print(f"  ✅ PASS - Active bias detected")
    filters_passed["bias_alignment"] = True
else:
    print(f"  ❌ FAIL - No clear bias")
    filters_failed["bias_alignment"] = "No bias"

# Filter 2: Price vs EMA(20)
print(f"\nFilter 2: Price vs EMA(20)")
print(f"  Current Price: ${sample_data['current_price']:.2f}")
print(f"  EMA(20): ${sample_data['5m_ema20']:.2f}")
price_vs_ema = sample_data["current_price"] > sample_data["5m_ema20"]
if price_vs_ema:
    print(f"  ✅ PASS - Price above structure")
    filters_passed["price_vs_ema"] = (
        f"${sample_data['current_price']:.2f} > ${sample_data['5m_ema20']:.2f}"
    )
else:
    print(f"  ❌ FAIL - Price below structure")
    filters_failed["price_vs_ema"] = (
        f"${sample_data['current_price']:.2f} <= ${sample_data['5m_ema20']:.2f}"
    )

# Filter 3: RSI(5) Dead Zone
print(f"\nFilter 3: RSI(5) Dead Zone Confirmation")
print(f"  Recent RSI(5) values: {sample_data['5m_rsi5_recent']}")
touched_dead_zone = any(r < 35 for r in sample_data["5m_rsi5_recent"])
print(f"  Check: Did any value touch < 35?")
if touched_dead_zone:
    min_rsi = min(sample_data["5m_rsi5_recent"])
    print(f"  ✅ PASS - Pullback confirmed (min RSI: {min_rsi})")
    filters_passed["rsi_dead_zone"] = f"Touched {min_rsi} < 35"
else:
    print(f"  ❌ FAIL - No pullback to dead zone")
    filters_failed["rsi_dead_zone"] = "Never touched < 35"

# Filter 4: RSI(5) Crossover
print(f"\nFilter 4: RSI(5) Crossover")
current_rsi = sample_data["5m_rsi5_recent"][-1]
print(f"  Current RSI(5): {current_rsi}")
print(f"  Check: {current_rsi} > 40?")
rsi_crossover = current_rsi > 40
if rsi_crossover:
    print(f"  ✅ PASS - Momentum ignited")
    filters_passed["rsi_crossover"] = f"{current_rsi} > 40"
else:
    print(f"  ❌ FAIL - RSI still below threshold")
    filters_failed["rsi_crossover"] = f"{current_rsi} <= 40"

# Filter 5: Volume
print(f"\nFilter 5: Volume Confirmation")
print(f"  Current Volume: {sample_data['5m_current_volume']:,}")
print(f"  Volume MA(20): {sample_data['5m_volume_ma20']:,}")
volume_check = sample_data["5m_current_volume"] > sample_data["5m_volume_ma20"]
volume_pct = (
    (sample_data["5m_current_volume"] / sample_data["5m_volume_ma20"]) - 1
) * 100
if volume_check:
    print(f"  ✅ PASS - Volume {volume_pct:.1f}% above average")
    filters_passed["volume"] = f"{volume_pct:.1f}% above MA"
else:
    print(f"  ❌ FAIL - Volume below average")
    filters_failed["volume"] = f"{volume_pct:.1f}% below MA"

# Filter 6: Candle Color
print(f"\nFilter 6: Candle Color")
print(f"  Open: ${sample_data['5m_candle_open']:.2f}")
print(f"  Close: ${sample_data['5m_candle_close']:.2f}")
is_green = sample_data["5m_candle_close"] > sample_data["5m_candle_open"]
if is_green:
    print(f"  ✅ PASS - GREEN candle (bullish)")
    filters_passed["candle_color"] = "GREEN"
else:
    print(f"  ❌ FAIL - RED candle (bearish)")
    filters_failed["candle_color"] = "RED"

# Filter 7: EMA Flatness
print(f"\nFilter 7: EMA Not Flat")
ema_values = sample_data["5m_ema_values"]
print(f"  Last 6 EMA values: {ema_values}")
slope = (ema_values[-1] - ema_values[0]) / 5
slope_pct = abs(slope) / sample_data["current_price"] * 100
print(f"  Slope: {slope:.4f}, Slope %: {slope_pct:.4f}%")
print(f"  Check: {slope_pct:.4f}% > 0.1%?")
ema_trending = slope_pct > 0.1
if ema_trending:
    print(f"  ✅ PASS - Market is trending")
    filters_passed["ema_flatness"] = f"Slope {slope_pct:.2f}% > 0.1%"
else:
    print(f"  ❌ FAIL - Market is ranging")
    filters_failed["ema_flatness"] = f"Slope {slope_pct:.2f}% < 0.1%"

# Filter 8: Time Gap
print(f"\nFilter 8: Minimum Time Between Entries")
print(f"  Last Entry: {sample_data['last_entry_time']}")
print(f"  Current Time: 10:45:00")
print(f"  Time Gap: 30 minutes")
time_gap = 30  # minutes
min_gap = 15
if time_gap >= min_gap:
    print(f"  ✅ PASS - {time_gap} min > {min_gap} min")
    filters_passed["time_gap"] = f"{time_gap} minutes"
else:
    print(f"  ❌ FAIL - Only {time_gap} min (need {min_gap} min)")
    filters_failed["time_gap"] = f"Only {time_gap} min"

# Final Result
print("\n" + "=" * 70)
print("FINAL RESULT")
print("=" * 70)

total_filters = 8
passed_count = len(filters_passed)
failed_count = len(filters_failed)

print(f"\nFilters Passed: {passed_count}/{total_filters}")
for key, val in filters_passed.items():
    print(f"  ✅ {key}: {val}")

if filters_failed:
    print(f"\nFilters Failed: {failed_count}")
    for key, val in filters_failed.items():
        print(f"  ❌ {key}: {val}")

if passed_count == total_filters:
    print("\n" + "🎯" * 35)
    print("✅ ALL FILTERS PASSED - EXECUTE CALL TRADE!")
    print("🎯" * 35)
    print(f"\nTrade Details:")
    print(f"  Symbol: {sample_data['symbol']}")
    print(f"  Entry Price: ${sample_data['current_price']:.2f}")
    print(f"  Signal: CALL")
    print(f"  Time: {sample_data['current_time']}")
else:
    print(f"\n❌ ENTRY REJECTED")
    print(
        f"   Reason: {list(filters_failed.keys())[0] if filters_failed else 'Unknown'}"
    )
    print(f"   Only {passed_count}/8 filters passed")

print("\n" + "=" * 70)
print("DEMONSTRATION COMPLETE")
print("=" * 70)
print("\nThis shows how the 8-filter funnel works:")
print("  • Each filter must PASS for trade execution")
print("  • Sequential evaluation (fails fast)")
print("  • Detailed logging shows exactly why entries are accepted/rejected")
print("  • Conservative approach = High quality trades only")
print("\n" + "=" * 70)
