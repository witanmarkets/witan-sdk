"""wtn serve: a node over a real local store, driven through real HTTP with the SDK."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import httpx
import pytest

from witan_sdk import Witan, WitanError

duckdb = pytest.importorskip("duckdb")

from witan_sdk.node import Server  # noqa: E402  (needs duckdb)

from test_bundle import ProjectFake  # noqa: E402

SLUG = "state-src"


def _part(path: Path, sql: str) -> tuple[str, int]:
    con = duckdb.connect()
    con.execute(f"COPY ({sql}) TO '{path}' (FORMAT PARQUET)")
    con.close()
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    final = path.with_name(f"{sha}.parquet")
    path.rename(final)
    return sha, final.stat().st_size


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """Two versions shaped like pull writes them: v1 = part A, v2 = parts A + B (allowExtra → _extra)."""
    root = tmp_path / "store"
    parts = root / SLUG / "parts"
    parts.mkdir(parents=True)
    sha_a, size_a = _part(parts / "a.parquet",
                          "SELECT * FROM (VALUES ('cursor', 41::BIGINT, 1.5::DOUBLE, true, NULL::VARCHAR),"
                          " ('retries', 2::BIGINT, NULL::DOUBLE, false, '{\"note\":\"slow\",\"tries\":3}'),"
                          " ('empty', NULL::BIGINT, 2.0::DOUBLE, NULL::BOOLEAN, NULL::VARCHAR)) AS t(key, n, v, ok, _extra)")
    sha_b, size_b = _part(parts / "b.parquet",
                          "SELECT * FROM (VALUES ('later', 7::BIGINT, 3.75::DOUBLE, false, '{\"region\":\"ap-northeast-2\"}'),"
                          " ('last', -5::BIGINT, -1.125::DOUBLE, NULL::BOOLEAN, NULL::VARCHAR)) AS t(key, n, v, ok, _extra)")
    a = {"sha256": sha_a, "bytes": size_a, "records": 3, "contributionId": "c1", "agentId": "ag", "mergedInVersion": 1}
    b = {"sha256": sha_b, "bytes": size_b, "records": 2, "contributionId": "c2", "agentId": "ag", "mergedInVersion": 2}
    schema = {"hash": "h", "fields": [{"name": "key", "type": "string"}, {"name": "n", "type": "integer"},
                                      {"name": "v", "type": "number"}, {"name": "ok", "type": "boolean", "required": False}],
              "allowExtra": True}
    for v, ps in ((1, [a]), (2, [a, b])):
        (root / SLUG / f"v{v}").mkdir()
        m = {"format": "parquet", "project": SLUG, "version": v, "parent": v - 1 or None, "createdAt": f"2026-09-2{v}T00:00:00Z",
             "schema": schema, "parts": ps, "totals": {"records": sum(p["records"] for p in ps), "bytes": sum(p["bytes"] for p in ps),
                                                      "parts": len(ps), "contributions": v},
             "count": sum(p["records"] for p in ps), "pulledAt": "x", "source": "http://origin.test"}
        (root / SLUG / f"v{v}" / "manifest.json").write_text(json.dumps(m))
    (root / SLUG / "project.json").write_text(json.dumps({
        "slug": SLUG, "title": "Function state", "readme": "State a function keeps.", "license": "cc-by-4.0",
        "access": "public", "visibility": "private", "schemaDef": {"fields": schema["fields"], "allowExtra": True}}))
    (root / "secret.csv").write_text("k,v\nhello,world\n")
    (root / SLUG / "secret.csv").write_text("k,v\nhello,world\n")
    return root


@pytest.fixture
def node(store: Path):
    srv = Server(store, port=0, quiet=True).start()
    yield srv
    srv.close()


@pytest.fixture
def client(node) -> Witan:
    return Witan("any-key", base_url=node.url)


def test_list_and_detail_have_the_origin_shapes(client: Witan) -> None:
    [p] = client.projects.list()
    assert p["slug"] == SLUG and p["latestVersion"] == 2 and p["records"] == 5 and p["visibility"] == "private"
    d = client.projects.get(SLUG)
    assert d["schemaDef"]["allowExtra"] is True and d["localVersions"] == [2, 1] and d["title"] == "Function state"
    assert d["versions"][0]["version"] == 2 and d["versions"][0]["manifest"]["format"] == "witan-dataset-manifest/1"
    assert all("url" not in part for part in d["versions"][0]["manifest"]["parts"])


def test_data_pages_across_parts_and_versions(client: Witan) -> None:
    page = client.projects.data(SLUG)
    assert page["version"] == 2 and page["count"] == 5
    assert page["records"][0] == {"key": "cursor", "n": 41, "v": 1.5, "ok": True}
    assert page["records"][1] == {"key": "retries", "n": 2, "ok": False, "note": "slow", "tries": 3}
    window = client.projects.data(SLUG, offset=2, limit=2)
    assert [r["key"] for r in window["records"]] == ["empty", "later"]  # crosses from part A into part B
    assert client.projects.data(SLUG, version=1)["count"] == 3
    with pytest.raises(WitanError) as ei:
        client.projects.data(SLUG, limit=1001)
    assert ei.value.status == 400
    with pytest.raises(WitanError) as ei:
        client.projects.data(SLUG, version=9)
    assert ei.value.status == 404


def test_query_and_its_sandbox(client: Witan, store: Path) -> None:
    r = client.projects.query_remote(SLUG, "SELECT count(*) AS c, sum(n) AS s FROM records")
    assert r["columns"] == ["c", "s"] and r["rows"] == [[5, 45]] and r["version"] == 2 and r["truncated"] is False
    assert client.projects.query_remote(SLUG, "SELECT sum(n) FROM records", version=1)["rows"] == [[43]]
    assert "key" in [row[0] for row in client.projects.query_remote(SLUG, "DESCRIBE records")["rows"]]
    t = client.projects.query_remote(SLUG, "SELECT key FROM records", limit=2)
    assert t["count"] == 2 and t["truncated"] is True
    blocked = [
        f"SELECT * FROM read_csv('{store / 'secret.csv'}')",
        f"SELECT * FROM read_csv('{store / SLUG / 'parts'}/../secret.csv')",
        f"COPY (SELECT 1) TO '{store / 'out.csv'}'",
        "SET enable_external_access = true",
        "SELECT 1; SELECT 2",
    ]
    for sql in blocked:
        with pytest.raises(WitanError) as ei:
            client.projects.query_remote(SLUG, sql)
        assert ei.value.status == 400, sql
    assert not (store / "out.csv").exists()


def test_sql_cannot_write_attach_or_reach_other_files(node, client: Witan, store: Path) -> None:
    # 0.27.2: the parts directory was allowed, and DuckDB lets a statement write where it may read —
    # COPY over a part made the project 404, ATTACH made files next to the parts
    parts = store / SLUG / "parts"
    before = {p.name: p.read_bytes() for p in parts.iterdir()}
    m = client.projects.manifest(SLUG)
    part = parts / f"{m['parts'][0]['sha256']}.parquet"
    (parts / "stray.csv").write_text("k\nv\n")
    writes = [
        f"COPY (SELECT 42 AS a) TO '{part}' (FORMAT parquet)",
        f"COPY (SELECT 42 AS a) TO '{part}' (FORMAT parquet, USE_TMP_FILE false)",
        f"COPY (SELECT 42 AS a) TO '{parts / 'new.parquet'}' (FORMAT parquet)",
        f"ATTACH '{parts / 'x.db'}'",
        "ATTACH ':memory:' AS scratch",
        f"EXPORT DATABASE '{parts}'",
        "INSTALL httpfs",
        "CREATE TABLE t AS SELECT 1",
        "PRAGMA enable_profiling",
        "CALL pragma_version()",
        f"EXPLAIN ANALYZE COPY (SELECT 1) TO '{parts / 'e.csv'}'",
    ]
    for sql in writes:
        with pytest.raises(WitanError) as ei:
            client.projects.query_remote(SLUG, sql)
        assert ei.value.status == 400 and "read-only" in str(ei.value), sql
    others = [  # the version's own part files and nothing else: not the directory, not a stray file in it
        "SELECT * FROM query('COPY (SELECT 42 AS a) TO ''" + str(part) + "'' (FORMAT parquet)')",  # DuckDB refuses it itself
        f"SELECT * FROM glob('{parts}/*')",
        f"SELECT * FROM read_csv('{parts / 'stray.csv'}')",
        f"SELECT * FROM read_parquet('{parts}/*.parquet', filename = true)",
    ]
    for sql in others:
        with pytest.raises(WitanError) as ei:
            client.projects.query_remote(SLUG, sql)
        assert ei.value.status == 400, sql
    (parts / "stray.csv").unlink()
    assert {p.name: p.read_bytes() for p in parts.iterdir()} == before  # nothing written, nothing replaced
    assert client.projects.query_remote(SLUG, "SELECT count(*) FROM records")["rows"] == [[5]]
    setting = client.projects.query_remote(SLUG, "SELECT current_setting('allowed_directories')::VARCHAR")["rows"][0][0]
    assert str(parts.resolve()) not in setting and parts.resolve().as_posix() not in setting
    # the same through MCP query_dataset, the tool an agent is told is read-only
    call = httpx.post(f"{node.url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "query_dataset", "arguments": {"slug": SLUG, "sql": f"COPY (SELECT 1) TO '{part}' (FORMAT parquet)"}}}).json()
    assert call["result"]["isError"] is True and "read-only" in call["result"]["content"][0]["text"]
    assert part.read_bytes() == before[part.name]


def test_every_kind_of_query_still_runs(client: Witan) -> None:
    for sql in ("DESCRIBE records", "SUMMARIZE records", "SHOW records", "FROM records", "VALUES (1), (2)",
                "WITH k AS (SELECT key FROM records) SELECT count(*) FROM k", "PRAGMA table_info('records')",
                "SELECT key FROM records UNION ALL SELECT 'x'", "TABLE records"):
        assert client.projects.query_remote(SLUG, sql)["count"] >= 1, sql
    # a semicolon inside a string is not a second statement (0.27.2 refused any ';')
    assert client.projects.query_remote(SLUG, "SELECT ';' AS s")["rows"] == [[";"]]
    with pytest.raises(WitanError) as ei:
        client.projects.query_remote(SLUG, "SELECT 1; COPY (SELECT 1) TO 'x.csv'")
    assert ei.value.status == 400 and "one statement" in str(ei.value)


def test_sql_errors_do_not_name_local_paths(client: Witan, store: Path) -> None:
    with pytest.raises(WitanError) as ei:
        client.projects.query_remote(SLUG, f"SELECT * FROM read_csv('{store.resolve() / 'secret.csv'}')")
    msg = str(ei.value)
    assert ei.value.status == 400 and "<store>" in msg, msg
    assert str(store.resolve()) not in msg and store.resolve().as_posix() not in msg
    with pytest.raises(WitanError) as ei:
        client.projects.query_remote(SLUG, f"SELECT * FROM read_csv('{Path.home() / 'nothing-here.csv'}')")
    assert str(Path.home()) not in str(ei.value) and "~" in str(ei.value)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM from outside is TerminateProcess on Windows")
def test_serve_stops_on_sigterm(store: Path) -> None:
    # as PID 1 in the container a SIGTERM with no handler is ignored: docker stop ended in SIGKILL (137)
    import signal
    import socket
    import subprocess

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("WITAN_")}
    proc = subprocess.Popen([sys.executable, "-m", "witan_sdk.cli", "serve", "--store", str(store), "--port", str(port),
                             "--quiet"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise AssertionError(proc.stderr.read().decode() if proc.poll() is not None else "node did not start")
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0  # the Ctrl-C path, not -15
        assert b"Traceback" not in proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_a_long_query_is_interrupted(store: Path) -> None:
    srv = Server(store, port=0, quiet=True, query_timeout=0.3).start()
    try:
        with pytest.raises(WitanError) as ei:
            Witan("k", base_url=srv.url).projects.query_remote(SLUG, "SELECT count(*) FROM range(200000000000)")
        assert ei.value.status == 408
    finally:
        srv.close()


def test_pull_from_the_node_then_query_the_copy_offline(client: Witan, tmp_path: Path) -> None:
    m = client.projects.pull(SLUG, tmp_path / "copy")
    assert m["version"] == 2 and m["downloaded"] == 2 and m["count"] == 5
    r = client.projects.query(SLUG, "SELECT count(*) FROM records", version=2, out_dir=tmp_path / "copy")
    assert r["rows"] == [[5]]


def test_export_streams_every_record(node) -> None:
    res = httpx.get(f"{node.url}/projects/{SLUG}/export", params={"version": 2})
    assert res.status_code == 200 and res.headers["content-type"] == "application/gzip"
    lines = gzip.decompress(res.content).decode().splitlines()
    assert len(lines) == 5 and json.loads(lines[3])["region"] == "ap-northeast-2"
    assert httpx.get(f"{node.url}/projects/{SLUG}/export", params={"version": 7}).status_code == 404


def test_read_only_and_unknown_routes(node) -> None:
    res = httpx.post(f"{node.url}/projects/{SLUG}/contribute", json={"records": [{"key": "x"}]})
    assert res.status_code == 405 and "copy of an origin project" in res.json()["error"]
    res = httpx.get(f"{node.url}/projects/{SLUG}/diff", params={"to": 2})
    assert res.status_code == 404 and "ask the origin" in res.json()["error"]
    assert httpx.get(f"{node.url}/projects/nope-nope").status_code == 404
    health = httpx.get(f"{node.url}/healthz").json()
    assert health["node"] is True and health["readOnly"] is False and health["projects"] == 1 and health["versions"] == 2
    assert health["localProjects"] == []


def test_token_guards_the_api_and_signs_part_urls(store: Path, tmp_path: Path) -> None:
    srv = Server(store, port=0, token="s3cret", quiet=True).start()
    try:
        assert httpx.get(f"{srv.url}/projects").status_code == 401
        assert httpx.get(f"{srv.url}/projects", headers={"authorization": "Bearer wrong"}).status_code == 401
        assert httpx.get(f"{srv.url}/healthz").json() == {"ok": True}  # liveness only, without the token
        assert httpx.get(f"{srv.url}/healthz", headers={"authorization": "Bearer s3cret"}).json()["auth"] == "token"
        w = Witan("s3cret", base_url=srv.url)
        assert w.projects.list()[0]["slug"] == SLUG
        m = w.projects.manifest(SLUG)
        url = m["parts"][0]["url"]
        assert "sig=" in url and "exp=" in url
        assert httpx.get(url).status_code == 200  # no Authorization header, like a presigned URL
        assert httpx.get(url.replace("sig=", "sig=0")).status_code == 403
        sha = m["parts"][0]["sha256"]
        past = int(time.time()) - 10
        stale = f"{srv.url}/parts/{SLUG}/{sha}.parquet?exp={past}&sig={srv.node.sign(SLUG, sha, past)}"
        assert httpx.get(stale).status_code == 403
        assert w.projects.pull(SLUG, tmp_path / "copy")["downloaded"] == 2
    finally:
        srv.close()


def test_other_addresses_need_a_token(store: Path) -> None:
    with pytest.raises(WitanError, match="token"):
        Server(store, host="0.0.0.0", port=0)
    with pytest.raises(WitanError, match="16 characters"):  # a token beyond this machine has to be a secret
        Server(store, host="0.0.0.0", port=0, token="abc")
    Server(store, host="0.0.0.0", port=0, token="t" * 16, quiet=True).close()  # constructs; closing an unstarted node returns
    Server(store, port=0, token="t", quiet=True).close()  # on loopback any token will do


def test_mcp_over_streamable_http(node) -> None:
    mcp = f"{node.url}/mcp"

    def rpc(body: dict) -> httpx.Response:
        return httpx.post(mcp, json=body, headers={"accept": "application/json, text/event-stream"})

    init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}).json()
    assert init["result"]["protocolVersion"] == "2025-06-18" and init["result"]["serverInfo"]["name"] == "witan-node"
    note = rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status_code == 202 and note.content == b""  # Streamable HTTP: 202 carries no body
    tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["list_datasets", "dataset_info", "read_dataset", "dataset_manifest", "query_dataset",
                                          "create_dataset", "contribute_records", "contribution_status"]
    # annotations as the origin gives them: reads are read-only and idempotent, writes neither; nothing is open-world
    for t in tools:
        a = t["annotations"]
        assert a["title"] == t["title"] and a["openWorldHint"] is False and a["destructiveHint"] is False, t["name"]
        assert a["readOnlyHint"] is a["idempotentHint"] is (t["name"] not in ("create_dataset", "contribute_records")), t["name"]
    instructions = init["result"]["instructions"]
    assert "contribute_records" in instructions and "read-only copies" not in instructions
    call = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "query_dataset", "arguments": {"slug": SLUG, "sql": "SELECT count(*) AS c FROM records"}}}).json()
    assert json.loads(call["result"]["content"][0]["text"])["rows"] == [[5]]
    page = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "read_dataset", "arguments": {"slug": SLUG, "limit": 2}}}).json()
    assert json.loads(page["result"]["content"][0]["text"])["count"] == 2
    bad = rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "dataset_info", "arguments": {"slug": "../x"}}}).json()
    assert bad["result"]["isError"] is True
    assert rpc({"jsonrpc": "2.0", "id": 6, "method": "resources/list"}).json()["error"]["code"] == -32601
    assert httpx.get(mcp).status_code == 405
    # an unknown tool and params that are not an object are invalid params, not a tool's isError answer
    unknown = rpc({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "search_knowledge", "arguments": {}}}).json()
    assert unknown["error"]["code"] == -32602 and "search_knowledge" in unknown["error"]["message"]
    assert rpc({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": ["query_dataset"]}).json()["error"]["code"] == -32602
    parse = httpx.post(mcp, content=b"{not json", headers={"content-type": "application/json"})
    assert parse.status_code == 400 and parse.json()["error"]["code"] == -32700 and parse.json()["id"] is None


def test_follow_pulls_the_latest_version_from_the_origin(tmp_path: Path) -> None:
    fake = ProjectFake()
    origin = Witan("km_test", base_url="http://api.test", transport=httpx.MockTransport(fake))
    srv = Server(tmp_path / "store", port=0, follow=["agent-api-observatory", "no-such-project"], interval=3600,
                 origin=origin, quiet=True)
    try:
        srv.follower.sync_once()  # type: ignore[union-attr]
        status = srv.node.follow
        assert status["agent-api-observatory"]["version"] == 110 and status["agent-api-observatory"]["error"] is None
        assert status["no-such-project"]["error"]  # recorded, and the others keep going
        assert (tmp_path / "store" / "agent-api-observatory" / "project.json").is_file()
        [p] = srv.node.list_projects()
        assert p["slug"] == "agent-api-observatory" and p["latestVersion"] == 110 and p["title"] == "Agent API observatory"
        manifests = sum(1 for c in fake.calls if c.url.path == "/projects/agent-api-observatory/manifest")
        srv.follower.sync_once()  # type: ignore[union-attr]
        srv.follower.sync_once()  # type: ignore[union-attr]
        # nothing new: each round reads the project list, not the manifest (which counts the version as egress)
        assert sum(1 for c in fake.calls if c.url.path == "/projects/agent-api-observatory/manifest") == manifests == 1
        assert status["agent-api-observatory"]["version"] == 110 and status["agent-api-observatory"]["error"] is None
    finally:
        srv.close()
    with pytest.raises(WitanError, match="origin"):
        Server(tmp_path / "store", port=0, follow=["agent-api-observatory"])


def test_web_pages_cannot_use_the_node_behind_its_users_back(node) -> None:
    port = node.httpd.server_address[1]
    projects = f"{node.url}/projects"
    # DNS rebinding: the page's own name, now resolving to 127.0.0.1
    for host in (f"evil.test:{port}", "evil.test", f"127.0.0.1:{port + 1}"):
        res = httpx.get(projects, headers={"host": host})
        assert res.status_code == 403 and "Host" in res.json()["error"], host
    assert httpx.get(f"{node.url}/healthz", headers={"host": f"evil.test:{port}"}).status_code == 403
    for host in (f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"):
        assert httpx.get(projects, headers={"host": host}).status_code == 200, host
    # a cross-origin page, even when the browser sends a simple request
    for origin in ("http://evil.test", "null", "https://127.0.0.1.evil.test"):
        assert httpx.get(projects, headers={"origin": origin}).status_code == 403, origin
    body = json.dumps({"sql": "SELECT 1"})
    res = httpx.post(f"{node.url}/projects/{SLUG}/query", content=body, headers={"content-type": "text/plain"})
    assert res.status_code == 415
    res = httpx.post(f"{node.url}/mcp", content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                     headers={"content-type": "application/x-www-form-urlencoded", "origin": "http://localhost:5173"})
    assert res.status_code == 415
    assert httpx.get(projects, headers={"origin": "http://localhost:5173"}).status_code == 200  # a local dev page


def test_a_token_lets_other_origins_in(store: Path) -> None:
    srv = Server(store, port=0, token="s3cret", quiet=True).start()
    try:
        page = {"origin": "https://dashboard.example"}
        assert httpx.get(f"{srv.url}/projects", headers=page).status_code == 403
        assert httpx.get(f"{srv.url}/projects", headers={**page, "authorization": "Bearer s3cret"}).status_code == 200
    finally:
        srv.close()


def test_a_negative_content_length_is_refused_not_waited_on(node) -> None:
    import socket

    host, port = node.httpd.server_address[:2]
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(f"POST /mcp HTTP/1.1\r\nHost: {host}:{port}\r\nContent-Type: application/json\r\n"
                     "Content-Length: -1\r\n\r\n".encode())
        sock.settimeout(5)
        head = sock.recv(4096).decode("latin-1")
    assert head.startswith("HTTP/1.0 400") or head.startswith("HTTP/1.1 400"), head[:80]


def test_a_node_without_a_token_takes_writes_with_no_key(node, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WITAN_API_KEY", raising=False)
    keyless = Witan(base_url=node.url)
    keyless.projects.create("my-runs", "My runs", "Timings of my own runs, kept on this node.",
                            {"fields": [{"name": "task", "type": "string", "required": True}], "allowExtra": False})
    r = keyless.projects.contribute("my-runs", [{"task": "build"}])
    assert r["status"] == "merged" and r["mergedVersion"] == 1


def test_a_token_node_says_how_to_authenticate_and_takes_any_host_with_it(store: Path) -> None:
    srv = Server(store, host="127.0.0.1", port=0, token="s3cret-s3cret-s3cret", quiet=True).start()
    try:
        res = httpx.get(f"{srv.url}/projects")
        assert res.status_code == 401 and res.headers["www-authenticate"] == 'Bearer realm="witan-node"'
        good = {"authorization": "bearer s3cret-s3cret-s3cret"}  # the scheme in any letter case
        assert httpx.get(f"{srv.url}/projects", headers=good).status_code == 200
        port = srv.httpd.server_address[1]
        # a name the node was not bound to: refused without the token (DNS rebinding), fine with it
        assert httpx.get(f"{srv.url}/projects", headers={"host": f"node.example:{port}"}).status_code == 403
        assert httpx.get(f"{srv.url}/projects", headers={**good, "host": f"node.example:{port}"}).status_code == 200
    finally:
        srv.close()


def test_part_urls_follow_a_tls_proxy(client: Witan, node) -> None:
    plain = httpx.get(f"{node.url}/projects/{SLUG}/manifest").json()
    assert all(p["url"].startswith("http://") for p in plain["parts"])
    proxied = httpx.get(f"{node.url}/projects/{SLUG}/manifest", headers={"x-forwarded-proto": "https"}).json()
    assert all(p["url"].startswith("https://") for p in proxied["parts"])


def test_health_names_the_follows_that_fail(node) -> None:
    node.node.follow["gone-away"] = {"version": None, "at": "2026-10-08T00:00:00+00:00", "error": "project not found (HTTP 404)"}
    node.node.follow["fine-one"] = {"version": 3, "at": "2026-10-08T00:00:00+00:00", "error": None}
    health = httpx.get(f"{node.url}/healthz").json()
    assert health["ok"] is True and health["followFailing"] == ["gone-away"]
