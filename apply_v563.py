"""Apply all v5.6.3 changes: per-pair dwell wiring + oil asset class + EA oil symbols"""

# 1. Fix engine.py: wire cfg into all _calculate_dynamic_dwell calls + add COMMODITY asset class
with open('src/engine.py', 'r', encoding='utf-8') as f:
    eng = f.read()

# Wire per-pair dwell: cooldown in _maybe_enter
eng = eng.replace(
    'cooldown_seconds = self._calculate_dynamic_dwell(state.last_hurst)\n            elapsed = (now - state.last_close_time)',
    'cooldown_seconds = self._calculate_dynamic_dwell(state.last_hurst, cfg)\n            elapsed = (now - state.last_close_time)'
)

# Wire per-pair dwell: FILLED log in _maybe_enter
eng = eng.replace(
    'dwell = self._calculate_dynamic_dwell(state.last_hurst)\n            logger.info(\n                f"  FILLED:',
    'dwell = self._calculate_dynamic_dwell(state.last_hurst, cfg)\n            logger.info(\n                f"  FILLED:'
)

# Wire per-pair dwell: exit dwell check in _maybe_exit
eng = eng.replace(
    'dwell_required = self._calculate_dynamic_dwell(state.last_hurst)\n            if hold_seconds < dwell_required:\n                return  # Still within minimum dwell',
    'dwell_required = self._calculate_dynamic_dwell(state.last_hurst, cfg)\n            if hold_seconds < dwell_required:\n                return  # Still within minimum dwell'
)

# Wire per-pair dwell: close_spread log
eng = eng.replace(
    'dwell_required = self._calculate_dynamic_dwell(state.last_hurst)\n\n        logger.info(\n            f"EXIT {cfg.name}',
    'dwell_required = self._calculate_dynamic_dwell(state.last_hurst, cfg)\n\n        logger.info(\n            f"EXIT {cfg.name}'
)

# Add COMMODITY asset class for oil symbols
eng = eng.replace(
    "MIN_STOP_DISTANCE = {\n        'INDEX': 500.0,",
    "MIN_STOP_DISTANCE = {\n        'INDEX': 500.0,\n        'COMMODITY': 5.0,      # Oil: 500 points (XTIUSD ~$70, 5.0 = ~7%)"
)

# Update _get_asset_class to detect oil/commodity symbols
eng = eng.replace(
    "        if symbol in jpy_symbols:\n            return 'FOREX_JPY'",
    "        if symbol in jpy_symbols:\n            return 'FOREX_JPY'\n        commodity_symbols = ('XTIUSD', 'XBRUSD', 'WTI', 'BRENT', 'USOIL', 'UKOIL',\n                             'CrudeOIL', 'BrentOIL', 'USOILm', 'UKOILm', 'WTIm', 'BRNm')\n        if symbol in commodity_symbols:\n            return 'COMMODITY'"
)

with open('src/engine.py', 'w', encoding='utf-8') as f:
    f.write(eng)

# Verify changes
count_cfg = eng.count('_calculate_dynamic_dwell(state.last_hurst, cfg)')
count_nocfg = eng.count('_calculate_dynamic_dwell(state.last_hurst)')
print(f"engine.py: {count_cfg} dwell calls with cfg, {count_nocfg - count_cfg} without (fallback in def)")
print(f"engine.py: COMMODITY in MIN_STOP_DISTANCE: {'COMMODITY' in eng}")
print(f"engine.py: commodity_symbols tuple: {'commodity_symbols' in eng}")

# 2. Fix EA: replace forex symbols with oil symbols
with open('MQL5/Experts/SHF_Bridge.mq5', 'r', encoding='utf-8') as f:
    ea = f.read()

# Replace forex pair detection with oil pair detection
ea = ea.replace(
    '   string fx_a1[] = {"AUDUSD","AUDUSDm","AUDUSD.","AUDUSD_"};',
    '   string oil_a[] = {"XTIUSD","WTI","USOIL","CrudeOIL","USOILm","WTIm","XTIUSD.","OIL.WTI"};'
)
ea = ea.replace(
    '   string fx_b1[] = {"NZDUSD","NZDUSDm","NZDUSD.","NZDUSD_"};',
    '   string oil_b[] = {"XBRUSD","BRENT","UKOIL","BrentOIL","UKOILm","BRNm","XBRUSD.","OIL.BRENT"};'
)
ea = ea.replace(
    '   string fx_a2[] = {"EURJPY","EURJPYm","EURJPY.","EURJPY_"};',
    '   // v5.6.3: Only Oil + Index duo (forex dropped — costs eat the edge)'
)
ea = ea.replace(
    '   string fx_b2[] = {"CHFJPY","CHFJPYm","CHFJPY.","CHFJPY_"};',
    ''
)
ea = ea.replace('   AddFirstValid(fx_a1);', '   AddFirstValid(oil_a);')
ea = ea.replace('   AddFirstValid(fx_b1);', '   AddFirstValid(oil_b);')
ea = ea.replace('   AddFirstValid(fx_a2);', '')
ea = ea.replace('   AddFirstValid(fx_b2);', '')

# Update version
ea = ea.replace('#property version   "5.61"', '#property version   "5.63"')
ea = ea.replace('SHF Bridge v5.61', 'SHF Bridge v5.63')

with open('MQL5/Experts/SHF_Bridge.mq5', 'w', encoding='utf-8') as f:
    f.write(ea)

print(f"EA: XTIUSD in DetectSymbols: {'XTIUSD' in ea}")
print(f"EA: XBRUSD in DetectSymbols: {'XBRUSD' in ea}")
print(f"EA: AUDUSD removed: {'AUDUSD' not in ea}")
print(f"EA: version 5.63: {'5.63' in ea}")

print("\nAll v5.6.3 changes applied!")
