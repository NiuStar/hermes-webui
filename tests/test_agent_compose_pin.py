from pathlib import Path

import pytest

from scripts.check_agent_compose_pin import PinError, verify_pin


@pytest.fixture
def scenario(tmp_path):
    source = str(tmp_path / 'docker-compose.yml')
    Path(source).write_text('services: {}\n')
    accepted = 'example/webui@sha256:' + 'a' * 64
    image = {'Id': 'sha256:' + 'b' * 64, 'RepoDigests': [accepted]}
    live = {'Id': 'c' * 64, 'Image': image['Id'],
            'State': {'Running': True, 'Health': {'Status': 'healthy'}},
            'Config': {'Labels': {'com.docker.compose.service': 'webui',
                                  'com.docker.compose.project.config_files': source}}}
    compose = {'services': {'webui': {'image': accepted}}}
    return live, image, compose, accepted, source


def test_exact_live_digest_survives_compose_recreate(scenario):
    live, image, compose, accepted, source = scenario
    result = verify_pin(live, image, compose, accepted=accepted,
                        service='webui', files=[source])
    assert result['container_id'] == live['Id']


@pytest.mark.parametrize('part', ['floating', 'different_digest', 'different_image',
                                  'different_stack', 'unhealthy', 'foreign_service'])
def test_drift_fails_closed(scenario, part):
    live, image, compose, accepted, source = scenario
    if part == 'floating':
        compose['services']['webui']['image'] = 'example/webui:latest'
    elif part == 'different_digest':
        image['RepoDigests'] = ['example/webui@sha256:' + 'd' * 64]
    elif part == 'different_image':
        live['Image'] = 'sha256:' + 'e' * 64
    elif part == 'different_stack':
        live['Config']['Labels']['com.docker.compose.project.config_files'] = source + ',/tmp/override.yml'
    elif part == 'unhealthy':
        live['State']['Health']['Status'] = 'starting'
    elif part == 'foreign_service':
        live['Config']['Labels']['com.docker.compose.service'] = 'api'
    with pytest.raises(PinError):
        verify_pin(live, image, compose, accepted=accepted,
                   service='webui', files=[source])
