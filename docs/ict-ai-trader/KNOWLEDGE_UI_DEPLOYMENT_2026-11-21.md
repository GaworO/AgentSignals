# Controlled knowledge inspection release — 21 November 2026

Base production commit: `5885cd3fbee6f3bb625120d0af451e8a926760c4`.
Scope: native Knowledge Library, authenticated GET-only SQLite views, safe source links, and viewing already-recorded historical examples. No compiler, strategy engine, broker code, paid transport, startup loop, trading flags or eligibility changes.

Private versioned assets are under `DATA_DIR/ict_ai/library136/72d72f7bcb828c2fc2c4c24b`, selected by `current.json`. The immutable database SHA-256 is `e52a58a117df717f98277e41cf78dcc9f763c9e6a0f293e5c5597b380031ede8`. Twelve files were hash-verified after transfer; SQLite quick_check passed and sources=136. No transcript or private market artifact is committed. The earlier unused embedded catalogue routes/UI are excluded from this production release.

Local validation: 188 relevant regression tests; 13 JavaScriptCore render cases; 136 sources / 52,647 cues / 1,382 pages audited against SQLite; 27 serial and 72 concurrent requests (8 concurrent). Audit-process peak 60.20 MiB; registration 34.30 MiB. These are local measurements, not production soak evidence.

Before release: Railway memory limit 8,000,000,000 bytes, 1 Gunicorn application worker / 8 HTTP threads; process RSS 805–835 MiB, cgroup approximately 1.60–1.63 GB, zero OOM events. Existing ICT AI mode OFF. Legacy broker live flags remain as previously configured; risk evidence is expired and broker snapshot shows no position or working order. No permissions are changed by this inspection release.

Backup: existing `backup_retention.py` completed and verified 842 files / 38 SQLite databases in the sole `backups/recovery-current.tar.gz`. No live journals or earlier evidence were removed.

Deploy only to Agent 50k, not other services sharing the repository. Preserve prior deployment `b3fc99e5-9fec-47ea-a0fe-7baf3d13af76` and prior commit. On missing artifacts, excessive memory or regression, redeploy that exact previous commit; do not restore the volume online or delete data.

Production smoke and post-deploy measurements are recorded separately after deployment. This document is a predeploy record, not a claim that production verification has passed.
