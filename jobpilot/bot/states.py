"""Conversation states (persisted in SQL via SQLStorage, so restarts resume mid-flow)."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class Flow(StatesGroup):
    waiting_resume = State()
    collecting_paste = State()
    confirming_profile = State()
    fixing_profile = State()
    answering = State()
    reviewing_plan = State()
    confirming_new = State()
    confirming_delete = State()


INTAKE_STATES = (Flow.waiting_resume, Flow.collecting_paste)
