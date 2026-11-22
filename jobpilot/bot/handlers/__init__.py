"""Routers in registration order. The fallback router must stay last."""

from __future__ import annotations

from aiogram import Router

from jobpilot.bot.handlers import fallback, help, jobs, menu, onboarding, questions, start


def all_routers() -> list[Router]:
    return [m.build_router() for m in (start, help, onboarding, questions, jobs, menu, fallback)]
