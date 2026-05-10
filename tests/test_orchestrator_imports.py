"""Regression tests for db_conn / get_db_connection import resolvability.

The orchestrator's post-task verify_task_updated() and several dev-test loop
helpers call ``db_conn(write=True)`` and ``get_db_connection()`` directly. If
a module USES one of those helpers but never imports it, Python only catches
the bug at runtime — and only when that specific code path executes. The
result is a NameError that surfaces deep inside an orchestrator dispatch,
hours after a deploy.

Three prior incidents in the wild:
- 2026-05-06 GutenForge Stage D: tasks.py:274 missing get_db_connection import.
- 2026-05-10 ForgeArcade Wave 4: loops.py:415 missing db_conn import (this fix).
- 2026-05-10 ForgeArcade Wave 5: same loops.py:415 path re-fired.

These tests load each orchestrator module that uses db_conn / get_db_connection
and assert the names are resolvable in the module's namespace. They will fail
on import-time errors, missing imports, or accidental rename-without-update.

Copyright 2026 Forgeborn
"""

from __future__ import annotations


def test_loops_module_resolves_db_helpers() -> None:
    from equipa import loops

    # loops.py:415 uses ``with db_conn(write=True) as conn:``
    assert hasattr(loops, "db_conn"), "equipa.loops must import db_conn"
    # loops.py uses get_db_connection elsewhere (e.g. line 1147)
    assert hasattr(loops, "get_db_connection"), "equipa.loops must import get_db_connection"


def test_dispatch_module_resolves_db_helpers() -> None:
    from equipa import dispatch

    assert hasattr(dispatch, "get_db_connection"), "equipa.dispatch must import get_db_connection"
    # dispatch.py imports db_conn defensively for future ``with db_conn(...)`` callers.
    assert hasattr(dispatch, "db_conn"), "equipa.dispatch must import db_conn"


def test_tasks_module_resolves_db_helpers() -> None:
    from equipa import tasks

    # The original 2026-05-06 incident — keep guarded.
    assert hasattr(tasks, "db_conn"), "equipa.tasks must import db_conn"
    assert hasattr(tasks, "get_db_connection"), "equipa.tasks must import get_db_connection"


def test_sessions_module_resolves_db_helpers() -> None:
    from equipa import sessions

    # sessions.py uses db_conn / get_db_connection per dependency report.
    # Guard against future drift.
    assert hasattr(sessions, "db_conn") or hasattr(sessions, "get_db_connection"), (
        "equipa.sessions must import at least one of db_conn / get_db_connection"
    )


def test_lessons_module_resolves_db_helpers() -> None:
    from equipa import lessons

    assert hasattr(lessons, "get_db_connection"), "equipa.lessons must import get_db_connection"


def test_reflexion_module_resolves_db_helpers() -> None:
    from equipa import reflexion

    # reflexion uses db helpers per dependency report.
    assert hasattr(reflexion, "db_conn") or hasattr(reflexion, "get_db_connection"), (
        "equipa.reflexion must import at least one of db_conn / get_db_connection"
    )
