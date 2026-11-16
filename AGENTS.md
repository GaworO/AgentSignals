# Agent50k deployment and storage

- Preserve concurrent changes: fetch and compare the current deployment branch before pushing. Do not force-push or deploy an older release.
- User-authorized backup retention is **one completed recovery backup**. Do not create additional timestamped `pre-*` backup directories or archives on the Railway volume.
- Before a deployment that needs a backup, run `python backup_retention.py --root /data/builder50` in the existing Agent50k container. It replaces `backups/recovery-current.tar.gz` only after verification. A temporary replacement may coexist during creation; the previous verified copy survives a failed attempt.
- Never delete live trading databases, broker/AI journals, paid-call ledgers, fills, fees, policy/risk evidence or market data to free space. The backup tool uses online SQLite snapshots and preserves all non-transient live files. Restore offline and reconcile broker state; individual database snapshots are not an atomic snapshot of the entire trading system.
- Do not use deployment/cleanup as permission to change trading flags, risk parameters or send test orders.
