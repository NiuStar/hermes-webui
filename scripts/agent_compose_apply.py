"""Fail-closed Compose-owned Agent update transaction (candidate only).

The runtime adapter is responsible for Docker Compose CLI and image inspection.
Do not wire into the sidecar until the adapter and stateful rollback are accepted.
"""
from __future__ import annotations

from pathlib import Path
import re
try:
    from .agent_compose_transaction import (
        PinError, pin_lock, validate_compose_owner, prepare_pin,
        apply_pin, verify_pin, restore_pin,
    )
except ImportError:
    from agent_compose_transaction import (
        PinError, pin_lock, validate_compose_owner, prepare_pin,
        apply_pin, verify_pin, restore_pin,
    )


def apply_compose_agent_image(path: Path, files: list[Path], service: str,
                              old_ref: str, new_ref: str, *, repository: str,
                              expected_old_id: str, expected_new_image_id: str,
                              runtime) -> dict:
    """Recreate exactly one Compose service; rollback recreates the old image.

    A foreign owner or ambiguous Docker reply is never overwritten. The caller
    must first authenticate the new image's signed manifest and reject active
    runs; this candidate does not provide those prerequisites.
    """
    if not files or Path(path) != Path(files[-1]):
        raise PinError('Only the last ordered Compose file may be pinned')
    if not re.fullmatch(r'[0-9a-f]{64}', expected_old_id):
        raise PinError('Invalid old owner ID')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', expected_new_image_id):
        raise PinError('Invalid target image ID')
    with pin_lock(path):
        baseline = runtime.render(path)
        old = runtime.inspect()
        if old.get('Id') != expected_old_id:
            raise PinError('Compose owner changed before preflight')
        validate_compose_owner(old, runtime.image_info(old_ref), files, baseline,
                               service=service, repository=repository,
                               config_hash=runtime.hash())
        if runtime.image_info(new_ref).get('Id') != expected_new_image_id:
            raise PinError('Target image ID changed before transaction')
        siblings = runtime.siblings()
        plan = prepare_pin(path, service, old_ref, new_ref, baseline, runtime.render)
        if runtime.inspect().get('Id') != expected_old_id or runtime.siblings() != siblings:
            raise PinError('Compose ownership changed before pin')
        guard = getattr(runtime, 'guard_idle', None)
        if callable(guard):
            guard()
        apply_pin(plan, runtime.render)
        try:
            verify_pin(plan, runtime.render)
        except Exception as exc:
            if runtime.inspect().get('Id') != expected_old_id or runtime.siblings() != siblings:
                raise PinError('candidate pin ambiguous; manual recovery required') from exc
            restore_pin(plan, runtime.render)
            raise PinError('candidate pin verification failed; original Compose config restored') from exc
        try:
            if callable(guard):
                guard()
            runtime.up()
            candidate = runtime.inspect()
            if (candidate.get('Id') == expected_old_id
                    or candidate.get('Image') != expected_new_image_id
                    or not re.fullmatch(r'[0-9a-f]{64}', str(candidate.get('Id') or ''))
                    or runtime.siblings() != siblings):
                raise PinError('foreign or invalid Compose candidate owner')
            validate_compose_owner(candidate, runtime.image_info(new_ref), files,
                                   runtime.render(path), service=service,
                                   repository=repository, config_hash=runtime.hash())
            runtime.accept(candidate)
            final = runtime.inspect()
            if (final.get('Id') != candidate['Id']
                    or final.get('Image') != expected_new_image_id
                    or runtime.siblings() != siblings):
                raise PinError('foreign Compose owner after acceptance')
            validate_compose_owner(final, runtime.image_info(new_ref), files,
                                   runtime.render(path), service=service,
                                   repository=repository, config_hash=runtime.hash())
        except Exception as exc:
            observed = runtime.inspect()
            if runtime.siblings() != siblings:
                raise PinError('Sibling changed; manual recovery required') from exc
            if 'candidate' not in locals():
                if observed.get('Id') != expected_old_id:
                    raise PinError('foreign or ambiguous Compose owner; manual recovery required') from exc
                restore_pin(plan, runtime.render)
                return {'state': 'rolled_back', 'old_id': expected_old_id,
                        'restored_id': expected_old_id}
            if observed.get('Id') != candidate.get('Id') or observed.get('Id') == expected_old_id:
                raise PinError('foreign or ambiguous Compose owner; manual recovery required') from exc
            restore_pin(plan, runtime.render)
            runtime.up()
            restored = runtime.inspect()
            if (restored.get('Id') == expected_old_id
                    or restored.get('Image') != old.get('Image')
                    or not re.fullmatch(r'[0-9a-f]{64}', str(restored.get('Id') or ''))
                    or runtime.siblings() != siblings):
                raise PinError('Old Compose service did not restore') from exc
            validate_compose_owner(restored, runtime.image_info(old_ref), files,
                                   runtime.render(path), service=service,
                                   repository=repository, config_hash=runtime.hash())
            try:
                runtime.accept_restored(restored)
            except Exception as restore_error:
                raise PinError('restored Compose service did not become healthy') from restore_error
            return {'state': 'rolled_back', 'old_id': expected_old_id,
                    'restored_id': restored['Id']}
        return {'state': 'succeeded', 'old_id': expected_old_id,
                'new_id': candidate['Id']}
