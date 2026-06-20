"""Cross-platform advisory-lock primitives in equipa.initiative_runner.

The initiative concurrency guard is fail-CLOSED: an already-held lock must raise
OSError so the caller refuses to dispatch. These tests assert that contract on
whatever platform they run (fcntl on POSIX, msvcrt on Windows), guarding the
Windows-portability fix from regressing.
"""

import os

from equipa import initiative_runner as ir


def test_module_imports_on_this_platform():
    # Regression: a bare top-level `import fcntl` made this module fail to import
    # on Windows, breaking collection of every test that touched it.
    assert hasattr(ir, "_lock_exclusive_nonblocking")
    assert hasattr(ir, "_unlock")


def test_exclusive_lock_blocks_second_holder_then_releases(tmp_path):
    lock_path = tmp_path / "x.lock"
    fh1 = open(lock_path, "w")
    fh2 = open(lock_path, "w")
    try:
        ir._lock_exclusive_nonblocking(fh1)  # first holder wins

        # Second, non-blocking acquisition must fail closed (OSError).
        try:
            ir._lock_exclusive_nonblocking(fh2)
            raised = False
        except OSError:
            raised = True
        assert raised, "second exclusive lock must raise OSError (fail-closed)"

        # After release, the second holder can acquire.
        ir._unlock(fh1)
        ir._lock_exclusive_nonblocking(fh2)
        ir._unlock(fh2)
    finally:
        fh1.close()
        fh2.close()
        # sanity: file still exists and is removable
        assert os.path.exists(lock_path)
