#!/usr/bin/env python3
"""
instruments.py — per-instrument calibration for Strategy D.

Two knobs make the MNQ method run on other instruments:

1. price_mult — multiply OHLC so prices land in the index-like range (~1e4-1e5). The detector
   HARD-CODES round(price, 1) / round(price, 2) for FVG levels, tuned for index prices in the
   thousands. At low nominal prices it destroys the structure: EURUSD (~1.07) rounds every level to
   the 0.1 grid -> 0 valid setups. Multiplying restores precision; R is scale-invariant so results
   stay valid. Rule of thumb: pick mult so median 1-min range lands in ~[1, 50] units.

2. volatility scale — the detector's point thresholds (tol/buf/vimin/vibig/max_stop_r/equals_tol) are
   auto-rescaled by (instrument median 1-min range) / (MNQ median 1-min range = 5.25), computed on the
   ALREADY-multiplied data.

point_value / tick_dollars feed only the $ cost model (cost-in-R), never the gross edge. For forex the
real cost is the SPREAD (~0.1-0.2 pip on majors); see strategy_d cost notes. VERIFY all specs.
"""

MNQ_REF_RANGE = 5.25
MNQ_THRESHOLDS = dict(tol=3.0, buf=3.0, vimin=10.0, vibig=50.0, max_stop_r=40.0, equals_tol=4.0)

INSTRUMENTS = {
    'mnq':    dict(name='MNQ (Micro Nasdaq)', price_mult=1,      point_value=2.0,  tick_dollars=0.50,
                   note='reference; scale=1.0 reproduces production'),
    'gold':   dict(name='Gold (MGC micro)',   price_mult=100,    point_value=10.0, tick_dollars=1.00,
                   note='price~2000-4500; mult=100 keeps rounding fine (early-2023 low-vol was coarse at mult=1). VERIFY MGC specs.'),
    'eurusd': dict(name='EUR/USD',            price_mult=100000, point_value=10.0, tick_dollars=0.0,
                   note='forex major; cost is SPREAD ~0.1-0.2 pip (1 pip = 10 units after mult). NDOG/NWOG mostly inert (24/5).'),
    'gbpusd': dict(name='GBP/USD',            price_mult=100000, point_value=10.0, tick_dollars=0.0,
                   note='forex major; higher vol than EURUSD, slightly wider spread. Untested — drop in data.'),
    'usdjpy': dict(name='USD/JPY',            price_mult=100,    point_value=10.0, tick_dollars=0.0,
                   note='forex major, JPY-quoted (~150, 3dp) so mult=100 (150->15000, median 1m range ~1.3u in [1,50]). '
                        '1 pip = 0.01 = 1 UNIT after mult (vs 10u on 5dp EUR/GBP). Cost is SPREAD ~0.5-1.0 pip; '
                        'model like eurusd via per-trade spread_units/risk. Tightest stops of the FX set -> cost-sensitive.'),
    'oil':    dict(name='Oil (MCL micro WTI)',price_mult=1000,   point_value=100.0, tick_dollars=1.00,
                   note='PLACEHOLDER — price~70 so mult~1000; MCL specs unverified.'),
}
