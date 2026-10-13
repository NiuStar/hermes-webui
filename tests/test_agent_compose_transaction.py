"""An Agent-only image pin changes exactly one Compose service image."""
import copy
import hashlib
from pathlib import Path

import pytest
import yaml

from scripts import agent_compose_transaction as tx

OLD = '24802117/hermes-webui@sha256:' + 'a' * 64
NEW = '24802117/hermes-webui@sha256:' + 'b' * 64


def fixture(tmp_path):
    p = tmp_path / 'compose.yaml'
    p.write_text('services:\n  hermes-webui:\n    image: ' + OLD + '\n    environment:\n      FLAG: yes\n  updater:\n    image: ' + OLD + '\n')
    return p


def resolved(path):
    import yaml
    return yaml.safe_load(path.read_text())


def test_forward_and_reverse_only_modify_service_image(tmp_path):
    p = fixture(tmp_path)
    original = p.read_bytes()
    before = resolved(p)
    forward = tx.prepare_pin(p, 'hermes-webui', OLD, NEW, before, resolved)
    assert forward.previous_sha256 == hashlib.sha256(original).hexdigest()
    assert p.read_bytes() == original  # preflight is read-only
    tx.apply_pin(forward, resolved)
    expected = copy.deepcopy(before)
    expected['services']['hermes-webui']['image'] = NEW
    assert resolved(p) == expected
    assert tx.verify_pin(forward, resolved)
    reverse = tx.prepare_pin(p, 'hermes-webui', NEW, OLD, expected, resolved)
    tx.apply_pin(reverse, resolved)
    assert p.read_bytes() == original


def test_refuses_changed_source_before_write(tmp_path):
    p = fixture(tmp_path)
    plan = tx.prepare_pin(p, 'hermes-webui', OLD, NEW, resolved(p), resolved)
    p.write_text(p.read_text() + '\n# changed\n')
    with pytest.raises(tx.PinError, match='changed'):
        tx.apply_pin(plan, resolved)
    assert p.read_text().endswith('# changed\n')


def test_refuses_partial_or_ambiguous_image(tmp_path):
    p = fixture(tmp_path)
    p.write_text(p.read_text().replace('image: ' + OLD, 'image: ${IMAGE}', 1))
    with pytest.raises(tx.PinError):
        tx.prepare_pin(p, 'hermes-webui', OLD, NEW, resolved(p), resolved)
    p = fixture(tmp_path)
    p.write_text(p.read_text().replace('    environment:', '    image: ' + OLD + '\n    environment:'))
    with pytest.raises(tx.PinError):
        tx.prepare_pin(p, 'hermes-webui', OLD, NEW, resolved(p), resolved)


def test_refuses_other_service_or_config_drift(tmp_path):
    p = fixture(tmp_path)
    before = resolved(p)
    def drift(path):
        cfg = resolved(path)
        if cfg['services']['hermes-webui']['image'] == NEW:
            cfg['services']['updater']['image'] = NEW
        return cfg
    with pytest.raises(tx.PinError, match='other'):
        tx.prepare_pin(p, 'hermes-webui', OLD, NEW, before, drift)
    assert resolved(p) == before


def test_refuses_symlink_and_untrusted_digest(tmp_path):
    p = fixture(tmp_path)
    link = tmp_path / 'link.yaml'
    link.symlink_to(p)
    with pytest.raises(tx.PinError):
        tx.prepare_pin(link, 'hermes-webui', OLD, NEW, resolved(p), resolved)
    with pytest.raises(tx.PinError):
        tx.prepare_pin(p, 'hermes-webui', OLD, 'repo:latest', resolved(p), resolved)


def test_refuses_nested_fake_service_and_mismatched_quotes(tmp_path):
    p = fixture(tmp_path)
    p.write_text(p.read_text().replace('  updater:', '    labels:\n      hermes-webui:\n        image: ' + OLD + '\n  updater:')
                 .replace('  hermes-webui:\n    image:', '  other:\n    image:', 1))
    with pytest.raises(tx.PinError):
        tx.prepare_pin(p, 'hermes-webui', OLD, NEW, resolved(p), resolved)
    p = fixture(tmp_path)
    p.write_text(p.read_text().replace('image: ' + OLD, 'image: "' + OLD + "'", 1))
    with pytest.raises(yaml.YAMLError):
        resolved(p)


def test_preserves_quoted_image_and_wont_leave_temp_on_failure(tmp_path):
    p = fixture(tmp_path)
    p.write_text(p.read_text().replace('image: ' + OLD, 'image: "' + OLD + '"', 1))
    plan = tx.prepare_pin(p, 'hermes-webui', OLD, NEW, resolved(p), resolved)
    tx.apply_pin(plan, resolved)
    assert resolved(p)['services']['hermes-webui']['image'] == NEW
    assert 'image: "' + NEW + '"' in p.read_text()


def test_refuses_changed_effective_stack_before_apply(tmp_path):
    p = fixture(tmp_path)
    before = resolved(p)
    plan = tx.prepare_pin(p, 'hermes-webui', OLD, NEW, before, resolved)
    # A later Compose file or .env can drift even when this source stays equal.
    with pytest.raises(tx.PinError, match='changed'):
        tx.apply_pin(plan, render=lambda _: {'services': {}})
    assert p.read_bytes() == plan.previous


def test_refuses_unsafe_source_image_comment(tmp_path):
    p = fixture(tmp_path)
    p.write_text(p.read_text().replace('image: ' + OLD,
                                       'image: ' + OLD + ' # operator note', 1))
    with pytest.raises(tx.PinError):
        tx.prepare_pin(p, 'hermes-webui', OLD, NEW, resolved(p), resolved)


def test_restoration_requires_exact_resulting_bytes(tmp_path):
    p = fixture(tmp_path)
    plan = tx.prepare_pin(p, 'hermes-webui', OLD, NEW, resolved(p), resolved)
    tx.apply_pin(plan, resolved)
    p.write_bytes(p.read_bytes() + b'\n# other operator\n')
    with pytest.raises(tx.PinError, match='changed'):
        tx.restore_pin(plan, resolved)
    assert p.read_bytes().endswith(b'# other operator\n')


def test_restore_exact_pin_recovers_original_bytes(tmp_path):
    p = fixture(tmp_path)
    plan = tx.prepare_pin(p, 'hermes-webui', OLD, NEW, resolved(p), resolved)
    tx.apply_pin(plan, resolved)
    assert tx.restore_pin(plan, resolved)
    assert p.read_bytes() == plan.previous
    assert resolved(p) == plan.baseline


def test_ordered_override_winning_image_refuses_base_edit(tmp_path):
    p = fixture(tmp_path)
    overlay = tmp_path / 'override.yaml'
    overlay.write_text('services:\n  hermes-webui:\n    image: ' + OLD + '\n')
    def render(path):
        base = resolved(path)
        base['services']['hermes-webui']['image'] = resolved(overlay)['services']['hermes-webui']['image']
        return base
    with pytest.raises(tx.PinError, match='other'):
        tx.prepare_pin(p, 'hermes-webui', OLD, NEW, render(p), render)
    assert p.read_text().startswith('services:')


def test_competing_transaction_cannot_take_the_same_compose_pin(tmp_path):
    p = fixture(tmp_path)
    with tx.pin_lock(p):
        with pytest.raises(tx.PinError, match='locked'):
            with tx.pin_lock(p):
                pytest.fail('competing updater entered')
    with tx.pin_lock(p):
        assert p.exists()


def test_old_tag_to_observed_digest_requires_exact_live_image(tmp_path):
    p = fixture(tmp_path)
    tag = '24802117/hermes-webui:v2026.10.10-r1'
    p.write_text(p.read_text().replace(OLD, tag, 1))
    before = resolved(p)
    image = {'Id': 'sha256:' + 'a' * 64,
             'RepoDigests': [OLD]}
    live = {'Image': image['Id'], 'State': {'Running': True}}
    plan = tx.prepare_baseline_pin(p, 'hermes-webui', tag, OLD,
                                   before, resolved, live=live, image=image)
    assert p.read_text().splitlines()[2].strip() == 'image: ' + tag
    tx.apply_pin(plan, resolved)
    assert resolved(p)['services']['hermes-webui']['image'] == OLD


def test_old_tag_pin_refuses_mismatched_or_unverified_image(tmp_path):
    p = fixture(tmp_path)
    tag = '24802117/hermes-webui:v2026.10.10-r1'
    p.write_text(p.read_text().replace(OLD, tag, 1))
    before = resolved(p)
    live = {'Image': 'sha256:' + 'a' * 64, 'State': {'Running': True}}
    image = {'Id': 'sha256:' + 'b' * 64, 'RepoDigests': [OLD]}
    with pytest.raises(tx.PinError):
        tx.prepare_baseline_pin(p, 'hermes-webui', tag, OLD,
                                before, resolved, live=live, image=image)
    image['Id'] = live['Image']
    image['RepoDigests'] = []
    with pytest.raises(tx.PinError):
        tx.prepare_baseline_pin(p, 'hermes-webui', tag, OLD,
                                before, resolved, live=live, image=image)


def test_compose_context_requires_matching_owner_files_and_exact_old_digest(tmp_path):
    p = fixture(tmp_path)
    owner = {'Id': 'f' * 64, 'Image': 'sha256:' + 'a' * 64,
             'Config': {'Labels': {'com.docker.compose.project': 'pin-qa',
                 'com.docker.compose.service': 'hermes-webui',
                 'com.docker.compose.project.working_dir': str(tmp_path),
                 'com.docker.compose.project.config_files': str(p)}},
             'State': {'Running': True}}
    image = {'Id': owner['Image'], 'RepoDigests': [OLD]}
    result = tx.validate_compose_owner(owner, image, [p], resolved(p),
                                       service='hermes-webui', repository='24802117/hermes-webui')
    assert result == OLD
    owner['Config']['Labels']['com.docker.compose.project.config_files'] = str(tmp_path / 'other.yaml')
    with pytest.raises(tx.PinError):
        tx.validate_compose_owner(owner, image, [p], resolved(p),
                                  service='hermes-webui', repository='24802117/hermes-webui')


def test_compose_context_rejects_wrong_running_image_even_if_compose_pinned(tmp_path):
    p = fixture(tmp_path)
    owner = {'Id': 'f' * 64, 'Image': 'sha256:' + 'a' * 64,
             'Config': {'Labels': {'com.docker.compose.project': 'pin-qa',
                 'com.docker.compose.service': 'hermes-webui',
                 'com.docker.compose.project.working_dir': str(tmp_path),
                 'com.docker.compose.project.config_files': str(p)}},
             'State': {'Running': True}}
    image = {'Id': 'sha256:' + 'b' * 64, 'RepoDigests': [OLD]}
    with pytest.raises(tx.PinError):
        tx.validate_compose_owner(owner, image, [p], resolved(p),
                                  service='hermes-webui', repository='24802117/hermes-webui')


def test_compose_context_rejects_nonimmutable_image_id(tmp_path):
    p = fixture(tmp_path)
    owner = {'Id': 'f' * 64, 'Image': 'not-immutable',
             'Config': {'Labels': {'com.docker.compose.project': 'pin-qa',
                 'com.docker.compose.service': 'hermes-webui',
                 'com.docker.compose.project.working_dir': str(tmp_path),
                 'com.docker.compose.project.config_files': str(p)}},
             'State': {'Running': True}}
    image = {'Id': owner['Image'], 'RepoDigests': [OLD]}
    with pytest.raises(tx.PinError):
        tx.validate_compose_owner(owner, image, [p], resolved(p),
                                  service='hermes-webui', repository='24802117/hermes-webui')
