"""Cost guard (spec 10): per-run token cap and a daily USD budget, tracked in `runs`."""

from __future__ import annotations

from datetime import datetime, time
from functools import cache
from pathlib import Path

import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobpilot.core.timeutil import utcnow
from jobpilot.db.models import Run
from jobpilot.llm.base import Usage, UsageMeter

PRICING_FILE = Path(__file__).resolve().parents[1] / "data" / "pricing.yaml"


@cache
def _pricing() -> tuple[dict[str, tuple[float, float]], tuple[float, float]]:
    data = yaml.safe_load(PRICING_FILE.read_text(encoding="utf-8"))
    models = {k: (float(v[0]), float(v[1])) for k, v in (data.get("models") or {}).items()}
    default = data.get("default", [1.0, 5.0])
    return models, (float(default[0]), float(default[1]))


def price_for(model: str) -> tuple[float, float]:
    models, default = _pricing()
    return models.get(model, default)


def estimate_cost(model: str, usage: Usage) -> float:
    if model in {"none", "fake-1"}:
        return 0.0
    pin, pout = price_for(model)
    return round((usage.input_tokens * pin + usage.output_tokens * pout) / 1_000_000, 6)


class CostGuard:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        model: str,
        *,
        per_run_tokens: int,
        daily_budget_usd: float,
    ) -> None:
        self._sessions = sessions
        self.model = model
        self._per_run = per_run_tokens
        self._daily = daily_budget_usd

    async def spent(self, since: datetime) -> float:
        async with self._sessions() as session:
            value = (
                await session.execute(
                    select(func.coalesce(func.sum(Run.est_cost_usd), 0.0)).where(
                        Run.started_at >= since
                    )
                )
            ).scalar_one()
        return float(value)

    async def spent_today(self) -> float:
        now = utcnow()
        return await self.spent(datetime.combine(now.date(), time(0), tzinfo=now.tzinfo))

    async def meter_for_run(self) -> UsageMeter:
        """Token cap for this run = min(per-run cap, what's left of today's budget)."""
        if self.model in {"none", "fake-1"}:
            return UsageMeter(max_tokens=self._per_run)
        remaining = max(0.0, self._daily - await self.spent_today())
        pin, pout = price_for(self.model)
        blended = (0.8 * pin + 0.2 * pout) / 1_000_000  # scoring is input-heavy
        affordable = int(remaining / blended) if blended > 0 else self._per_run
        return UsageMeter(max_tokens=min(self._per_run, affordable))

    def cost_of(self, usage: Usage) -> float:
        return estimate_cost(self.model, usage)
