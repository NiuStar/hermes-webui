"""Sprint 3 tests: cron API, skills API, memory API, input validation."""
import json, pathlib, shutil, tempfile, urllib.request, urllib.error

import pytest

from tests._pytest_port import BASE


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read()), r.status

def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def make_outside_trusted_dir(prefix):
    home_root = pathlib.Path.home().resolve()
    temp_root = pathlib.Path(tempfile.gettempdir()).resolve()
    base_root = temp_root if temp_root != home_root and home_root not in (temp_root, *temp_root.parents) else REPO_ROOT
    outside_root = base_root / ".tmp-outside-trusted"
    outside_root.mkdir(exist_ok=True)
    return pathlib.Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=outside_root))

def make_session_tracked(created_list, ws=None):
    """Create a session and register it with the cleanup fixture."""
    import pathlib as _pathlib
    body = {}
    if ws: body["workspace"] = str(ws)
    d, _ = post("/api/session/new", body)
    sid = d["session"]["session_id"]
    created_list.append(sid)
    return sid, _pathlib.Path(d["session"]["workspace"])


def test_crons_list():
    data, status = get("/api/crons")
    assert status == 200
    assert "jobs" in data

def test_crons_list_has_required_fields():
    data, _ = get("/api/crons")
    if not data["jobs"]: return
    job = data["jobs"][0]
    for field in ("id", "name", "prompt", "enabled", "schedule_display"):
        assert field in job

def test_crons_output_requires_job_id():
    try:
        get("/api/crons/output")
        assert False
    except urllib.error.HTTPError as e:
        assert e.code == 400

def test_crons_output_real_job():
    data, _ = get("/api/crons")
    if not data["jobs"]: return
    job_id = data["jobs"][0]["id"]
    out, status = get(f"/api/crons/output?job_id={job_id}&limit=3")
    assert status == 200
    assert "outputs" in out

def test_crons_pause_requires_job_id():
    result, status = post("/api/crons/pause", {})
    assert status in (400, 404)

def test_crons_resume_requires_job_id():
    result, status = post("/api/crons/resume", {})
    assert status in (400, 404)

def test_crons_run_nonexistent():
    result, status = post("/api/crons/run", {"job_id": "doesnotexist999"})
    assert status == 404

@pytest.fixture
def owned_skills(test_server):
    """Own a fresh profile, never write through conftest's real-skills symlink.

    The server pins HERMES_BASE_HOME to TEST_STATE_DIR. Request cookies select
    this profile without changing the server default or any client's profile.
    """
    from tests.conftest import TEST_STATE_DIR

    profiles = TEST_STATE_DIR / "profiles"
    assert not profiles.is_symlink(), "refusing a redirected test profiles root"
    profiles.mkdir(exist_ok=True)
    assert profiles.resolve().parent == TEST_STATE_DIR.resolve()
    with tempfile.TemporaryDirectory(prefix="sprint3-skills-", dir=profiles) as home:
        profile = pathlib.Path(home).name
        expected = {
            "sprint3-owned-alpha": ("sprint3-selected", "Deterministic alpha lookup."),
            "sprint3-owned-beta": ("sprint3-other", "Deterministic beta lookup."),
        }
        contents = {}
        for name, (category, description) in expected.items():
            content = f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nOwned fixture body for {name}.\n"
            target = pathlib.Path(home) / "skills" / category / name / "SKILL.md"
            target.parent.mkdir(parents=True)
            target.write_text(content, encoding="utf-8")
            contents[name] = content

        def request(path):
            req = urllib.request.Request(
                BASE + path, headers={"Cookie": f"hermes_profile={profile}"},
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                return json.loads(response.read()), response.status

        yield request, expected, contents


def test_skills_list(owned_skills):
    request, expected, _ = owned_skills
    data, status = request("/api/skills")
    assert status == 200
    assert {s["name"] for s in data["skills"]} == set(expected)
    assert len(data["skills"]) == len(expected)


def test_skills_list_has_required_fields(owned_skills):
    request, expected, _ = owned_skills
    data, status = request("/api/skills")
    assert status == 200
    assert {s["name"] for s in data["skills"]} == set(expected)
    for skill in data["skills"]:
        category, description = expected[skill["name"]]
        assert skill["description"] == description
        assert skill["category"] == category
        assert skill["disabled"] is False


def test_skills_content_known(owned_skills):
    request, _, contents = owned_skills
    for name, content in contents.items():
        data, status = request(f"/api/skills/content?name={name}")
        assert status == 200
        assert "error" not in data
        assert data["content"] == content


def test_skills_content_requires_name():
    try:
        get("/api/skills/content")
        assert False
    except urllib.error.HTTPError as e:
        assert e.code == 400


def test_skills_search_returns_subset(owned_skills):
    """Exercise the API's category filter; text search is client-side."""
    request, expected, _ = owned_skills
    all_data, status = request("/api/skills")
    assert status == 200
    all_names = {s["name"] for s in all_data["skills"]}
    assert all_names == set(expected)
    data, status = request("/api/skills?category=sprint3-selected")
    assert status == 200
    names = {s["name"] for s in data["skills"]}
    assert names == {"sprint3-owned-alpha"}
    assert names < all_names
    missing, status = request("/api/skills?category=sprint3-no-match")
    assert status == 200
    assert missing["skills"] == []

def test_memory_returns_both_files():
    data, status = get("/api/memory")
    assert status == 200
    assert "memory" in data and "user" in data

def test_memory_content_is_string():
    data, _ = get("/api/memory")
    assert isinstance(data["memory"], str)
    assert isinstance(data["user"], str)

def test_memory_has_mtime():
    data, _ = get("/api/memory")
    assert "memory_mtime" in data and "user_mtime" in data

def test_session_update_requires_session_id():
    result, status = post("/api/session/update", {"model": "openai/gpt-5.4-mini"})
    assert status == 400

def test_session_delete_requires_session_id():
    result, status = post("/api/session/delete", {})
    assert status == 400


def test_session_delete_rejects_absolute_path_payload(tmp_path):
    victim = tmp_path / "victim.json"
    victim.write_text("TOPSECRET", encoding="utf-8")
    result, status = post("/api/session/delete", {"session_id": str(victim.with_suffix(""))})
    assert status == 400
    assert victim.exists(), "absolute-path payload must not delete arbitrary files"


def test_session_delete_rejects_traversal_payload(tmp_path):
    victim = tmp_path / "outside.json"
    victim.write_text("TOPSECRET", encoding="utf-8")
    traversal = f"../../../../{victim.with_suffix('').as_posix().lstrip('/')}"
    result, status = post("/api/session/delete", {"session_id": traversal})
    assert status == 400
    assert victim.exists(), "traversal payload must not delete arbitrary files"


def test_chat_start_requires_session_id():
    result, status = post("/api/chat/start", {"message": "hello"})
    assert status == 400

def test_chat_start_requires_message(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    result, status = post("/api/chat/start", {"session_id": sid, "message": ""})
    assert status == 400

def test_session_update_unknown_id_returns_404():
    result, status = post("/api/session/update", {"session_id": "nosuchsession", "model": "openai/gpt-5.4-mini"})
    assert status == 404


def test_session_update_rejects_workspace_outside_trusted_root(tmp_path):
    d, _ = post("/api/session/new", {})
    sid = d["session"]["session_id"]
    outside = make_outside_trusted_dir("outside")
    try:
        result, status = post("/api/session/update", {"session_id": sid, "workspace": str(outside)})
        assert status == 400
        assert "outside" in result.get("error", "").lower()
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_chat_start_rejects_workspace_outside_trusted_root(tmp_path):
    d, _ = post("/api/session/new", {})
    sid = d["session"]["session_id"]
    outside = make_outside_trusted_dir("outside-chat")
    try:
        result, status = post("/api/chat/start", {"session_id": sid, "message": "hello", "workspace": str(outside)})
        assert status == 400
        assert "outside" in result.get("error", "").lower()
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_workspace_add_allows_external_valid_paths(tmp_path):
    """Adding a path outside home is now allowed when the user explicitly provides it.
    The strict trust check (resolve_trusted_workspace) is only applied when *using*
    an existing workspace, not when registering a new one (validate_workspace_to_add)."""
    outside = tmp_path / "outside-add"
    outside.mkdir(parents=True, exist_ok=True)
    result, status = post("/api/workspaces/add", {"path": str(outside), "name": "Outside"})
    # Explicit registration of an external path is now allowed
    assert status == 200, f"Expected 200, got {status}: {result}"
    # Verify it was actually saved
    wss_result, ws_status = get("/api/workspaces")
    paths = [w["path"] for w in wss_result.get("workspaces", [])]
    assert str(outside.resolve()) in paths


def test_workspace_add_rejects_system_paths():
    """System paths (/, /etc, /sys) are always rejected even with the relaxed add validation."""
    for path in ("/etc", "/private/etc"):
        _, status = post("/api/workspaces/add", {"path": path, "name": "System"})
        assert status == 400, f"{path} should be rejected"


def test_legacy_chat_rejects_workspace_outside_trusted_root(tmp_path):
    """Legacy /api/chat must use the same trusted workspace validation as /api/chat/start."""
    d, _ = post("/api/session/new", {})
    sid = d["session"]["session_id"]
    outside = make_outside_trusted_dir("outside-legacy-chat")
    try:
        result, status = post("/api/chat", {"session_id": sid, "message": "hello", "workspace": str(outside)})
        assert status == 400
        assert "outside" in result.get("error", "").lower()
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_session_new_rejects_workspace_outside_trusted_root(tmp_path):
    outside = make_outside_trusted_dir("outside-new")
    try:
        result, status = post("/api/session/new", {"workspace": str(outside)})
        assert status == 400
        assert "outside" in result.get("error", "").lower()
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_session_search_returns_matches(cleanup_test_sessions):
    sid, _ = make_session_tracked(cleanup_test_sessions)
    post("/api/session/rename", {"session_id": sid, "title": f"unique-s3-{sid}"})
    data, status = get(f"/api/sessions/search?q=unique-s3-{sid}")
    assert status == 200
    sids = [s["session_id"] for s in data["sessions"]]
    assert sid in sids

def test_session_search_empty_query_returns_all():
    data, status = get("/api/sessions/search?q=")
    assert status == 200 and "sessions" in data

def test_session_search_no_results():
    data, status = get("/api/sessions/search?q=zzznomatchzzz9999")
    assert status == 200 and data["sessions"] == []
