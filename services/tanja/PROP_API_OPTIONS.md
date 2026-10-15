# Prop API research — October 14, 2026

## Confirmed conditional option: TopstepX / ProjectX

[Topstep's current API guide](https://help.topstep.com/en/articles/11187768-topstepx-api-access)
explicitly permits custom automated strategies, live/historical market data,
direct orders, REST and WebSocket access. It lists API access at $29/month or
$14.50/month with its `topstep` discount code.

Two material limits: the ProjectX API does not support Topstep Live Funded
Accounts, and trading/order transmission must originate on a personal device.
VPS/remote-server order triggers, relays and order management are prohibited.
This is a candidate for eligible simulated accounts and Practice testing, with
the decision/execution loop moved to the user's computer. It is not a fit for
the current Railway trading architecture or an API route into Live Funded.

No account or subscription was purchased, support message sent, credentials
requested or broker connection changed.

## Other routes checked, not confirmed for this deployment

[ProjectX](https://www.projectx.com/api) documents a trader API, but its existence
does not establish an individual firm's current account eligibility or rules.

[My Funded Futures' automation policy](https://help.myfundedfutures.com/en/articles/8444599-fair-play-and-prohibited-trading-practices)
allows qualifying automation and excludes HFT/simulation exploitation. This
does not establish direct API credentials for the user's Builder account.

[Tradovate's API access requirements](https://tradovate.zendesk.com/hc/en-us/articles/4408927027987-How-Do-I-Get-Access-to-the-Tradovate-API)
describe a funded personal LIVE Tradovate account and a paid add-on. They do not
prove that a Tradovate prop/evaluation account inherits API access.

Tradeify/BluSky/The Futures Desk searches did not establish a current direct
trader API route compatible with Railway and actual live funded execution.
They are not listed as verified alternatives. A firm-specific supported API,
account phase and permitted hosting must be established before choosing an
execution adapter; platform branding or automation permission alone is insufficient.
