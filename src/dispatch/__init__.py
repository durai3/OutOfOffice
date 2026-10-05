"""Dispatch: launching one meeting worker per meeting, per user."""

from src.dispatch.base import (
    Dispatcher,
    DispatchError,
    DispatchHandle,
    DispatchMode,
    DispatchUnavailable,
    MeetingJob,
    configured_mode,
)
from src.dispatch.callback import CallbackEmitter, build_emitter, is_worker
from src.dispatch.container import ContainerDispatcher
from src.dispatch.inprocess import InProcessDispatcher

__all__ = [
    "CallbackEmitter",
    "ContainerDispatcher",
    "DispatchError",
    "DispatchHandle",
    "DispatchMode",
    "DispatchUnavailable",
    "Dispatcher",
    "InProcessDispatcher",
    "MeetingJob",
    "build_emitter",
    "configured_mode",
    "is_worker",
]
