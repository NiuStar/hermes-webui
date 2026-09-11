"""Retired connection-taking initializer; no runtime database access is allowed.

Empty database creation belongs to the separately gated offline bootstrap
lifecycle. This compatibility name grants no migration or activation ability.
"""


class LegacyInitializerDisabled(ValueError):
    code = 'LEGACY_INITIALIZER_DISABLED'


def initialize(db) -> None:
    """Reject before inspecting the supplied connection or executing SQLite."""
    raise LegacyInitializerDisabled(LegacyInitializerDisabled.code)
