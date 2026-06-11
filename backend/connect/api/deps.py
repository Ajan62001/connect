"""Request-scoped access to the composition root."""

from __future__ import annotations

import sqlite3

from fastapi import Request

from connect.orchestration.container import Container


def get_container(request: Request) -> Container:
    return request.app.state.container


def get_db(request: Request) -> sqlite3.Connection:
    return request.app.state.container.db
