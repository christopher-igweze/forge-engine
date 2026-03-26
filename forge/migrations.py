"""Config schema migrations for vibe2prod.

Each migration function transforms config from one schema version to the next.
Named: migrate_X_to_Y where X and Y are schema version numbers.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Registry of migrations: (from_version, to_version) -> function
_MIGRATIONS: dict[tuple[int, int], callable] = {}


def _register(from_v: int, to_v: int):
    """Decorator to register a migration function."""
    def decorator(func):
        _MIGRATIONS[(from_v, to_v)] = func
        return func
    return decorator


# Example migration — uncomment when schema v2 is defined:
# @_register(1, 2)
# def migrate_1_to_2(config: dict) -> dict:
#     """Add new sections with defaults."""
#     config.setdefault("some_new_field", "default_value")
#     config["config_schema_version"] = 2
#     return config


def run_migrations(config: dict, from_version: int, to_version: int) -> dict:
    """Run all migrations from from_version to to_version sequentially."""
    current = from_version
    while current < to_version:
        next_v = current + 1
        migration = _MIGRATIONS.get((current, next_v))
        if migration is None:
            logger.warning("No migration found for %d → %d, skipping", current, next_v)
            current = next_v
            continue
        logger.info("Running migration %d → %d", current, next_v)
        config = migration(config)
        current = next_v
    return config
