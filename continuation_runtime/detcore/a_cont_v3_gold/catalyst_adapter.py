"""Causal registered/local-liquidity catalyst adapter.

Raid/reclaim is deliberately absent. A catalyst is emitted only on a close
through the directional edge of a pool that existed before the breaking bar.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .types import Bar, Catalyst, DOLSnapshot, Direction, LiquidityPool, Subtype

SnapshotProvider = Callable[[int, Direction, tuple[LiquidityPool, ...]], DOLSnapshot]


def _unavailable_snapshot(
    bar_index: int, direction: Direction, pools: tuple[LiquidityPool, ...]
) -> DOLSnapshot:
    del direction, pools
    return DOLSnapshot.unavailable(
        bar_index,
        "frozen ranked-DOL runtime is absent from the authoritative repository; "
        "the typed metadata interface is preserved and no DOL execution gate is applied",
    )


class CatalystAdapter:
    """Detect one-shot close-throughs of immutable liquidity pools."""

    def __init__(
        self,
        pools: Iterable[LiquidityPool] = (),
        *,
        snapshot_provider: SnapshotProvider | None = None,
    ) -> None:
        self._pools: dict[str, LiquidityPool] = {pool.pool_id: pool for pool in pools}
        self._consumed: set[str] = set()
        self._snapshot_provider = snapshot_provider or _unavailable_snapshot
        self._penetrated: dict[str, int] = {}
        self._resolved_context: set[str] = set()
        self._context_at: dict[Direction, int] = {}
        self._bars: dict[int, Bar] = {}
        # A registered or local catalyst begins directional delivery.  A later
        # local break in that direction needs a fresh, causal reset.
        self._last_delivery_at: dict[Direction, int] = {}

    @property
    def pools(self) -> tuple[LiquidityPool, ...]:
        return tuple(sorted(self._pools.values(), key=lambda p: (p.available_at, p.pool_id)))

    def register(self, pool: LiquidityPool) -> None:
        existing = self._pools.get(pool.pool_id)
        if existing is not None and existing != pool:
            raise ValueError(f"immutable pool ID reused with different geometry: {pool.pool_id}")
        self._pools[pool.pool_id] = pool

    def _remember(self, previous: Bar | None, bar: Bar) -> None:
        if previous is not None:
            self._bars.setdefault(previous.index, previous)
        self._bars[bar.index] = bar

    def _registered_alignment(self, pool: LiquidityPool, bar_index: int) -> bool:
        """Whether a local pool overlaps still-open registered liquidity."""
        for registered in self.pools:
            if (
                registered.kind == "confirmed_local"
                or registered.side != pool.side
                or registered.status != "OPEN"
                or registered.pool_id in self._consumed
                or registered.available_at >= bar_index
            ):
                continue
            if pool.lower <= registered.upper and registered.lower <= pool.upper:
                return True
            if registered.pool_id in pool.constituent_ids:
                return True
        return False

    def _balance_opposite(
        self, pool: LiquidityPool, bar_index: int
    ) -> LiquidityPool | None:
        """Return the opposite boundary of the latest closed local balance.

        A balance is defined structurally: both opposing local boundaries were
        already published and every intervening completed close remained
        inside them.  No reaction-count, age, ATR, or future boundary is used.
        """
        opposite_side = "SSL" if pool.side == "BSL" else "BSL"
        matches: list[tuple[int, LiquidityPool]] = []
        for opposite in self.pools:
            if (
                opposite.kind != "confirmed_local"
                or opposite.side != opposite_side
                or opposite.pool_id in self._consumed
                or opposite.available_at >= bar_index
            ):
                continue
            lower = opposite.lower if pool.side == "BSL" else pool.lower
            upper = pool.upper if pool.side == "BSL" else opposite.upper
            if lower >= upper:
                continue
            established_at = max(pool.available_at, opposite.available_at)
            closed = [
                self._bars[index]
                for index in sorted(self._bars)
                if established_at <= index < bar_index
            ]
            if not closed or any(item.close < lower or item.close > upper for item in closed):
                continue
            matches.append((established_at, opposite))
        if not matches:
            return None
        return max(matches, key=lambda item: (item[0], item[1].available_at, item[1].pool_id))[1]

    def _cllc_eligible(
        self, pool: LiquidityPool, direction: Direction, bar_index: int
    ) -> bool:
        """Apply the frozen two-condition CLLC semantic gate."""
        opposite = self._balance_opposite(pool, bar_index)
        externally_legible = opposite is not None or self._registered_alignment(pool, bar_index)
        if not externally_legible:
            return False

        last_delivery = self._last_delivery_at.get(direction, -1)
        raid_reset = self._context_at.get(direction, -1)
        raid_rearmed = last_delivery < raid_reset < bar_index
        # For a repeated same-direction break, the counter-directional boundary
        # itself must have become available after the previous delivery began.
        balance_rearmed = opposite is not None and opposite.available_at > last_delivery
        return raid_rearmed or balance_rearmed

    def on_close(self, previous: Bar | None, bar: Bar) -> tuple[Catalyst, ...]:
        if previous is None:
            return ()
        self._remember(previous, bar)
        # Raid/reclaim/failure is retained only as prior directional context.
        # It never emits a catalyst on its own. Context persists causally until
        # superseded by a later opposite resolved interaction.
        for pool in self.pools:
            if pool.available_at >= bar.index or pool.status not in {"OPEN", "DELIVERED"}:
                continue
            if pool.side == "SSL" and bar.low < pool.lower:
                self._penetrated.setdefault(pool.pool_id, bar.index)
            elif pool.side == "BSL" and bar.high > pool.upper:
                self._penetrated.setdefault(pool.pool_id, bar.index)
            if pool.pool_id not in self._penetrated or pool.pool_id in self._resolved_context:
                continue
            if pool.side == "SSL" and bar.close > pool.upper:
                self._context_at[Direction.LONG] = bar.index
                self._resolved_context.add(pool.pool_id)
            elif pool.side == "BSL" and bar.close < pool.lower:
                self._context_at[Direction.SHORT] = bar.index
                self._resolved_context.add(pool.pool_id)

        crossed: dict[Direction, list[LiquidityPool]] = {
            Direction.LONG: [],
            Direction.SHORT: [],
        }
        for pool in self.pools:
            if (
                pool.pool_id in self._consumed
                or pool.status not in {"OPEN", "DELIVERED"}
                or pool.available_at >= bar.index
            ):
                continue
            if pool.side == "BSL" and previous.close <= pool.upper < bar.close:
                crossed[Direction.LONG].append(pool)
            elif pool.side == "SSL" and previous.close >= pool.lower > bar.close:
                crossed[Direction.SHORT].append(pool)

        out: list[Catalyst] = []
        for direction in (Direction.LONG, Direction.SHORT):
            raw_hit = crossed[direction]
            if not raw_hit:
                continue

            registered_hit = [p for p in raw_hit if p.kind != "confirmed_local"]
            if registered_hit:
                # Preserve registered-catalyst behaviour.  A simultaneous local
                # source may join the source set under the prior closed-context
                # rule, but the physical event remains RLC.
                hit = [
                    p
                    for p in raw_hit
                    if p.kind != "confirmed_local"
                    or self._context_at.get(direction, bar.index) < bar.index
                ]
                subtype = Subtype.RLC
            else:
                hit = [
                    p
                    for p in raw_hit
                    if self._cllc_eligible(p, direction, bar.index)
                ]
                subtype = Subtype.CLLC

            # A close-through consumes the physical local object even when it
            # fails the semantic gate; it cannot be resurrected by a later reset.
            for pool in raw_hit:
                self._consumed.add(pool.pool_id)
            if not hit:
                continue
            # OPEN/DELIVERED is DOL narrative metadata, not a V3 profitability
            # gate. All registered objects remain eligible unless invalidated.
            # All levels physically crossed by the same directional close are one
            # catalyst source-set. Successive later closes can create new events.
            hit.sort(key=lambda p: (p.lower, p.upper, p.pool_id))
            pool_ids = tuple(p.pool_id for p in hit)
            dol = self._snapshot_provider(bar.index, direction, tuple(hit))
            out.append(
                Catalyst(
                    catalyst_id=f"CAT|{direction.value}|{bar.index}|{'|'.join(pool_ids)}",
                    subtype=subtype,
                    direction=direction,
                    pool_ids=pool_ids,
                    bar_index=bar.index,
                    lower=min(p.lower for p in hit),
                    upper=max(p.upper for p in hit),
                    dol=dol,
                )
            )
            self._last_delivery_at[direction] = bar.index
        return tuple(out)


class ConfirmedLocalLiquidity:
    """Causal k-fractal publisher used only by the CLLC subtype.

    A pivot at ``p`` is published at close ``p + k`` and therefore cannot be
    broken by the candle which first makes it known.
    """

    def __init__(self, strength: int = 2) -> None:
        if strength < 1:
            raise ValueError("local pivot strength must be positive")
        self.strength = strength
        self._published: set[str] = set()

    def on_close(self, history: list[Bar]) -> tuple[LiquidityPool, ...]:
        k = self.strength
        if len(history) < 2 * k + 1:
            return ()
        p = len(history) - 1 - k
        pivot = history[p]
        window = history[p - k : p + k + 1]
        out: list[LiquidityPool] = []
        if pivot.high == max(x.high for x in window) and sum(x.high == pivot.high for x in window) == 1:
            pid = f"LOCAL_BSL|{pivot.index}|{pivot.high:.8f}"
            if pid not in self._published:
                self._published.add(pid)
                out.append(
                    LiquidityPool(pid, "BSL", pivot.high, pivot.high, history[-1].index,
                                  kind="confirmed_local", formed_at=pivot.index)
                )
        if pivot.low == min(x.low for x in window) and sum(x.low == pivot.low for x in window) == 1:
            pid = f"LOCAL_SSL|{pivot.index}|{pivot.low:.8f}"
            if pid not in self._published:
                self._published.add(pid)
                out.append(
                    LiquidityPool(pid, "SSL", pivot.low, pivot.low, history[-1].index,
                                  kind="confirmed_local", formed_at=pivot.index)
                )
        return tuple(out)
