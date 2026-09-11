"""Strict admitted-growth gate used by every V4 lifecycle writer."""
from api.display_bootstrap_policy import BootstrapRejected


def before_growth(owner):
    if owner.resource['format_version'] != 4:
        return  # Historical test/read compatibility, not V4 admission.
    from api.display_bootstrap_growth_manager import GrowthManager
    manager = getattr(owner, 'growth_manager', None)
    if type(manager) is not GrowthManager:
        raise BootstrapRejected('ACCESS_BOUNDARY_UNPROVEN')
    if (manager.candidate_id != owner.candidate_id
            or manager.resource != owner.resource):
        raise BootstrapRejected('IDENTITY_CHANGED')
    manager.before_growth()
