"""A/B allocation within the existing SETUP_GROUP_RISK_USD ceiling.

Change SHALLOW_RISK_SHARE and redeploy to change the split:
  0.00 -> deep $750.00, shallow   $0.00 (this upload's default)
  0.25 -> deep $562.50, shallow $187.50
  0.50 -> deep $375.00, shallow $375.00
Amounts above assume SETUP_GROUP_RISK_USD=750 and sufficient guard capacity.
AB_SHALLOW_ENABLED must also be 1 for a nonzero shallow allocation.
Supported shares: 0 through 0.5. Stops and targets are not changed.
"""

SHALLOW_RISK_SHARE = 0.00
DEFAULT_GROUP_RISK_USD = 750.0
