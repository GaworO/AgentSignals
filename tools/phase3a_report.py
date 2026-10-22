"""Build a self-contained local Phase 3A HTML report from saved evidence."""
import html
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
esc=lambda x:html.escape(str(x))


def render(folder):
    folder=Path(folder)
    research=json.loads((folder/'research.json').read_text())
    demo=json.loads((folder/'lifecycle-demo.json').read_text())
    verification=json.loads((folder/'verification.json').read_text())
    labels={'ORIGINAL_V3':'Original fixed 2R','BOUNDARY_BEFORE_RUNNER':'Boundary early exit',
            'RECOVERY_RUNNER_BRANCH_ONLY':'Selective 3R only','BOUNDARY_FOUR_STATE':'Boundary + selective 3R'}
    metrics=research['periods']['OLDER_580']
    rows=''.join(f'<tr><th>{name}</th><td>{metrics[key]["net_pf"]:.4f}</td>'
        f'<td>{metrics[key]["win_rate"]:.2%}</td><td>${metrics[key]["expectancy_usd"]:,.2f}</td>'
        f'<td>${metrics[key]["closed_trade_drawdown_usd"]:,.2f}</td></tr>' for key,name in labels.items())
    steps=''
    for index,step in enumerate(demo['steps'],1):
        s=step['state'];legs=s['protective_orders']
        protection=' · '.join(f'{l["kind"]} {l["price"]} ×{l["quantity"]} ({l["status"]})' for l in legs)
        steps+=f'''<article class="step"><span class="n">{index:02}</span><div><h3>{esc(step['step'])}</h3>
        <span class="badge">{esc(s['state'])}</span><p>Remaining: <b>{s['remaining_quantity']}</b> ·
        Initial risk: ${s['initial_risk_usd']:.2f} · Broker clock: {step['at_ms']} synthetic ms</p>
        <p>{esc(protection)}</p><details><summary>Exact simulated state and event</summary>
        <pre>{esc(json.dumps(step,indent=2))}</pre></details></div></article>'''
    audit=(ROOT/'ICT_POSITION_MANAGER_PHASE3A.md').read_text()
    # Render the concise original/proposed table from the audited Markdown.
    lines=audit.splitlines();start=next(i for i,l in enumerate(lines) if l.startswith('| Capability'))
    table=[]
    for line in lines[start:]:
        if not line.startswith('|'):break
        if line.startswith('|---'):continue
        cells=line.strip('|').split('|')
        table.append('<tr>'+''.join('<td>'+esc(c.strip())+'</td>' for c in cells)+'</tr>')
    document=f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><title>Directional · Phase 3A</title>
    <style>
    :root{{font-family:system-ui,-apple-system,sans-serif;color:#e8edf5;background:#0d1420;color-scheme:dark}}
    body{{margin:0}}main{{max-width:1120px;margin:auto;padding:48px 24px 80px}}h1{{font-size:40px;line-height:1.12;letter-spacing:-1px;margin:12px 0 18px}}
    h2{{font-size:25px;margin:38px 0 16px}}h3{{font-size:17px;margin:0 0 12px}}p,li{{line-height:1.65;color:#bfcbdc}}a{{color:#8fc7ff}}
    .eyebrow{{text-transform:uppercase;letter-spacing:2px;font-size:12px;color:#84c4ce}}.lead{{max-width:800px;font-size:19px}}
    .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;margin:28px 0}}
    .card,.step,.notice{{background:#172335;border:1px solid #2d405b;border-radius:12px;padding:20px}}
    .card strong{{display:block;font-size:28px;color:#a7e2d1}}.card small{{display:block;margin-top:8px;color:#c0ccda}}
    .notice{{border-left:4px solid #f2b85d}}.badge{{display:inline-block;font-size:12px;letter-spacing:.5px;background:#274359;color:#b8e0ff;padding:6px 9px;border-radius:5px}}
    .scroll{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;background:#142031}}td,th{{padding:13px;text-align:left;border-bottom:1px solid #314057;vertical-align:top;font-size:14px;line-height:1.5}}th{{font-weight:600}}
    .step{{display:grid;grid-template-columns:44px 1fr;gap:18px;margin:14px 0}}.n{{font-size:24px;color:#6daab8}}
    details{{margin-top:14px}}summary{{cursor:pointer;color:#91c9ff}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.65 ui-monospace,monospace;color:#bfcbdc;background:#0e1927;padding:18px;border-radius:8px;max-height:520px;overflow:auto}}
    .footer{{font-size:12px;color:#8295ac;margin-top:40px}}@media(max-width:600px){{main{{padding:28px 14px}}h1{{font-size:30px}}.step{{grid-template-columns:1fr}}}}
    </style></head><body><main>
    <div class="eyebrow">AgentSignals / Directional / 22 October 2026</div>
    <h1>ICT Position Manager<br>Phase 3A foundation</h1>
    <p class="lead">Persistent broker observations, deterministic management proposals and offline lifecycle verification. Existing Directional entry rules and A/B shadow routing are preserved.</p>
    <span class="badge">LOCAL ONLY</span> <span class="badge">REAL MANAGEMENT WRITES BLOCKED</span>
    <div class="cards"><div class="card"><strong>{verification['focused_tests_passed']}</strong><small>Focused tests passed, plus {verification['subtests_passed']} subtests</small></div>
    <div class="card"><strong>{verification['manager_tests_passed']}</strong><small>New manager cases; all 12 requested lifecycle categories</small></div>
    <div class="card"><strong>0</strong><small>Paid AI calls / real orders / deployments in Phase 3A</small></div>
    <div class="card"><strong>580</strong><small>Same historical entries compared; not a new holdout</small></div></div>
    <div class="notice"><b>Broker-side management is not verified.</b><p>The real adapter refuses every partial, stop/target change and runner action. Simulated bracket resizing below is not evidence that CrossTrade can safely perform that operation on the MFFU account.</p></div>
    <h2>What changed</h2><div class="scroll"><table>{''.join(table)}</table></div>
    <p>New journals are connected to the existing broker reconciler. They keep owned fills, protection, quantities and costs across restarts. Closed-candle proposals require fresh quotes, risk and broker evidence. No production rule scheduler or real management writer has been activated.</p>
    <h2>Illustrative position lifecycle</h2><p><b>Entirely simulated.</b> Example LONG: 2 contracts, entry 100, SL 90, original TP 120. One contract at 110, runner to 130. These prices and quantities demonstrate protocol behaviour; they are not an approved live policy or an actual trade.</p>
    {steps}
    <h2>Historical results do not justify promotion</h2>
    <p>Saved 2019–2022 outcomes, same 580 entries and quantities. Net figures include the archive's $3.50 round-trip cost per contract. Fixed 2R performs best among these four variants, but all have PF below 1.</p>
    <div class="scroll"><table><thead><tr><th>Archived policy</th><th>Net PF</th><th>Net win rate</th><th>Expectancy / trade</th><th>Realized DD</th></tr></thead><tbody>{rows}</tbody></table></div>
    <p>Partial take-profit and structural trailing: <b>not measured</b>, because no matched causal outcome ledger defines those rules. The later archive contains only one filled trade.</p>
    <ul>{''.join('<li>'+esc(x)+'</li>' for x in research['limitations'])}</ul>
    <h2>Test scope and the legacy failures</h2>
    <p>The focused suites passed {verification['focused_tests_passed']} tests plus {verification['subtests_passed']} subtests. The separate expanded audit found 18 failures in unchanged legacy one-second-candle tests. The runtime deliberately rejects that obsolete input. Those failures are retained, not hidden, in the linked evidence.</p>
    <h2>Phase 3B: required before any real management</h2>
    <ol><li>Supported isolated paper account, verified executable quotes, actual SL/TP/OCO topology and repaired-child ownership.</li>
    <li>Exact command ACK → report → working-order verification, including rejection, timeout, restart and duplicate events.</li>
    <li>Prove safe partial-close and bracket-resize sequencing. Keep partials blocked if continuous protection cannot be established.</li>
    <li>Coordinate the existing Phase 2B geometry validator and all writers so only confirmed owned modifications are accepted.</li>
    <li>Specify and validate partial/runner/trailing/invalidation rules; wire causal events and recheck Guard/account-wide risk before each potential operation.</li>
    <li>Complete broker-paper evidence for all lifecycles. Deployment and live activation remain separate, with explicit approval.</li></ol>
    <h2>Evidence and complete audit</h2><p><a href="ICT_POSITION_MANAGER_PHASE3A.md">Full audit and Phase 3B checklist</a> · <a href="research.json">Research metrics + source hashes</a> · <a href="lifecycle-demo.json">Exact simulated lifecycle</a> · <a href="verification.json">Verification manifest</a> · <a href="tests.xml">Focused JUnit</a> · <a href="entry-tests.xml">Entry rules JUnit</a> · <a href="expanded-tests.xml">Expanded audit JUnit</a> · <a href="legacy-check.json">Legacy failure evidence</a></p>
    <details><summary>Read the complete audit here</summary><pre>{esc(audit)}</pre></details>
    <p class="footer">Base d4ff5ec. No Railway access or production mutations in this phase. Code: directional_position_manager.py · directional_management_broker.py · existing reconciliation hooks. Git commit is recorded separately in delivery.json.</p>
    </main></body></html>'''
    (folder/'ICT_POSITION_MANAGER_PHASE3A.md').write_text(audit)
    (folder/'report.html').write_text(document)
    return folder/'report.html'


if __name__=='__main__':print(render(sys.argv[1]))
