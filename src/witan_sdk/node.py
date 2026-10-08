"""A WITAN node (``wtn serve``): the origin's read API, SQL and MCP over a local store.

The store is the directory ``pull`` and ``load`` write (``<root>/<slug>/parts/<sha256>.parquet``,
``<root>/<slug>/v<N>/manifest.json``, ``<root>/<slug>/project.json``). A node answers on the
same paths and with the same JSON shapes as the origin, so the SDKs and MCP clients work
against it by changing the base URL:

    GET  /projects                        projects in the store
    GET  /projects/{slug}                 detail: schema, readme, license, local versions
    GET  /projects/{slug}/data            a page of records (version, limit <= 1000, offset)
    GET  /projects/{slug}/manifest        the version manifest; part URLs point at this node
    POST /projects/{slug}/query           SQL over the version as the table `records`
    GET  /projects/{slug}/export          every record of a version as jsonl.gz
    GET  /parts/{slug}/{sha256}.parquet   a part (signed URL when the node has a token)
    POST /projects                        create a local project (same body as the origin)
    POST /projects/{slug}/contribute      append records to a local project; the answer is final
    GET  /projects/{slug}/contributions/{id}
    POST /mcp                             MCP (Streamable HTTP, JSON responses): the dataset tools
    GET  /healthz                         liveness, store summary, follow status

Copies of origin projects (pulled, loaded, followed) are read-only; projects created on the
node itself (``POST /projects``) take writes at ``POST /projects/{slug}/contribute`` with the
origin's gates and merge inside the request (see ``node_write``). ``--read-only`` turns
writes off. ``follow`` keeps chosen projects current by pulling their latest version from
the origin on an interval. ``keep_versions`` (``--keep-versions N``) keeps the newest N versions
of the projects the node writes or follows and deletes the older ones and the parts only they
used; without it nothing is ever deleted. SQL runs in DuckDB, read-only: one SELECT, with file
access limited to the version's own parts. Bound to loopback by default; any other
address requires a token. Web pages cannot use a node behind its user's back: a request must
name the node's own address in Host (DNS rebinding), an Origin header must be a loopback one
unless the request carries the token, and POST bodies must be declared JSON.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import shutil
import signal
import sys
import threading
import time
import zlib
from collections import Counter, OrderedDict
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import parse_qs, urlsplit

from .bundle import PROJECT_KEYS, SLUG_RE, published_manifest, record_from_row
from .errors import WitanError
from .node_write import WriteError, Writer

MAX_DATA_PAGE = 1000
QUERY_MAX_ROWS = 1000
PART_URL_TTL = 3600
MAX_BODY = 1024 * 1024
WRAPPABLE = re.compile(r"(?is)^\s*(select|with|from|values|describe|summarize)\b")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_DIR = re.compile(r"v[1-9][0-9]*")
READ_ONLY_SQL = "queries are read-only — SELECT, WITH, FROM, DESCRIBE, SUMMARIZE or VALUES over the table records"
MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
# Tool annotations, as the origin's MCP gives them: what a client may assume before calling. Every tool
# works on this node's own store, so none is open-world.
MCP_READS = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
MCP_WRITES = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}


class NodeError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _plain(v: Any) -> Any:
    """A DuckDB value as JSON the SDKs read the way they read the origin's answers."""
    import decimal
    import uuid

    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return v if abs(v) <= 2 ** 53 else str(v)
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, decimal.Decimal):
        f = float(v)
        return f if math.isfinite(f) else str(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).hex()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    return str(v)


def _duckdb() -> Any:
    try:
        import duckdb
    except ImportError as exc:
        raise WitanError('a node needs the query extra: pip install "witan-sdk[query]"') from exc
    return duckdb


class Store:
    """The local store ``pull`` and ``load`` write.

    Versions never change, so the store remembers which ones are complete and keeps the last few
    manifests it parsed: a request lists the project directory and reads only what is new, rather
    than parsing every ``v<N>/manifest.json`` and stating every part. The parts of the version a
    request serves are still checked. Manifests come back shared: do not change them.

    ``prune`` (``wtn serve --keep-versions``) drops old versions, then the parts no remaining
    version lists — except parts a request is reading (``reading``), which go on a later prune."""

    MANIFESTS_KEPT = 16  # parsed manifests held in memory (a manifest grows with the project's contributions)

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._complete: dict[str, dict[int, tuple[str, ...]]] = {}  # slug → version found complete → its parts
        self._broken: dict[str, dict[int, Any]] = {}  # slug → version not complete → its manifest's stat then
        self._manifests: OrderedDict[tuple[str, int], tuple[Any, dict[str, Any]]] = OrderedDict()
        self._pruned = False  # once pruning began, a read checks its parts again after taking its hold
        self._pins: Counter[tuple[str, str]] = Counter()  # (slug, sha256) → requests reading the part
        self._doomed: dict[str, set[str]] = {}  # slug → parts to delete once nobody reads them

    def _project_json(self, slug: str) -> dict[str, Any]:
        try:
            p = json.loads((self.root / slug / "project.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return p if isinstance(p, dict) else {}

    def is_local(self, slug: str) -> bool:
        """Created on this node (writable here), as opposed to a copy of an origin project."""
        return SLUG_RE.match(slug) is not None and bool(self._project_json(slug).get("local"))

    def _manifest_stat(self, slug: str, version: int) -> tuple[int, int] | None:
        try:
            st = (self.root / slug / f"v{version}" / "manifest.json").stat()
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    def _parsed(self, slug: str, version: int) -> dict[str, Any] | None:
        """The Parquet manifest of a version, from memory while its file is unchanged."""
        stat = self._manifest_stat(slug, version)
        if stat is None:
            return None
        key = (slug, version)
        with self._lock:
            hit = self._manifests.get(key)
            if hit is not None and hit[0] == stat:
                self._manifests.move_to_end(key)
                return hit[1]
        try:
            m = json.loads((self.root / slug / f"v{version}" / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(m, dict) or m.get("format") != "parquet" or not isinstance(m.get("parts"), list):
            return None
        with self._lock:
            self._manifests[key] = (stat, m)
            while len(self._manifests) > self.MANIFESTS_KEPT:
                self._manifests.popitem(last=False)
        return m

    def _parts_present(self, slug: str, m: dict[str, Any]) -> bool:
        parts = self.root / slug / "parts"
        for p in m["parts"]:
            try:
                if (parts / f"{p['sha256']}.parquet").stat().st_size != int(p["bytes"]):
                    return False
            except (OSError, KeyError, TypeError, ValueError):
                return False
        return True

    def _local(self, slug: str, version: int) -> dict[str, Any] | None:
        """The manifest of a version whose every part is on disk with the listed size; None otherwise."""
        m = self._parsed(slug, version)
        if m is not None and self._parts_present(slug, m):
            refs = tuple(sys.intern(str(p["sha256"])) for p in m["parts"])
            with self._lock:
                self._complete.setdefault(slug, {})[version] = refs
                self._broken.get(slug, {}).pop(version, None)
            return m
        stat = self._manifest_stat(slug, version)
        with self._lock:  # not complete (yet): looked at again once its manifest changes
            self._complete.get(slug, {}).pop(version, None)
            self._manifests.pop((slug, version), None)
            self._broken.setdefault(slug, {})[version] = stat
        return None

    def _version_dirs(self, slug: str) -> set[int]:
        try:
            with os.scandir(self.root / slug) as it:
                return {int(e.name[1:]) for e in it if VERSION_DIR.fullmatch(e.name) and e.is_dir()}
        except OSError:
            return set()

    def versions(self, slug: str) -> list[int]:
        """Versions complete on disk (every part present with the manifest's size), newest first.
        Lists the project directory; only a version not seen complete before is read."""
        if not SLUG_RE.match(slug):
            return []
        names = self._version_dirs(slug)
        with self._lock:
            complete = self._complete.setdefault(slug, {})
            broken = self._broken.setdefault(slug, {})
            for v in [v for v in complete if v not in names]:
                del complete[v]
            for v in [v for v in broken if v not in names]:
                del broken[v]
            unseen = [v for v in names if v not in complete]
            known_broken = dict(broken)
        for v in sorted(unseen):  # oldest first, so the newest stay among the manifests kept in memory
            if v in known_broken and known_broken[v] == self._manifest_stat(slug, v):
                continue  # still the manifest that was incomplete
            self._local(slug, v)
        with self._lock:
            return sorted(self._complete.get(slug, {}), reverse=True)

    def slugs(self) -> list[str]:
        try:
            with os.scandir(self.root) as it:
                names = sorted(e.name for e in it if e.is_dir() and SLUG_RE.match(e.name))
        except OSError:
            return []
        return [s for s in names if self.versions(s) or self.is_local(s)]

    def manifest(self, slug: str, version: int | None = None) -> dict[str, Any]:
        """The manifest of ``version`` (the newest complete one when None), its parts checked on disk."""
        if not SLUG_RE.match(slug):
            raise NodeError(404, "project not found on this node")
        if version is not None:  # a pinned version is looked up directly, without listing the others
            m = self._local(slug, version)
            if m is not None:
                return m
        versions = self.versions(slug)
        if not versions:
            raise NodeError(404, "no published version yet" if self.is_local(slug) else "project not found on this node")
        if version is not None:
            raise NodeError(404, f"version {version} is not on this node (local versions: {', '.join(map(str, versions[:10]))})")
        for v in versions:  # the newest whose parts are still all there
            m = self._local(slug, v)
            if m is not None:
                return m
        raise NodeError(404, "no complete version on this node")

    # ---- reads in progress, and pruning ----
    @contextmanager
    def reading(self, slug: str, m: dict[str, Any]) -> Iterator[None]:
        """Hold the parts of manifest ``m`` while a request reads them: prune leaves them for later."""
        keys = [(slug, str(p["sha256"])) for p in m["parts"]]
        with self._lock:
            self._pins.update(keys)
            pruned = self._pruned
        try:
            if pruned and not self._parts_present(slug, m):  # dropped between the lookup and the hold
                raise NodeError(404, f"v{m.get('version')} of {slug} was just dropped from this node (--keep-versions) — ask again")
            yield
        finally:
            with self._lock:
                self._pins.subtract(keys)
                for k in keys:
                    if self._pins[k] <= 0:
                        del self._pins[k]

    def prune(self, slug: str, keep: int, *, sweep: bool = False) -> dict[str, int]:
        """Keep the newest ``keep`` complete versions of ``slug``: delete the older version directories,
        then every part no remaining version lists. A part a request is reading waits for the next
        prune. ``sweep`` also deletes parts no version ever listed (crash leftovers) — only for a
        project nothing else writes meanwhile. Returns what went: versions, parts and bytes."""
        assert keep >= 1
        with self._lock:
            self._pruned = True
        d = self.root / slug
        parts_dir = d / "parts"
        versions = self.versions(slug)
        gone: set[str] = set()
        dropped = 0
        for v in versions[keep:]:
            vdir = d / f"v{v}"
            with self._lock:
                refs = self._complete.get(slug, {}).get(v, ())
            try:
                (vdir / "manifest.json").unlink()  # the version is gone for every reader from here
            except FileNotFoundError:
                pass
            except OSError:
                continue  # still there: it keeps its parts
            shutil.rmtree(vdir, ignore_errors=True)  # whatever is left of it: without a manifest no reader lists it
            gone.update(refs)
            dropped += 1
            with self._lock:
                self._complete.get(slug, {}).pop(v, None)
                self._manifests.pop((slug, v), None)
        # every version left on disk keeps its parts, complete or not (a pull may be filling one)
        held: set[str] = set()
        remaining = self._version_dirs(slug)
        with self._lock:
            complete = dict(self._complete.get(slug, {}))
            gone |= self._doomed.pop(slug, set())
        for v in remaining:
            if v in complete:
                held.update(complete[v])
            else:
                m = self._parsed(slug, v)
                held.update(str(p.get("sha256")) for p in (m or {}).get("parts", []) if isinstance(p, dict))
        if sweep:
            try:
                with os.scandir(parts_dir) as it:
                    for e in it:
                        if e.name.endswith(".parquet") and SHA_RE.match(e.name[:-8]):
                            gone.add(e.name[:-8])
                        elif e.name.startswith(".") and e.name.endswith(".parquet.tmp"):
                            try:
                                os.unlink(e.path)
                            except OSError:
                                pass
            except OSError:
                pass
        deleted = freed = 0
        waiting: set[str] = set()
        for sha in sorted(gone - held):
            f = parts_dir / f"{sha}.parquet"
            with self._lock:  # a request takes its hold under the same lock: it sees the file or none
                if self._pins.get((slug, sha)):
                    waiting.add(sha)
                    continue
                try:
                    size = f.stat().st_size
                    f.unlink()
                except FileNotFoundError:
                    continue
                except OSError:
                    waiting.add(sha)  # Windows: open somewhere
                    continue
            deleted += 1
            freed += size
        if waiting:
            with self._lock:
                self._doomed.setdefault(slug, set()).update(waiting)
        return {"versions": dropped, "parts": deleted, "bytes": freed}

    def project(self, slug: str) -> dict[str, Any]:
        p = self._project_json(slug)
        schema_def = p.get("schemaDef")
        if not p or not schema_def:  # a pulled copy without project.json: the manifest carries the schema
            s = self.manifest(slug).get("schema") or {}
            schema_def = schema_def or {"fields": s.get("fields", []), "allowExtra": bool(s.get("allowExtra"))}
        return {
            "slug": slug,
            "title": p.get("title") or slug,
            "readme": p.get("readme") or "",
            "schemaDef": schema_def,
            "license": p.get("license") or "unknown",
            "tags": p.get("tags") or [],
            "access": p.get("access") or "public",
            "visibility": p.get("visibility") or "public",
            "maintainer": p.get("maintainer"),
            "local": bool(p.get("local")),
            "createdAt": p.get("createdAt"),
        }

    def parts_dir(self, slug: str) -> Path:
        return self.root / slug / "parts"

    def files(self, slug: str, manifest: dict[str, Any]) -> list[Path]:
        return [self.parts_dir(slug) / f"{p['sha256']}.parquet" for p in manifest["parts"]]


class Node:
    """The state one ``wtn serve`` process answers from."""

    def __init__(self, store: Store, *, token: str | None = None, query_timeout: float = 20.0,
                 max_queries: int = 2, quiet: bool = False, read_only: bool = False,
                 follow_slugs: Iterable[str] = (), keep_versions: int | None = None) -> None:
        if keep_versions is not None and keep_versions < 1:
            raise WitanError("--keep-versions must be at least 1 (the newest version is always kept)")
        self.store = store
        self.read_only = read_only
        self.keep_versions = keep_versions
        self.follow_slugs = list(follow_slugs)
        self.writer = Writer(store.root, set(self.follow_slugs), on_merge=self.prune if keep_versions else None)
        self.token = token or None
        self.query_timeout = query_timeout
        self.queries = threading.BoundedSemaphore(max(1, max_queries))
        self.quiet = quiet
        self.started = _now_iso()
        self.follow: dict[str, dict[str, Any]] = {}
        hidden = {str(store.root.resolve()): "<store>"}
        try:
            home = str(Path.home())
        except RuntimeError:  # no home directory to name
            home = ""
        if len(Path(home).parts) > 1:  # never "/" itself
            hidden.setdefault(home, "~")
        both = {**hidden, **{p.replace("\\", "/"): n for p, n in hidden.items()}}  # Windows paths come both ways
        self._hidden_paths = sorted(both.items(), key=lambda pn: -len(pn[0]))  # the store before the home it sits in

    # ---- signed part URLs (a presigned-URL stand-in when the node has a token) ----
    def sign(self, slug: str, sha: str, exp: int) -> str:
        assert self.token
        return hmac.new(self.token.encode(), f"{slug}/{sha}/{exp}".encode(), hashlib.sha256).hexdigest()

    def part_url(self, base: str, slug: str, sha: str) -> str:
        url = f"{base}/parts/{slug}/{sha}.parquet"
        if self.token:
            exp = int(time.time()) + PART_URL_TTL
            url += f"?exp={exp}&sig={self.sign(slug, sha, exp)}"
        return url

    # ---- reads ----
    def list_projects(self) -> list[dict[str, Any]]:
        out = []
        for slug in self.store.slugs():
            p = self.store.project(slug)
            versions = self.store.versions(slug)
            m = self.store.manifest(slug) if versions else {}
            totals = m.get("totals", {})
            out.append({
                "slug": slug, "title": p["title"], "status": "open", "license": p["license"],
                "access": p["access"], "visibility": p["visibility"], "createdAt": p["createdAt"] or m.get("createdAt"),
                "stars": 0, "contributions": int(totals.get("contributions", 0) or 0),
                "records": int(totals.get("records", 0) or 0), "latestVersion": int(m.get("version", 0)),
                "localVersions": len(versions), "local": p["local"],
            })
        return out

    def detail(self, slug: str) -> dict[str, Any]:
        if not (self.store.versions(slug) or self.store.is_local(slug)):
            raise NodeError(404, "project not found on this node")
        p = self.store.project(slug)
        versions = self.store.versions(slug)
        shown = []
        for v in versions[:10]:
            m = self.store.manifest(slug, v)
            shown.append({"version": v, "manifest": published_manifest(m), "createdAt": m.get("createdAt")})
        return {"id": None, **p, "status": "open", "createdAt": p["createdAt"] or (shown[-1]["createdAt"] if shown else None),
                "stars": 0, "latestVersion": versions[0] if versions else 0, "localVersions": versions,
                "contributors": [], "versions": shown}

    def data(self, slug: str, version: int | None, limit: int, offset: int) -> dict[str, Any]:
        m = self.store.manifest(slug, version)
        # Only the parts the window touches (the manifest's record counts say which), read in one scan.
        chosen: list[Path] = []
        start = 0
        pos = 0
        for part, f in zip(m["parts"], self.store.files(slug, m)):
            n = int(part["records"])
            if pos + n <= offset:
                pos += n
                continue
            if not chosen:
                start = offset - pos
            chosen.append(f)
            pos += n
            if pos - offset >= limit:
                break
        records: list[dict[str, Any]] = []
        if chosen:
            with self.store.reading(slug, m):
                con = _duckdb().connect()
                try:
                    files = ", ".join(_quote(str(f)) for f in chosen)
                    cur = con.execute(f"SELECT * FROM read_parquet([{files}], union_by_name = true) LIMIT {limit} OFFSET {start}")
                    cols = [d[0] for d in cur.description]
                    records = [_plain(record_from_row(cols, row)) for row in cur.fetchall()]
                finally:
                    con.close()
        return {"project": slug, "version": int(m["version"]), "count": len(records), "records": records}

    def manifest(self, slug: str, version: int | None, base: str) -> dict[str, Any]:
        m = published_manifest(self.store.manifest(slug, version))
        m["parts"] = [{**p, "url": self.part_url(base, slug, p["sha256"])} for p in m["parts"]]
        m["urlExpiresAt"] = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=PART_URL_TTL)).isoformat(timespec="seconds")
        return m

    def _redact(self, message: str) -> str:
        """A DuckDB message without this machine's paths: the store as <store>, the home directory as ~."""
        for path, name in self._hidden_paths:
            message = message.replace(path, name)
        return message

    def query(self, slug: str, sql: str, version: int | None, limit: int) -> dict[str, Any]:
        m = self.store.manifest(slug, version)
        with self.store.reading(slug, m):
            return self._query(slug, m, sql, limit)

    def _query(self, slug: str, m: dict[str, Any], sql: str, limit: int) -> dict[str, Any]:
        statement = sql.strip().rstrip(";").strip()
        if not statement:
            raise NodeError(400, "empty statement")
        if not self.queries.acquire(timeout=10):
            raise NodeError(429, "too many queries at once on this node — retry shortly")
        duckdb = _duckdb()
        try:
            con = duckdb.connect(config={"autoinstall_known_extensions": "false", "autoload_known_extensions": "false"})
            try:
                # DuckDB's file allow-lists do not tell reading from writing: an allowed directory takes
                # COPY TO and ATTACH as well. So the list holds exactly this version's parts (no directory
                # to list, attach in or add files to), and only a SELECT runs — DESCRIBE, SUMMARIZE, SHOW,
                # VALUES and FROM are SELECTs too; COPY, ATTACH, EXPORT, INSTALL, SET, CREATE are not.
                files = ", ".join(_quote(str(f.resolve())) for f in self.store.files(slug, m))
                con.execute(f"SET allowed_paths = [{files}]")
                con.execute(f"CREATE VIEW records AS SELECT * FROM read_parquet([{files}], union_by_name = true)")
                con.execute("SET enable_external_access = false")
                con.execute("SET lock_configuration = true")
                wrapped = f"SELECT * FROM ({statement}) AS q LIMIT {limit + 1}" if WRAPPABLE.match(statement) else statement
                for text in (statement, wrapped):  # parsed here, by the parser that runs it, after the lock
                    found = con.extract_statements(text)
                    if len(found) != 1:
                        raise NodeError(400, "one statement at a time")
                    if found[0].type != duckdb.StatementType.SELECT:
                        raise NodeError(400, READ_ONLY_SQL)
                timer = threading.Timer(self.query_timeout, con.interrupt)
                t0 = time.monotonic()
                timer.start()
                try:
                    cur = con.execute(wrapped)
                    desc = cur.description or []
                    rows = cur.fetchmany(limit + 1) if desc else []
                finally:
                    timer.cancel()
                ms = round((time.monotonic() - t0) * 1000)
            except duckdb.InterruptException as exc:
                raise NodeError(408, f"the query ran longer than {self.query_timeout:g} s") from exc
            except duckdb.Error as exc:
                raise NodeError(400, self._redact(str(exc).splitlines()[0])[:300]) from exc
            finally:
                con.close()
        finally:
            self.queries.release()
        truncated = len(rows) > limit
        rows = rows[:limit]
        return {
            "project": slug, "version": int(m["version"]),
            "columns": [d[0] for d in desc], "types": [str(d[1]) for d in desc],
            "rows": [[_plain(v) for v in r] for r in rows], "count": len(rows), "truncated": truncated, "ms": ms,
            "scannedBytes": int(m.get("totals", {}).get("bytes", 0) or 0),
        }

    def export_lines(self, slug: str, version: int) -> Iterable[bytes]:
        from .bundle import iter_records

        m = self.store.manifest(slug, version)
        with self.store.reading(slug, m):
            for rec in iter_records(self.store.files(slug, m)):
                yield (json.dumps(_plain(rec), ensure_ascii=False) + "\n").encode("utf-8")

    def health(self) -> dict[str, Any]:
        slugs = self.store.slugs()
        return {"ok": True, "node": True, "readOnly": self.read_only, "store": str(self.store.root), "since": self.started,
                "projects": len(slugs), "versions": sum(len(self.store.versions(s)) for s in slugs),
                "localProjects": [s for s in slugs if self.store.is_local(s)],
                "keepVersions": self.keep_versions,
                "auth": "token" if self.token else "none", "follow": self.follow}

    # ---- retention (--keep-versions) ----
    def prune(self, slug: str, *, sweep: bool = False) -> dict[str, int]:
        """Drop all but the newest ``keep_versions`` versions of a project this node writes or follows."""
        if not self.keep_versions:
            return {"versions": 0, "parts": 0, "bytes": 0}
        return self.store.prune(slug, self.keep_versions, sweep=sweep)

    def prune_all(self) -> dict[str, int]:
        """At start: every local project (crash leftovers included) and every followed one already in the store."""
        total = {"versions": 0, "parts": 0, "bytes": 0}
        if not self.keep_versions:
            return total
        for slug in self.store.slugs():
            local = self.store.is_local(slug)
            if local or slug in self.follow_slugs:
                for k, v in self.prune(slug, sweep=local).items():
                    total[k] += v
        return total

    # ---- writes (local projects only) ----
    def create_project(self, body: Any) -> dict[str, Any]:
        if self.read_only:
            raise NodeError(405, "this node is read-only (--read-only)")
        try:
            return self.writer.create(body)
        except WriteError as exc:
            raise NodeError(exc.status, exc.message) from exc

    def contribute(self, slug: str, body: Any, idem: str | None) -> tuple[int, dict[str, Any], bool]:
        if self.read_only:
            raise NodeError(405, "this node is read-only (--read-only)")
        if not (self.store.is_local(slug) or self.store.versions(slug)):
            raise NodeError(404, "project not found on this node")
        try:
            return self.writer.contribute(slug, body, idem)
        except WriteError as exc:
            raise NodeError(exc.status, exc.message) from exc

    def contribution(self, slug: str, cid: str) -> dict[str, Any]:
        try:
            return self.writer.contribution(slug, cid)
        except WriteError as exc:
            raise NodeError(exc.status, exc.message) from exc

    # ---- MCP (Streamable HTTP; every POST answered with JSON) ----
    def mcp_tools(self) -> list[dict[str, Any]]:
        slug = {"type": "string", "pattern": SLUG_RE.pattern, "description": "a project slug"}
        version = {"type": "integer", "minimum": 1, "description": "a version on this node (latest local when omitted)"}

        def tool(name: str, title: str, hints: dict[str, bool], description: str, schema: dict[str, Any]) -> dict[str, Any]:
            return {"name": name, "title": title, "description": description, "inputSchema": schema,
                    "annotations": {"title": title, **hints}}

        tools = [
            tool("list_datasets", "List datasets", MCP_READS,
                 "Dataset projects on this WITAN node (copies pulled from an origin or loaded from bundles, and projects created on the node): slug, title, access, visibility, latest local version, record count.",
                 {"type": "object", "properties": {"query": {"type": "string", "maxLength": 100, "description": "substring filter on slug or title"}}}),
            tool("dataset_info", "Dataset details", MCP_READS,
                 "One dataset on this node: readme, record schema (fields, types, required, allowExtra), license, the versions held locally.",
                 {"type": "object", "properties": {"slug": slug}, "required": ["slug"]}),
            tool("read_dataset", "Read dataset records", MCP_READS,
                 "Read records of a local dataset version, paged (50 by default, up to 200). A version never changes, so pages are stable.",
                 {"type": "object", "properties": {"slug": slug, "version": version,
                  "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "offset": {"type": "integer", "minimum": 0}}, "required": ["slug"]}),
            tool("dataset_manifest", "Dataset version manifest", MCP_READS,
                 "The manifest of a local dataset version: schema, totals and the content-addressed Parquet parts, each with a URL on this node.",
                 {"type": "object", "properties": {"slug": slug, "version": version}, "required": ["slug"]}),
            tool("query_dataset", "Query a dataset with SQL", MCP_READS,
                 "Run SQL over a local dataset version: the version's Parquet parts are the table `records` (extra fields of an allowExtra schema are the JSON column `_extra`). Read-only sandbox, one statement, up to 1000 rows (`truncated` says if more matched). DESCRIBE records shows the columns.",
                 {"type": "object", "properties": {"slug": slug, "sql": {"type": "string", "minLength": 1, "maxLength": 4000},
                  "version": version, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, "required": ["slug", "sql"]}),
        ]
        if self.read_only:
            return tools
        return tools + [
            tool("create_dataset", "Create a dataset project", MCP_WRITES,
                 "Create a local project on this node — it takes writes here (contribute_records); copies of origin projects do not. A slug, a title, a readme (20+ characters) that says what belongs in it, and the record schema every contribution must match. A node does not sell data: access is public, visibility private by default. Schema and visibility do not change later.",
                 {"type": "object", "properties": {
                     "slug": slug, "title": {"type": "string", "minLength": 4, "maxLength": 140},
                     "readme": {"type": "string", "minLength": 20, "maxLength": 20000, "description": "what the records are, how they are measured, what does not belong"},
                     "schemaDef": {"type": "object", "description": "the record contract: fields with a name and a type (string, number, integer, boolean), and whether extra fields are kept",
                                   "properties": {"fields": {"type": "array", "minItems": 1, "maxItems": 40, "items": {"type": "object", "properties": {
                                       "name": {"type": "string", "pattern": "^[a-zA-Z][a-zA-Z0-9_]{0,60}$"},
                                       "type": {"type": "string", "enum": ["string", "number", "integer", "boolean"]},
                                       "required": {"type": "boolean"}}, "required": ["name", "type"]}},
                                       "allowExtra": {"type": "boolean"}}, "required": ["fields"]},
                     "license": {"type": "string", "maxLength": 60},
                     "tags": {"type": "array", "maxItems": 8, "items": {"type": "string", "pattern": "^[a-z0-9-]{2,30}$"}},
                     "visibility": {"type": "string", "enum": ["public", "private"]}},
                  "required": ["slug", "title", "readme", "schemaDef"]}),
            tool("contribute_records", "Contribute records", MCP_WRITES,
                 "Append a batch of records (1-500 JSON objects matching the schema) to a local project on this node — one created here, not a copy of an origin project. The node runs the schema, personal-data and duplicate gates and merges in the same call: the answer is merged (mergedVersion, acceptedCount) or rejected (verdict names the gate and the reason). Pass idempotencyKey (a token unique to this write) so a retried call replays the first answer instead of writing twice.",
                 {"type": "object", "properties": {"slug": slug,
                  "records": {"type": "array", "minItems": 1, "maxItems": 500, "items": {"type": "object"}},
                  "sourceDeclaration": {"type": "string", "maxLength": 500, "description": "where the records come from"},
                  "idempotencyKey": {"type": "string", "minLength": 1, "maxLength": 200},
                  "wait": {"type": "integer", "minimum": 0, "maximum": 20, "description": "accepted for parity with the origin; a node always answers with the final status"}},
                  "required": ["slug", "records"]}),
            tool("contribution_status", "Contribution status", MCP_READS,
                 "A contribution on this node: merged (mergedVersion, acceptedCount) or rejected (verdict).",
                 {"type": "object", "properties": {"slug": slug, "id": {"type": "string", "format": "uuid"}}, "required": ["slug", "id"]}),
        ]

    def mcp_instructions(self) -> str:
        reads = ("list_datasets and dataset_info to discover, read_dataset for a page, query_dataset for SQL over the table "
                 "`records`, dataset_manifest for the Parquet parts. Versions never change once published.")
        if self.read_only:
            return ("A WITAN node, read-only: local copies of dataset versions (pulled from a WITAN origin, loaded from bundles "
                    "or created on the node), served with no network. The same dataset tools as the origin: " + reads +
                    " This node takes no writes; write to the origin.")
        return ("A WITAN node: local copies of dataset versions (pulled from a WITAN origin or loaded from bundles), served "
                "with no network, and projects created on the node. The same dataset tools as the origin: " + reads +
                " Copies of origin projects are read-only here — write those to the origin. A project created on this node "
                "(create_dataset) takes records with contribute_records: the node runs the schema, personal-data and "
                "duplicate gates and answers merged or rejected in the same call.")

    def mcp_call(self, name: str, args: dict[str, Any], base: str) -> Any:
        def slug() -> str:
            s = args.get("slug")
            if not isinstance(s, str) or not SLUG_RE.match(s):
                raise NodeError(400, "slug must be a project slug")
            return s

        def opt_int(key: str, lo: int, hi: int | None, default: int | None) -> int | None:
            v = args.get(key, default)
            if v is None:
                return None
            if not isinstance(v, int) or isinstance(v, bool) or v < lo or (hi is not None and v > hi):
                raise NodeError(400, f"{key} must be an integer in [{lo}, {hi if hi is not None else '∞'}]")
            return v

        if name == "list_datasets":
            q = str(args.get("query") or "").lower()
            projects = self.list_projects()
            return {"projects": [p for p in projects if not q or q in p["slug"] or q in p["title"].lower()]}
        if name == "dataset_info":
            return self.detail(slug())
        if name == "read_dataset":
            return self.data(slug(), opt_int("version", 1, None, None), opt_int("limit", 1, 200, 50) or 50, opt_int("offset", 0, None, 0) or 0)
        if name == "dataset_manifest":
            return self.manifest(slug(), opt_int("version", 1, None, None), base)
        if name == "query_dataset":
            sql = args.get("sql")
            if not isinstance(sql, str) or not sql.strip() or len(sql) > 4000:
                raise NodeError(400, "sql must be a statement of 1-4000 characters")
            return self.query(slug(), sql, opt_int("version", 1, None, None), opt_int("limit", 1, QUERY_MAX_ROWS, 200) or 200)
        if name == "create_dataset" and not self.read_only:
            return self.create_project(dict(args))
        if name == "contribute_records" and not self.read_only:
            body: dict[str, Any] = {"records": args.get("records")}
            if args.get("sourceDeclaration") is not None:
                body["sourceDeclaration"] = args["sourceDeclaration"]
            key = args.get("idempotencyKey")
            _, view, replayed = self.contribute(slug(), body, key if isinstance(key, str) else None)
            return {**view, "replayed": replayed}
        if name == "contribution_status" and not self.read_only:
            cid = args.get("id")
            return self.contribution(slug(), cid if isinstance(cid, str) else "")
        raise NodeError(404, f"unknown tool: {name}")

    def mcp(self, message: Any, base: str) -> dict[str, Any] | None:
        from . import __version__

        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid JSON-RPC request"}}
        mid = message.get("id")
        method = message["method"]
        params = message.get("params") or {}
        if mid is None:  # a notification: nothing to answer
            return None

        def ok(result: Any) -> dict[str, Any]:
            return {"jsonrpc": "2.0", "id": mid, "result": result}

        if method == "initialize":
            asked = params.get("protocolVersion")
            return ok({
                "protocolVersion": asked if asked in MCP_VERSIONS else MCP_VERSIONS[1],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "witan-node", "version": __version__},
                "instructions": self.mcp_instructions(),
            })
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": self.mcp_tools()})
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if not isinstance(name, str) or not isinstance(args, dict):
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "tools/call needs name and arguments"}}
            try:
                result = self.mcp_call(name, args, base)
                return ok({"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]})
            except NodeError as exc:
                return ok({"content": [{"type": "text", "text": f"{exc.status}: {exc.message}"}], "isError": True})
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


class _Handler(BaseHTTPRequestHandler):
    server_version = "witan-node"
    node: Node  # set on the server class per node

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401 - http.server hook
        if not self.server.node.quiet:  # type: ignore[attr-defined]
            sys.stderr.write(f"[node] {self.address_string()} {fmt % args}\n")

    # ---- plumbing ----
    @property
    def _node(self) -> Node:
        return self.server.node  # type: ignore[attr-defined]

    def _base(self) -> str:
        host = self.headers.get("host") or f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        return f"http://{host}"

    def _send_json(self, status: int, body: Any, extra: dict[str, str] | None = None) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "no-store")
        self.send_header("x-witan-node", "1")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _body(self) -> Any:
        try:
            n = int(self.headers.get("content-length") or 0)
        except ValueError as exc:
            raise NodeError(400, "content-length must be a number") from exc
        if n < 0:  # rfile.read(-1) would wait for the client to close the connection
            raise NodeError(400, "content-length must not be negative")
        if n > MAX_BODY:
            raise NodeError(413, "request body too large")
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError) as exc:
            raise NodeError(400, "body must be JSON") from exc

    def _authorized(self) -> bool:
        token = self._node.token
        if not token:
            return True
        header = self.headers.get("authorization") or ""
        given = header[7:] if header.startswith("Bearer ") else ""
        return hmac.compare_digest(given.encode(), token.encode())

    def _host_ok(self) -> bool:
        """Host must be this node's own address: a page whose name was rebound to 127.0.0.1 sends
        its own name, and must not reach the store through the visitor's browser."""
        bound, port = self.server.server_address[:2]
        if bound in ("0.0.0.0", "::", ""):  # every interface: a token is required there, and it is the guard
            return True
        names = {"localhost", "127.0.0.1", "[::1]", f"[{bound}]" if ":" in bound else bound}
        allowed = {f"{n}:{port}" for n in names} | (names if port == 80 else set())
        return (self.headers.get("host") or "").strip().lower() in allowed

    def _guard(self, method: str) -> None:
        if not self._host_ok():
            raise NodeError(403, "this node answers only to its own address (Host) — a guard against DNS rebinding")
        origin = self.headers.get("origin")
        if origin is not None and not _loopback_origin(origin) and not (self._node.token and self._authorized()):
            raise NodeError(403, f"requests from web pages at {origin[:100]} are refused — a node trusts other origins only "
                                 "with its token")
        if method == "POST":
            ctype = (self.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise NodeError(415, "POST bodies must be JSON (content-type: application/json)")

    @staticmethod
    def _int(q: dict[str, list[str]], key: str, default: int | None, lo: int, hi: int | None) -> int | None:
        if key not in q:
            return default
        try:
            v = int(q[key][0])
        except ValueError as exc:
            raise NodeError(400, f"querystring/{key} must be integer") from exc
        if v < lo or (hi is not None and v > hi):
            raise NodeError(400, f"querystring/{key} must be >= {lo}" + (f" and <= {hi}" if hi is not None else ""))
        return v

    # ---- dispatch ----
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        try:
            self._route(method)
        except NodeError as exc:
            self.close_connection = True  # a refused request's body may still be unread
            self._send_json(exc.status, {"error": exc.message})
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - one request must not take the node down
            sys.stderr.write(f"[node] error on {method} {self.path}: {type(exc).__name__}: {exc}\n")
            try:
                self._send_json(500, {"error": "internal error on the node"})
            except OSError:
                pass

    def _route(self, method: str) -> None:
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        q = parse_qs(url.query)
        seg = [s for s in path.split("/") if s]
        node = self._node
        self._guard(method)

        if path == "/healthz" and method == "GET":
            if node.token and not self._authorized():  # liveness only: the store stays private
                return self._send_json(200, {"ok": True})
            return self._send_json(200, node.health())
        if len(seg) == 3 and seg[0] == "parts" and method == "GET":
            return self._part(seg[1], seg[2], q)
        if not self._authorized():
            raise NodeError(401, "this node needs its token: Authorization: Bearer <token>")
        if path == "/mcp":
            if method != "POST":
                return self._send_json(405, {"error": "MCP on this node is POST only (Streamable HTTP, JSON responses)"}, {"allow": "POST"})
            body = self._body()
            if isinstance(body, list):
                replies = [r for r in (node.mcp(m, self._base()) for m in body) if r is not None]
                return self._send_json(200, replies) if replies else self._send_json(202, {})
            reply = node.mcp(body, self._base())
            return self._send_json(202, {}) if reply is None else self._send_json(200, reply)
        if path == "/projects" and method == "GET":
            return self._send_json(200, {"projects": node.list_projects()})
        if path == "/projects" and method == "POST":
            return self._send_json(201, node.create_project(self._body()))
        if len(seg) >= 2 and seg[0] == "projects":
            slug = seg[1]
            if not SLUG_RE.match(slug):
                raise NodeError(404, "project not found on this node")
            rest = "/".join(seg[2:])
            if method == "POST" and rest == "contribute":
                self._int(q, "wait", 0, 0, 20)  # accepted for parity with the origin; the answer is always final
                header = self.headers.get("idempotency-key")
                status, view, replayed = node.contribute(slug, self._body(), header.strip() if header is not None else None)
                return self._send_json(status, view, {"idempotent-replayed": "true"} if replayed else None)
            if method == "GET" and len(seg) == 4 and seg[2] == "contributions":
                self._int(q, "wait", 0, 0, 20)
                return self._send_json(200, node.contribution(slug, seg[3]))
            if rest.startswith("uploads"):
                raise NodeError(405, "a node takes JSON batches of up to 500 records at /contribute; multipart uploads go to the origin")
            if method != "GET" and not (method == "POST" and rest == "query"):
                raise NodeError(405, "not writable on a node — write to the origin")
            if rest == "":
                return self._send_json(200, node.detail(slug))
            if rest == "data":
                return self._send_json(200, node.data(slug, self._int(q, "version", None, 1, None),
                                                      self._int(q, "limit", 200, 1, MAX_DATA_PAGE) or 200,
                                                      self._int(q, "offset", 0, 0, None) or 0))
            if rest == "manifest":
                return self._send_json(200, node.manifest(slug, self._int(q, "version", None, 1, None), self._base()))
            if rest == "query":
                body = self._body()
                sql = body.get("sql") if isinstance(body, dict) else None
                if not isinstance(sql, str) or not sql.strip() or len(sql) > 4000:
                    raise NodeError(400, "body/sql must be a statement of 1-4000 characters")
                version = body.get("version")
                limit = body.get("limit", 200)
                if version is not None and (not isinstance(version, int) or isinstance(version, bool) or version < 1):
                    raise NodeError(400, "body/version must be an integer >= 1")
                if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= QUERY_MAX_ROWS:
                    raise NodeError(400, f"body/limit must be an integer in [1, {QUERY_MAX_ROWS}]")
                return self._send_json(200, node.query(slug, sql, version, limit))
            if rest == "export":
                version = self._int(q, "version", None, 1, None)
                if version is None:
                    raise NodeError(400, "querystring must have required property 'version'")
                return self._export(slug, version)
            raise NodeError(404, f"not served by a WITAN node: /projects/{slug}/{rest} — ask the origin")
        raise NodeError(404, "not found on this node")

    def _part(self, slug: str, name: str, q: dict[str, list[str]]) -> None:
        node = self._node
        if not SLUG_RE.match(slug) or not name.endswith(".parquet") or not SHA_RE.match(name[:-8]):
            raise NodeError(404, "part not found")
        sha = name[:-8]
        if node.token:
            try:
                exp = int(q.get("exp", ["0"])[0])
            except ValueError:
                exp = 0
            sig = q.get("sig", [""])[0]
            if exp < time.time() or not hmac.compare_digest(sig.encode(), node.sign(slug, sha, exp).encode()):
                raise NodeError(403, "part URL signature is missing, wrong or expired — ask for the manifest again")
        f = node.store.parts_dir(slug) / name
        try:  # open first: a part pruned meanwhile keeps reading from the open file (or was never sent)
            fh = f.open("rb")
        except OSError as exc:
            raise NodeError(404, "part not found") from exc
        with fh:
            size = os.fstat(fh.fileno()).st_size
            self.send_response(200)
            self.send_header("content-type", "application/vnd.apache.parquet")
            self.send_header("content-length", str(size))
            self.send_header("x-witan-node", "1")
            self.end_headers()
            if self.command == "HEAD":
                return
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _export(self, slug: str, version: int) -> None:
        self._node.store.manifest(slug, version)  # a missing version answers 404 before the stream starts
        lines = self._node.export_lines(slug, version)
        self.send_response(200)
        self.send_header("content-type", "application/gzip")
        self.send_header("content-disposition", f'attachment; filename="{slug}-v{version}.jsonl.gz"')
        self.send_header("x-witan-node", "1")
        self.send_header("connection", "close")
        self.end_headers()
        gz = zlib.compressobj(6, zlib.DEFLATED, 31)
        with closing(lines):  # a client that goes away mid-stream releases the parts' hold at once
            for line in lines:
                self.wfile.write(gz.compress(line))
        self.wfile.write(gz.flush())
        self.close_connection = True


class Follower(threading.Thread):
    """Keeps chosen projects current: pull the latest version from the origin every ``interval`` seconds."""

    def __init__(self, node: Node, origin: Any, slugs: list[str], interval: float,
                 log: Callable[[str], None] | None = None, verify: bool | None = None) -> None:
        super().__init__(name="witan-follow", daemon=True)
        self.node = node
        self.origin = origin
        self.verify = verify
        self.slugs = slugs
        self.interval = max(1.0, float(interval))
        self.stop = threading.Event()
        self.log = log or (lambda s: sys.stderr.write(s + "\n"))
        for s in slugs:
            node.follow[s] = {"version": None, "at": None, "error": None}

    def sync_once(self) -> None:
        root = self.node.store.root
        for slug in self.slugs:
            status = self.node.follow[slug]
            try:
                m = self.origin.projects.pull(slug, root, verify=self.verify)  # a bad signature keeps the old version
                if m.get("format") != "parquet":
                    raise WitanError(f"v{m.get('version')} is not available as Parquet parts")
                detail = self.origin.projects.get(slug)
                project = {k: detail[k] for k in PROJECT_KEYS if k in detail}
                (root / slug / "project.json").write_text(json.dumps(project, indent=2, ensure_ascii=False), encoding="utf-8")
                changed = status["version"] != m["version"]
                status.update({"version": int(m["version"]), "at": _now_iso(), "error": None,
                               "signature": m.get("verified"), "from": self.origin.base_url})
                if changed:
                    self.log(f"[follow] {slug} v{m['version']} ({m.get('downloaded', 0)} new parts)")
                    gone = self.node.prune(slug)
                    if gone["versions"]:
                        self.log(f"[follow] {slug}: dropped {_gone(gone)} (--keep-versions {self.node.keep_versions})")
            except Exception as exc:  # noqa: BLE001 - keep following the others
                status.update({"at": _now_iso(), "error": str(exc)[:300]})
                self.log(f"[follow] {slug}: {exc}")

    def run(self) -> None:
        while not self.stop.is_set():
            self.sync_once()
            self.stop.wait(self.interval)


def _interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def _gone(g: dict[str, int]) -> str:
    return f"{g['versions']} old versions and {g['parts']} parts ({g['bytes'] / 1e6:.1f} MB)"


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _loopback_origin(origin: str) -> bool:
    """An Origin header of a page served from this machine (any port)."""
    try:
        u = urlsplit(origin.strip())
        host = u.hostname or ""
    except ValueError:
        return False
    return u.scheme in ("http", "https") and _loopback(host.lower())


class Server:
    """A running node: HTTP server thread plus the optional follower."""

    def __init__(self, store_dir: "str | os.PathLike[str]", *, host: str = "127.0.0.1", port: int = 8686,
                 token: str | None = None, follow: Iterable[str] = (), interval: float = 600.0,
                 origin: Any = None, query_timeout: float = 20.0, quiet: bool = False, read_only: bool = False,
                 verify: bool | None = None, keep_versions: int | None = None) -> None:
        if not _loopback(host) and not token:
            raise WitanError(f"binding {host} exposes the store beyond this machine — set a token (--token or WITAN_NODE_TOKEN)")
        slugs = list(follow)
        bad = [s for s in slugs if not SLUG_RE.match(s)]
        if bad:
            raise WitanError(f"not a project slug: {bad[0]}")
        if slugs and origin is None:
            raise WitanError("follow needs an origin client")
        _duckdb()
        root = Path(store_dir)
        root.mkdir(parents=True, exist_ok=True)
        store = Store(root)
        local = [s for s in slugs if store.is_local(s)]
        if local:
            raise WitanError(f"{local[0]} is a local project on this node — there is no origin version to follow")
        self.node = Node(store, token=token, query_timeout=query_timeout, quiet=quiet, read_only=read_only, follow_slugs=slugs,
                         keep_versions=keep_versions)
        self.pruned = self.node.prune_all()  # before the first request: old versions, and a local project's crash leftovers
        if (self.pruned["versions"] or self.pruned["parts"]) and not quiet:
            sys.stderr.write(f"[node] dropped {_gone(self.pruned)} (--keep-versions {keep_versions})\n")
        handler = type("NodeHandler", (_Handler,), {})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.httpd.node = self.node  # type: ignore[attr-defined]
        self.follower = (Follower(self.node, origin, slugs, interval, log=None if not quiet else (lambda s: None), verify=verify)
                         if slugs else None)
        self._thread: threading.Thread | None = None
        self._serving = False  # shutdown() waits for serve_forever, so only call it once serving began

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "Server":
        """Serve on a background thread (tests, embedding)."""
        if self.follower:
            self.follower.start()
        self._serving = True
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="witan-node", daemon=True)
        self._thread.start()
        return self

    def close(self, drain: float = 5.0) -> None:
        """Stop taking requests, give writes in progress up to ``drain`` seconds to finish, close."""
        if self.follower:
            self.follower.stop.set()
        if self._serving:
            self.httpd.shutdown()
            self._serving = False
        self.node.writer.wait_idle(drain)
        self.httpd.server_close()

    def serve_forever(self) -> None:
        """Serve until Ctrl-C or SIGTERM (docker stop, Kubernetes): both stop the node the same way.
        As PID 1 in a container, a SIGTERM with no handler is ignored and ends in SIGKILL."""
        if self.follower:
            self.follower.start()
        self._serving = True
        main = threading.current_thread() is threading.main_thread()  # signal handlers live on the main thread
        previous = signal.signal(signal.SIGTERM, _interrupt) if main else None
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if main:  # a second SIGTERM while draining gets the old disposition
                signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)
            self.close()
