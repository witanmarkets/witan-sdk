"""A node's store over many versions: levelled folding, the version cache, --keep-versions and its
garbage collection, and the MCP tool annotations."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from witan_sdk import Witan, WitanError

duckdb = pytest.importorskip("duckdb")

from witan_sdk import node_write  # noqa: E402
from witan_sdk.cli import main  # noqa: E402
from witan_sdk.node import Node, NodeError, Server, Store  # noqa: E402

from test_bundle import ProjectFake  # noqa: E402
from test_client import PART_A, SHA_A, SHA_B  # noqa: E402

SLUG = "runs-log"
SCHEMA = {"fields": [{"name": "key", "type": "string"}, {"name": "n", "type": "integer"}]}
README = "One record per step of an agent run, written as the run goes."


def new_node(root: Path, **kw) -> Node:
    node = Node(Store(root), **kw)
    node.create_project({"slug": SLUG, "title": "Runs log", "readme": README, "schemaDef": SCHEMA})
    return node


def batch(c: int, size: int = 5) -> dict:
    return {"records": [{"key": f"r{c}-{j}", "n": c * size + j} for j in range(size)]}


def version_dirs(root: Path, slug: str = SLUG) -> list[int]:
    return sorted(int(p.name[1:]) for p in (root / slug).iterdir() if p.name.startswith("v") and p.name[1:].isdigit())


def part_files(root: Path, slug: str = SLUG) -> set[str]:
    return {p.name for p in (root / slug / "parts").iterdir()}


def test_small_batches_fold_by_levels_not_into_one_growing_tail(tmp_path: Path) -> None:
    node = new_node(tmp_path / "store")
    written = 0
    for c in range(64):
        _, view, _ = node.contribute(SLUG, batch(c), None)
        assert view["status"] == "merged" and view["mergedVersion"] == c + 1
        m = node.store.manifest(SLUG)
        written += m["parts"][-1]["records"]  # the one part this merge wrote
        assert len(m["parts"]) <= 7, [p["records"] for p in m["parts"]]
        for p in m["parts"]:  # the sources still tile each part, in order
            offset = 0
            for s in p["sources"]:
                assert s["offset"] == offset
                offset += s["records"]
            assert offset == p["records"]
    # folding everything into the tail rewrote 5 + 10 + ... + 320 = 10,400 records (32.5x); levels stay a few x
    assert written <= 6 * 320, written
    m = node.store.manifest(SLUG)
    assert [s["mergedInVersion"] for p in m["parts"] for s in p["sources"]] == list(range(1, 65))
    assert [r["n"] for r in node.data(SLUG, None, 1000, 0)["records"]] == list(range(320))
    assert [r["n"] for r in node.data(SLUG, 10, 1000, 0)["records"]] == list(range(50))  # old versions read as they were
    text = (tmp_path / "store" / SLUG / "v64" / "manifest.json").read_text(encoding="utf-8")
    assert "\n" not in text and json.loads(text)["version"] == 64  # one line: every version lists every source


def test_a_full_part_is_never_rewritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(node_write, "COMPACT_MAX_BYTES", 1)  # every part counts as full
    node = new_node(tmp_path / "store")
    for c in range(3):
        node.contribute(SLUG, batch(c), None)
    assert [p["records"] for p in node.store.manifest(SLUG)["parts"]] == [5, 5, 5]


def test_requests_read_only_the_versions_they_have_not_seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "store"
    node = new_node(root)
    for c in range(30):
        node.contribute(SLUG, batch(c), None)
    reads: list[str] = []
    real = Path.read_text

    def spy(self: Path, *a, **k) -> str:  # type: ignore[no-untyped-def]
        if self.name == "manifest.json":
            reads.append(self.parent.name)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy)
    store = Store(root)  # a node that just started
    assert store.versions(SLUG) == list(range(30, 0, -1)) and len(reads) == 30
    reads.clear()
    for _ in range(3):
        store.versions(SLUG)
        store.slugs()
        assert store.manifest(SLUG)["version"] == 30
    assert reads == []  # nothing parsed again
    node.contribute(SLUG, batch(30), None)  # the writer reads its parent, v30
    reads.clear()
    assert store.versions(SLUG)[0] == 31 and reads == ["v31"]  # only the new one

    # a version whose part goes missing is not served; the newest complete one is
    m31 = store.manifest(SLUG, 31)
    (root / SLUG / "parts" / f"{m31['parts'][-1]['sha256']}.parquet").unlink()
    assert store.manifest(SLUG)["version"] == 30 and 31 not in store.versions(SLUG)
    with pytest.raises(NodeError) as ei:
        store.manifest(SLUG, 31)
    assert ei.value.status == 404


def test_keep_versions_drops_old_versions_and_the_parts_only_they_used(tmp_path: Path) -> None:
    root = tmp_path / "store"
    srv = Server(root, port=0, quiet=True, keep_versions=3).start()
    try:
        w = Witan("k", base_url=srv.url)
        w.projects.create(SLUG, "Runs log", README, SCHEMA)
        for c in range(20):
            assert w.projects.contribute(SLUG, batch(c)["records"])["mergedVersion"] == c + 1
        assert version_dirs(root) == [18, 19, 20]
        kept = {f"{p['sha256']}.parquet" for v in (18, 19, 20) for p in w.projects.manifest(SLUG, version=v)["parts"]}
        assert part_files(root) == kept  # nothing else: no old part, no temp file
        assert w.projects.data(SLUG, limit=1000)["count"] == 100
        with pytest.raises(WitanError) as ei:
            w.projects.data(SLUG, version=17)
        assert ei.value.status == 404
        h = httpx.get(f"{srv.url}/healthz").json()
        assert h["keepVersions"] == 3 and h["versions"] == 3
        again = w.projects.contribute(SLUG, batch(0)["records"])  # v1's records are still known
        assert again["status"] == "rejected" and again["verdict"]["gate"] == "dedup"
    finally:
        srv.close()


def test_starting_with_keep_versions_prunes_the_store_it_finds(tmp_path: Path) -> None:
    root = tmp_path / "store"
    node = new_node(root)
    for c in range(10):
        node.contribute(SLUG, batch(c), None)
    orphan = root / SLUG / "parts" / f"{'0' * 64}.parquet"  # a crash between part and manifest
    orphan.write_bytes(b"PAR1....PAR1")
    leftover = root / SLUG / "parts" / ".dead.parquet.tmp"
    leftover.write_bytes(b"x")
    copy = root / "origin-copy"  # a pulled copy the node neither writes nor follows: left alone
    (copy / "parts").mkdir(parents=True)
    for v in (1, 2):
        (copy / f"v{v}").mkdir()
        (copy / f"v{v}" / "manifest.json").write_text(json.dumps({"format": "parquet", "project": "origin-copy", "version": v,
                                                                  "parts": [], "totals": {"records": 0}}))
    before = sum(f.stat().st_size for f in (root / SLUG / "parts").iterdir())
    srv = Server(root, port=0, quiet=True, keep_versions=2)
    try:
        assert srv.pruned["versions"] == 8 and srv.pruned["parts"] >= 2 and 0 < srv.pruned["bytes"] < before
        assert version_dirs(root) == [9, 10] and version_dirs(root, "origin-copy") == [1, 2]
        assert not orphan.exists() and not leftover.exists()
        kept = {f"{p['sha256']}.parquet" for v in (9, 10) for p in srv.node.store.manifest(SLUG, v)["parts"]}
        assert part_files(root) == kept
    finally:
        srv.close()


def test_a_part_being_read_outlives_its_version(tmp_path: Path) -> None:
    node = new_node(tmp_path / "store", keep_versions=1)
    node.contribute(SLUG, batch(0), None)
    m1 = node.store.manifest(SLUG, 1)
    p1 = tmp_path / "store" / SLUG / "parts" / f"{m1['parts'][0]['sha256']}.parquet"
    with node.store.reading(SLUG, m1):
        node.contribute(SLUG, batch(1), None)  # folds v1's part into a new one and drops v1
        assert version_dirs(tmp_path / "store") == [2]
        assert p1.exists()  # still being read
    m2 = node.store.manifest(SLUG, 2)
    node.contribute(SLUG, batch(2), None)  # folds v2's part too; the prune takes both
    assert not p1.exists()
    # a read that looked a version up just before it was dropped is told so, not handed a broken read
    with pytest.raises(NodeError) as ei:
        with node.store.reading(SLUG, m2):
            pass
    assert ei.value.status == 404 and "--keep-versions" in ei.value.message


def test_follow_with_keep_versions_keeps_shared_parts_and_drops_the_rest(tmp_path: Path) -> None:
    root = tmp_path / "store"
    slug = "agent-api-observatory"
    parts = root / slug / "parts"
    parts.mkdir(parents=True)
    old1, old2 = b"PAR1" + b"1" * 10 + b"PAR1", b"PAR1" + b"2" * 10 + b"PAR1"
    import hashlib

    refs = {}
    for name, data in (("old1", old1), ("old2", old2), ("a", PART_A)):
        sha = hashlib.sha256(data).hexdigest()
        (parts / f"{sha}.parquet").write_bytes(data)
        refs[name] = {"sha256": sha, "bytes": len(data), "records": 1}
    for v, ps in ((100, [refs["old1"], refs["a"]]), (105, [refs["a"], refs["old2"]])):
        (root / slug / f"v{v}").mkdir()
        (root / slug / f"v{v}" / "manifest.json").write_text(json.dumps({
            "format": "parquet", "project": slug, "version": v, "parts": ps,
            "totals": {"records": len(ps), "bytes": sum(p["bytes"] for p in ps), "parts": len(ps), "contributions": v}}))
    fake = ProjectFake()
    origin = Witan("km_test", base_url="http://api.test", transport=httpx.MockTransport(fake))
    srv = Server(root, port=0, follow=[slug], interval=3600, origin=origin, quiet=True, keep_versions=1)
    try:
        assert version_dirs(root, slug) == [105]  # pruned at start: v100 and the part only it used
        assert part_files(root, slug) == {f"{refs['a']['sha256']}.parquet", f"{refs['old2']['sha256']}.parquet"}
        srv.follower.sync_once()  # type: ignore[union-attr]
        assert srv.node.follow[slug]["version"] == 110 and srv.node.follow[slug]["error"] is None
        assert version_dirs(root, slug) == [110]
        assert part_files(root, slug) == {f"{SHA_A}.parquet", f"{SHA_B}.parquet"}
        assert not any(str(r.url).endswith("/a") for r in fake.calls)  # the shared part was kept, not fetched again
    finally:
        srv.close()


def test_mcp_creates_a_local_project_and_says_what_this_node_writes(tmp_path: Path) -> None:
    root = tmp_path / "store"

    def rpc(url: str, mid: int, method: str, params: dict | None = None) -> dict:
        return httpx.post(f"{url}/mcp", json={"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}).json()

    srv = Server(root, port=0, quiet=True).start()
    try:
        made = rpc(srv.url, 1, "tools/call", {"name": "create_dataset", "arguments": {
            "slug": SLUG, "title": "Runs log", "readme": README, "schemaDef": SCHEMA}})
        assert json.loads(made["result"]["content"][0]["text"])["local"] is True
        added = rpc(srv.url, 2, "tools/call", {"name": "contribute_records", "arguments": {"slug": SLUG, **batch(0)}})
        assert json.loads(added["result"]["content"][0]["text"])["mergedVersion"] == 1
        bad = rpc(srv.url, 3, "tools/call", {"name": "create_dataset", "arguments": {
            "slug": "other-one", "title": "Other", "readme": README, "schemaDef": SCHEMA, "access": "paid"}})
        assert bad["result"]["isError"] is True and "does not sell" in bad["result"]["content"][0]["text"]
    finally:
        srv.close()
    ro = Server(root, port=0, quiet=True, read_only=True).start()
    try:
        tools = [t["name"] for t in rpc(ro.url, 1, "tools/list")["result"]["tools"]]
        assert "create_dataset" not in tools and "contribute_records" not in tools
        instructions = rpc(ro.url, 2, "initialize", {"protocolVersion": "2025-06-18"})["result"]["instructions"]
        assert "read-only" in instructions and "contribute_records" not in instructions
    finally:
        ro.close()


def test_keep_versions_must_be_a_positive_number(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for bad in ("0", "-2", "many"):
        with pytest.raises(SystemExit) as ei:
            main(["serve", "--store", str(tmp_path), "--keep-versions", bad])
        assert ei.value.code == 2
    monkeypatch.setenv("WITAN_NODE_KEEP_VERSIONS", "0")
    with pytest.raises(SystemExit):
        main(["serve", "--store", str(tmp_path)])
    with pytest.raises(WitanError, match="at least 1"):
        Node(Store(tmp_path), keep_versions=0)
