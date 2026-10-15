# LiveScript readiness — 2026-10-11

**NO-GO for unattended Tanja ingestion today.** Technical transport is documented;
source/use authorization, API access and recurring discovery are not established.
This is a documentation and local-code review, not a live API validation.

| Check | Finding |
|---|---|
| Public stream not owned by us | Provider advertises public YouTube livestream support. The documented subscription has no broadcaster OAuth/ownership field. This establishes a technical claim, not rights to process Tanja's content. |
| Active broadcasts | Documented live WebSocket; completed videos use separate REST retrieval. |
| Continuous delivery | `wss://livescript.live/api/v1/live`, Bearer key, subscribe by video ID. Stable segment IDs, increasing revisions, interim replacement and final segments. No transcript webhook documented. |
| Laptop off | A server WebSocket client is technically possible; not tested on Railway. |
| Recurring session discovery | Channel alerts run server-side but are email-based, one-shot, and require manual reactivation. No channel-discovery API/webhook documented. |
| Price | Web plans advertise Free: 5 live minutes/month then five-minute delayed feed; Day Pass $4.99/24h, 1,000 minutes; Pro $19.99/month, 10,000 minutes. API entitlement/limits need account verification. |
| Limits | Web pricing advertises unlimited paid concurrent streams; API usage example shows one. Treat that example as illustrative; actual allowance comes from authenticated `/api/v1/usage`. REST rate limits/top-up prices are not established by reviewed docs. |
| Latency | Marketing claims under one second; FAQ says 1–2 seconds. Neither is a measured Tanja/API end-to-end result or final-segment SLA. API messages lack source wall-clock speech time, so offsets alone cannot measure speech-to-database latency. |
| Credentials | No local `LIVESCRIPT_API_KEY`. Read-only Railway variable lookup was unavailable; remote credentials unverified. No provider API request made. |
| Terms | Provider requires lawful rights to process content and written consent for commercial transcript use/reproduction. It also restricts automated extraction absent consent, despite publishing an API; clarify API authorization scope. YouTube restricts automated access and independent content use. Public availability alone does not establish compatibility. |

Sources reviewed:

- [Introduction](https://livescript.live/documentation/introduction)
- [API reference](https://livescript.live/documentation/api)
- [Authentication](https://livescript.live/documentation/authentication)
- [Pricing and latency FAQ](https://livescript.live/)
- [Channel alerts](https://livescript.live/youtube-live-alerts)
- [LiveScript terms](https://livescript.live/terms), sections 5, 7 and 8
- [YouTube terms](https://www.youtube.com/static?template=terms), permissions/restrictions and license to other users

## Existing integration fit

Reviewed only `services/tanja/transcript_ingestion.py`. Existing
`TranscriptJournal.ingest` and final-to-Shadow journaling can be reused, but an
adapter must preserve LiveScript's semantics: interim text is a revised complete
segment, not an append-only delta. Preserve raw provider messages, IDs, revisions,
source offsets and local receipt time; deterministic event identity can use video
ID/segment ID/revision. Only stable finals should enter the existing final Shadow
path. Duplicate finals must remain idempotent. Reject conflicting revisions and
source mismatches. Reconnect must record gaps: the documented subscription has
no resume cursor or replay guarantee. This mapping has not been implemented or
validated because the requested prerequisites are unresolved.

## Smallest next step

Obtain provider confirmation covering unattended API ingestion of this third-party
channel, permitted transcript storage/Shadow research and applicable content rights;
confirm whether additional broadcaster/YouTube permission is needed. Also ask for
API plan/concurrency limits and a recurring discovery mechanism or documented live
video-ID feed. Configure an entitled `ls_live_` API key securely, then approve a
short active-stream test with a capped minute budget.

No adapter or fabricated provider fixture was added. No live transcript-to-database
success, measured latency or Railway reachability is claimed. No paid calls,
deployment, runtime setting changes, broker requests or execution activation.
Only this report was added for the LiveScript task; existing interpreter work was
left as it stood when the user switched tasks.
