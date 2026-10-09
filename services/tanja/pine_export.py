"""Visual-only exports of frozen research plans. No inferred fills or orders."""
import json
import math


def export_plan(record, ticker):
    if not record or record.get('state') != 'PLAN_READY':
        raise ValueError('No complete research plan to export')
    compiled=record.get('compiled') or {}
    plan=compiled.get('plan') or {}
    review=compiled.get('price_review') or {}
    if plan.get('symbol') != 'MNQ' or plan.get('direction') != 'long' or review.get('errors') or review.get('missing'):
        raise ValueError('Plan did not pass price checks')
    entry=(plan.get('entry_reference') or {}).get('price')
    stop,target=review.get('stop'),review.get('target')
    if not all(type(v) in (int,float) and math.isfinite(v) and v>0 for v in (entry,stop,target)) or not stop<entry<target:
        raise ValueError('Invalid export prices')
    # Draw only from the time the recorded plan was available, never at a prior trigger.
    start=math.ceil(max(record['cutoff'],record['frozen_at'],plan['selected_at'])*1000)
    end=plan.get('context_valid_until')
    if type(end) is not int or end*1000<=start:
        raise ValueError('Missing or expired display interval')
    quote=lambda value:json.dumps(str(value),ensure_ascii=True)
    return f'''//@version=6
indicator("Tanja research plan - NOT a filled trade", overlay=true, max_lines_count=10, max_labels_count=10)
// Frozen research evidence. No strategy orders, signals or simulated fills.
// Use the same MNQ chart series and contract adjustment settings as the source.
// Entry is a reference price; no broker entry, partial or exit is verified.
// Plan ID: {str(plan.get('plan_id','')).replace(chr(10),' ').replace(chr(13),' ')}
int planAvailable = {start}
int planExpires = {end*1000}
string sourceTicker = {quote(ticker)}
var table status = table.new(position.top_right, 1, 1)
if barstate.isfirst
    if syminfo.tickerid != sourceTicker
        runtime.error("Open the source chart: " + sourceTicker)
    line.new(planAvailable, {entry}, planExpires, {entry}, xloc=xloc.bar_time, color=color.blue, width=2)
    line.new(planAvailable, {stop}, planExpires, {stop}, xloc=xloc.bar_time, color=color.red, width=2)
    line.new(planAvailable, {target}, planExpires, {target}, xloc=xloc.bar_time, color=color.green, width=2)
    label.new(planAvailable, {entry}, "Planned entry reference: {entry}", xloc=xloc.bar_time, style=label.style_label_left, color=color.blue, textcolor=color.white)
    label.new(planAvailable, {stop}, "Planned stop: {stop}", xloc=xloc.bar_time, style=label.style_label_left, color=color.red, textcolor=color.white)
    label.new(planAvailable, {target}, "Planned target: {target}", xloc=xloc.bar_time, style=label.style_label_left, color=color.green, textcolor=color.white)
    table.cell(status, 0, 0, "RESEARCH PLAN ONLY\\nNo verified broker fills or exits", bgcolor=color.new(color.orange, 15), text_color=color.black)
'''
