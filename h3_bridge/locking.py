from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO


_PUBLICATION_CONTROL_GUARD = "h3-publication-control.lock"


@contextmanager
def publication_control_guard(project_root: Path) -> Iterator[TextIO]:
    """Serialize generation-lock publication with control authorization.

    This descriptor is intentionally short-lived and must never be inherited
    by the H3 launcher. The separate generation lock remains the lifetime lock.
    """

    path = project_root.resolve() / "runtime" / _PUBLICATION_CONTROL_GUARD
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as guard:
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
        try:
            yield guard
        finally:
            fcntl.flock(guard.fileno(), fcntl.LOCK_UN)
