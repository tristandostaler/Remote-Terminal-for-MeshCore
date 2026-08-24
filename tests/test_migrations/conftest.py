"""Shared constants for the migration tests.

Migration tests that run ``run_migrations`` to completion assert
``get_version == LATEST_SCHEMA_VERSION`` and
``applied == LATEST_SCHEMA_VERSION - starting_version``, so only this one value
has to track the migration set rather than every individual assertion.

It is **derived from the migrations on disk** rather than hardcoded. It used to
be a literal with a comment claiming it was "updated automatically when a new
migration is added"; it was not, and it sat at 64 while migrations 065-069
landed, which left nine of these tests failing on main. Discovering it the same
way :func:`app.migrations.run_migrations` discovers migrations means adding a
migration can no longer break these tests.
"""

import pkgutil
import re

import app.migrations


def _latest_migration_number() -> int:
    """The highest ``_NNN_`` prefix in :mod:`app.migrations`.

    Mirrors the runner's own discovery in ``app/migrations/__init__.py``: same
    package, same regex. If the two ever disagree the tests are asserting
    against a schema version the runner will never reach.
    """
    numbers = [
        int(match.group(1))
        for module in pkgutil.iter_modules(app.migrations.__path__)
        if (match := re.match(r"_(\d+)_", module.name))
    ]
    if not numbers:  # pragma: no cover - the package always has migrations
        raise RuntimeError("no migrations found in app.migrations")
    return max(numbers)


LATEST_SCHEMA_VERSION = _latest_migration_number()
