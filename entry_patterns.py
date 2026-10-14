"""Causal checks for the two entry sequences approved on 13 October 2026.

Direction and entry timing are independent of the setup family. The chosen
context/levels remain explicit judgments; these checks verify their geometry.
"""
PROFILES = ('manipulation_retracement_inversion', 'htf_fvg_retracement_inversion')


def validate_pattern(packet, selection, trigger, point):
    profile = selection.get('setup_profile')
    if profile not in PROFILES:
        return {}
    ev = packet['evidence']; symbol = selection['analysis_symbol']
    sign = 1 if selection['direction'] == 'long' else -1
    spec = selection.get('setup_sequence')
    if not isinstance(spec, dict):
        raise ValueError('SETUP_SEQUENCE_REQUIRED')

    def item(ref, kinds):
        e = ev.get(ref, {})
        if e.get('symbol') != symbol or e.get('kind') not in kinds:
            raise ValueError('INVALID_SEQUENCE_EVIDENCE')
        return e

    def bar(ref):
        b = item(ref, ('bar',))
        if (b.get('timeframe') != 1 or b.get('time', -1)+60 != b.get('available_at')
            or not all(type(b.get(k)) in (int, float) and b[k] > 0 and b[k]*4 == round(b[k]*4)
                       for k in ('open','high','low','close'))
            or not b['low'] <= min(b['open'],b['close']) <= max(b['open'],b['close']) <= b['high']):
            raise ValueError('CLOSED_M1_SEQUENCE_BAR_REQUIRED')
        return b

    def touched(b, low, high):
        return b['low'] <= high and b['high'] >= low

    def gap(ref, tf=None):
        g = item(ref, ('FVG','HTF_FVG'))
        if tf is not None and g.get('timeframe') != tf:
            raise ValueError('M15_DISPLACEMENT_GAP_REQUIRED')
        refs = g.get('evidence_ids', [])
        bars = [item(r, ('bar',)) for r in refs]
        if len(bars) != 3:
            raise ValueError('DISPLACEMENT_GAP_BARS_REQUIRED')
        a,b,c = sorted(bars, key=lambda x:x['time']); minutes = g['timeframe']
        if (any(x.get('timeframe') != minutes or x['available_at'] != x['time']+minutes*60 for x in (a,b,c))
            or b['time']-a['time'] != minutes*60 or c['time']-b['time'] != minutes*60
            or g['available_at'] != c['available_at']):
            raise ValueError('INVALID_DISPLACEMENT_SEQUENCE')
        side=g.get('side',g.get('direction'))
        if side not in ('bull','bear'):raise ValueError('DISPLACEMENT_DIRECTION_REQUIRED')
        lower,upper = (a['high'],c['low']) if side == 'bull' else (c['high'],a['low'])
        if not lower < upper or (g.get('lower'),g.get('upper')) != (lower,upper):
            raise ValueError('INVALID_DISPLACEMENT_GAP')
        return g, (a,b,c)

    retracement = bar(spec['retracement_bar_id'])
    zone_ref=spec['retracement_zone_id']
    if profile==PROFILES[1]:
        zone, formation=gap(zone_ref,15)
        zone_low,zone_high=zone['lower'],zone['upper']
    else:
        zone=item(zone_ref,('FVG','HTF_FVG','IFVG','bar','window'))
        if zone['kind'] in ('FVG','HTF_FVG'):
            zone,formation=gap(zone_ref)
        if zone['kind']=='window' and zone.get('complete') is not True:
            raise ValueError('RETRACEMENT_RANGE_MUST_BE_COMPLETE')
        zone_low,zone_high=(zone['lower'],zone['upper']) if 'lower' in zone else (zone['low'],zone['high'])
    if (zone['available_at'] > retracement['time'] or retracement['available_at'] > trigger['time']
        or not touched(retracement, zone_low, zone_high)):
        raise ValueError('RETRACEMENT_MUST_TOUCH_SELECTED_ZONE_BEFORE_CONFIRMATION')

    if profile == PROFILES[0]:
        manipulation = spec['manipulation']; mb = bar(manipulation['bar_id'])
        level = point(manipulation['level'], symbol)
        side = manipulation.get('side')
        if (side not in ('above','below') or level['available_at'] > mb['time']
            or (mb['high'] <= level['price'] if side == 'above' else mb['low'] >= level['price'])):
            raise ValueError('MANIPULATION_MUST_BREAK_PREEXISTING_LEVEL')
        structure = spec['structure']; sb = bar(structure['bar_id'])
        swing = item(structure['swing_id'], ('pivot',))
        swing_side = swing.get('side'); role = structure.get('role'); mode = structure.get('mode')
        expected = ('high' if sign == 1 else 'low') if role == 'directional_break' else ('low' if sign == 1 else 'high')
        if role not in ('directional_break','liquidity_sweep') or mode not in ('wick','close'):
            raise ValueError('STRUCTURE_ROLE_AND_BREAK_MODE_REQUIRED')
        if swing_side != expected or swing['available_at'] > sb['time']:
            raise ValueError('STRUCTURE_SWING_SIDE_OR_AVAILABILITY_INVALID')
        value = sb[swing_side if mode == 'wick' else 'close']
        if (value <= swing['price'] if swing_side == 'high' else value >= swing['price']):
            raise ValueError('SELECTED_STRUCTURE_LEVEL_NOT_BROKEN')
        if not mb['time'] <= sb['time'] < retracement['time']:
            raise ValueError('MANIPULATION_STRUCTURE_RETRACEMENT_ORDER_INVALID')
        # A sell-side break can precede a long; the bullish inversion, checked by
        # the compiler, is its entry confirmation. It is not a bullish BOS.
        return dict(setup_sequence_validated=True, structure_role=role,
                    structure_side=swing_side, setup_family=profile)

    objective = point(spec['objective'], symbol)
    anchor = ev[spec['objective']['evidence_id']]
    reached = bar(spec['objective_touch_bar_id'])
    if anchor.get('timeframe') not in (60,240) or objective['available_at'] > reached['time']:
        raise ValueError('PREEXISTING_H1_H4_OBJECTIVE_REQUIRED')
    if not touched(reached, objective['price'], objective['price']):
        raise ValueError('HTF_OBJECTIVE_NOT_REACHED')
    a,b,c = formation
    if (reached['available_at'] > b['time'] or zone.get('side',zone.get('direction')) != ('bull' if sign == 1 else 'bear')
        or sign*(b['close']-b['open']) <= 0):
        raise ValueError('OBJECTIVE_THEN_DIRECTIONAL_M15_DISPLACEMENT_REQUIRED')
    return dict(setup_sequence_validated=True, setup_family=profile,
                objective_price=objective['price'], displacement_gap_id=zone_ref)
