from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


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


def test_docker_init_prioritizes_configured_baked_agent_source():
    init = (ROOT / "docker_init.bash").read_text(encoding="utf-8")
    paths = init[init.index("_agent_paths=("):init.index(")", init.index("_agent_paths=("))]
    assert '"${HERMES_WEBUI_AGENT_DIR:-}"' in paths
    assert paths.index('"${HERMES_WEBUI_AGENT_DIR:-}"') < paths.index('"/home/hermeswebui/.hermes/hermes-agent"')
