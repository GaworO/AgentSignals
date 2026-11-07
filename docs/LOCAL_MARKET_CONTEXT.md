# Agent50k local ES/NQ/MNQ context

This release moves the existing deterministic Tanja context engine into Agent50k. ES remains an analysis/comparison input, NQ the analysis instrument, MNQ the execution-price context. It does not add a strategy or change trading permissions, risk, AI models or budgets.

Enable with MARKET_CONTEXT_ENABLED=true and AB_CONTEXT_SOURCE=LOCAL. MARKET_CONTEXT_FEED_TOKEN must equal the old Tanja feed token. MARKET_CONTEXT_ES_TICKER, MARKET_CONTEXT_NQ_TICKER and MARKET_CONTEXT_MNQ_TICKER must match the immutable database bindings. MARKET_CONTEXT_REFERENCE_CONTRACTS preserves the dated alert intake as separate observations, not executable quotes. Existing source/session configuration and calendar rules are preserved.

Database: DATA_DIR/market_context/tanja.sqlite3. Seed from a checksum-verified snapshot before switching source. It retains the raw candles, receipt times and immutable duplicates. A new context is scheduled only after all three symbols arrive for the same closed minute. The worker reads only candles closed and received by that decision cutoff. Only 16 full deterministic context snapshots are retained, plus 14 days of job metadata; paid-review input/output journals remain unchanged. Raw candles are not deleted by this worker.

The Tanja service runs services/tanja/relay.py only. The existing /feed/<token> and /reference/<token> URLs forward to Agent50k over Railway private networking. No old AI, scanners, transcript, reference monitor, broker capture or trading loop starts. Reference intake remains observation-only; it cannot authorize price translation or orders. No broker polling is introduced to refresh the retired Tanja reference-repair binding.

The relay returns success only after Agent50k accepts the payload. It does not retry uncertain requests; immutable duplicate handling is in the receiving database. Connection failures return 503; downstream validation errors remain errors. Existing freshness and completeness checks block stale AI context. TradingView delivery without retries can still lose an alert during a disconnect; missing candles are explicit, never fabricated. GET /market-data/state uses the existing Basic authentication; GET /health on the relay reports RELAY_ONLY. Do not log private webhook paths.

OpenAI credentials are stored directly on Agent50k with unchanged values, rather than referencing Tanja. The relay needs no OpenAI or broker credentials. Do not remove an old variable until all cross-service references have been detached and verified.

Deployment order: seed and verify the new database; stage data-source settings only; deploy Agent50k from the consolidated GitHub branch; verify local context and unchanged effective permissions; deploy the relay on the existing Tanja service from the same branch. Keep its root directory /services/tanja. No new service. Do not call AI or submit orders as migration tests.

Rollback: retain the pre-migration Tanja image and compressed, checksum-verified SQLite backup. Restore the old Tanja process/data and its original source branch first, verify /api/state, then set AB_CONTEXT_SOURCE=TANJA and MARKET_CONTEXT_ENABLED=false on Agent50k and deploy. Keep all execution/risk settings unchanged. Do not restore an old trading database or old execution flags. Rollback images/source must be inspected before deployment; future unrelated changes must not be overwritten.

Parity evidence and migration measurements are in audit/context_migration_20261107 in the operator workspace. Compare AI-consumed semantic fields rather than packet IDs: the unused top-level setup_scanner output is deliberately absent, so new packet hashes can differ. Existing setup/approval binding remains mandatory.
