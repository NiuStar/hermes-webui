from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_official_agent_release_uses_supported_python_and_exact_commit():
    assert DOCKERFILE.startswith('FROM python:3.14-slim\n')
    assert 'ARG HERMES_AGENT_REVISION=818c13be1dc4fd28987e1e881a9408224afd4535' in DOCKERFILE
    assert 'ARG HERMES_AGENT_VERSION=v0.21.6' in DOCKERFILE
    assert 'org.opencontainers.image.hermes-agent.version="${HERMES_AGENT_VERSION}"' in DOCKERFILE


def test_docker_image_bakes_an_exact_hermes_agent_revision():
    assert "ARG HERMES_AGENT_REPOSITORY=https://github.com/NousResearch/hermes-agent.git" in DOCKERFILE
    assert "ARG HERMES_AGENT_REVISION=" in DOCKERFILE
    assert "git clone --filter=blob:none --no-checkout" in DOCKERFILE
    assert 'git checkout --detach "$HERMES_AGENT_REVISION"' in DOCKERFILE
    assert 'test "$(git rev-parse HEAD)" = "$HERMES_AGENT_REVISION"' in DOCKERFILE
    assert '/opt/hermes/.hermes-agent-revision' in DOCKERFILE
    assert 'rm -rf /opt/hermes/.git' in DOCKERFILE


def test_docker_image_exposes_baked_agent_identity_and_default_path():
    assert 'ENV HERMES_WEBUI_AGENT_DIR=/opt/hermes' in DOCKERFILE
    assert 'org.opencontainers.image.hermes-agent.revision="${HERMES_AGENT_REVISION}"' in DOCKERFILE
    assert 'org.opencontainers.image.hermes-agent.repository="${HERMES_AGENT_REPOSITORY}"' in DOCKERFILE
    assert 'org.opencontainers.image.hermes-agent.path="/opt/hermes"' in DOCKERFILE


def test_image_accepts_full_webui_commit_in_oci_label():
    assert 'ARG HERMES_WEBUI_REVISION=unknown' in DOCKERFILE
    assert 'org.opencontainers.image.revision="${HERMES_WEBUI_REVISION}"' in DOCKERFILE


def test_version_tag_cannot_publish_unverified_release_or_latest():
    import yaml
    workflow = yaml.safe_load((ROOT / '.github/workflows/release.yml').read_text(encoding='utf-8'))
    # YAML 1.1 treats `on` as a boolean; inspect the parsed trigger regardless.
    trigger = workflow.get('on', workflow.get(True))
    assert trigger == {'workflow_dispatch': None}
    assert workflow['permissions'] == {'contents': 'read'}
    assert not {'release', 'build', 'publish'} & set(workflow['jobs'])
    assert 'action-gh-release' not in str(workflow)
    assert 'build-push-action' not in str(workflow)
    assert not any('type=raw,value=latest' in str(job) or 'ghcr.io/' in str(job)
                   for job in workflow['jobs'].values())


def test_system_python_sidecar_has_manifest_verifier_dependency():
    compose = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
    assert '"/usr/local/bin/python", "/apptoo/api/docker_self_update.py"' in compose
    assert '/usr/local/bin/python -m pip install --no-cache-dir "cryptography>=42.0"' in DOCKERFILE


def test_docker_init_prioritizes_configured_baked_agent_source():
    init = (ROOT / "docker_init.bash").read_text(encoding="utf-8")
    paths = init[init.index("_agent_paths=("):init.index(")", init.index("_agent_paths=("))]
    assert '"${HERMES_WEBUI_AGENT_DIR:-}"' in paths
    assert paths.index('"${HERMES_WEBUI_AGENT_DIR:-}"') < paths.index('"/home/hermeswebui/.hermes/hermes-agent"')
