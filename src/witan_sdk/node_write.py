"""Writes on a WITAN node: local projects, contributions, versions.

A node writes only to its **local projects** — created on the node, marked ``"local": true``
in their ``project.json``. Copies of origin projects (pulled, loaded or followed) stay
read-only there, because a version written next to the origin's would fork its history.

A contribution runs the origin's gates without the LLM screen (like a private project on
the origin): the schema contract, the personal-data pattern, and record-level duplicates
against everything the project already holds. It merges inside the request, so the answer
is final — ``merged`` with the new version, or ``rejected`` with the gate and the reason.
Accepted records become a content-addressed Parquet part and a new immutable version
manifest. The part is written first and the version's directory is renamed into place in one
step, so a crash leaves at most an unreferenced part. ``Idempotency-Key`` replays the first
answer for 24 hours; the answer and the key are written after the version, from a journal in
it, and a node stopped in between finishes them on its next write.

Small parts fold together the way the origin compacts a small tail, but a batch folds only
the run of tail parts that are not much bigger than it (``fold_run``): part sizes fall
geometrically towards the tail, so a record is rewritten a few times over the life of a
project rather than once per contribution, and a version holds a handful of small parts
behind the full (1 MiB) ones. Older versions keep the parts they listed; ``--keep-versions``
on ``wtn serve`` drops old versions and the parts only they used.

Duplicate detection hashes each record in the form it is stored in (null fields dropped,
integers and numbers normalized), so the set rebuilt from the parts after a restart agrees
with the one kept while running.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator

from .bundle import EXTRA_COLUMN, SLUG_RE, iter_records

MAX_BATCH_RECORDS = 500
MAX_BATCH_BYTES = 512 * 1024
COMPACT_MAX_BYTES = 1024 * 1024  # a part this big is full: nothing folds into it again
FOLD_FACTOR = 2  # a tail part folds into a new batch while it holds at most this many times the batch's records
IDEMPOTENCY_TTL_S = 24 * 3600
VERSION_DIR = re.compile(r"v[1-9][0-9]*")
FIELD_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,60}$")
TAG = re.compile(r"^[a-z0-9-]{2,30}$")
DUCK_TYPE = {"string": "VARCHAR", "number": "DOUBLE", "integer": "BIGINT", "boolean": "BOOLEAN"}
BIGINT_MIN, BIGINT_MAX = -(2 ** 63), 2 ** 63 - 1
DOUBLE_MAX = sys.float_info.max
CREATE_KEYS = {"slug", "title", "readme", "schemaDef", "license", "tags", "access", "visibility"}
COMMIT_JOURNAL = "commit.json"
SCHEMA_ERRORS = 20  # a rejection names up to this many bad lines, as the origin's does (api/src/worker/datasets.ts)  # in v<N>/ only from the version's commit until its answer is written


class WriteError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# --- the personal-data gate, as the origin keeps it (api/src/pii.ts) ---
# A resident registration number in a string of the record. Written the usual way (six digits, a
# hyphen, seven): the first six are a date and the seventh is 1-8. Written with a space or with
# nothing in between: the same, and the check digit is right — thirteen digits in a row are far more
# often a timestamp in milliseconds or an order number. Digits inside a longer run are not one, and
# the numbers of a record are not read.
_RRN = re.compile(r"(?<![0-9])([0-9]{2})([0-9]{2})([0-9]{2})([-\s]?)([1-8][0-9]{5})([0-9])(?![0-9])")
_RRN_DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
_RRN_WEIGHTS = (2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5)


def has_resident_number(text: str) -> bool:
    for m in _RRN.finditer(text):
        month, day = int(m[2]), int(m[3])
        if not (1 <= month <= 12 and 1 <= day <= _RRN_DAYS[month - 1]):
            continue
        if m[4] == "-":
            return True
        total = sum(int(d) * w for d, w in zip(m[1] + m[2] + m[3] + m[5], _RRN_WEIGHTS))
        if (11 - total % 11) % 10 == int(m[6]):
            return True
    return False


def record_has_resident_number(value: object) -> bool:
    if isinstance(value, str):
        return has_resident_number(value)
    if isinstance(value, (list, tuple)):
        return any(record_has_resident_number(v) for v in value)
    if isinstance(value, dict):
        return any(has_resident_number(str(k)) or record_has_resident_number(v) for k, v in value.items())
    return False


# --- end of the gate ---


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _js_number(v: float) -> str:
    """A float the way JavaScript's JSON.stringify writes it (1.0 → 1, 1e-05 → 0.00001)."""
    if v != v or v in (float("inf"), float("-inf")):
        return "null"
    if v.is_integer() and abs(v) < 1e21:
        return str(int(v))
    if 1e-6 <= abs(v) < 1e21:
        return format(Decimal(repr(v)), "f")
    mantissa, _, exp = repr(v).partition("e")
    e = int(exp)
    return f"{mantissa}e{'+' if e > 0 else '-'}{abs(e)}"


def stable_stringify(v: Any) -> str:
    """The origin's stableStringify: keys sorted recursively, JSON otherwise."""
    if isinstance(v, list):
        return "[" + ",".join(stable_stringify(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(json.dumps(str(k), ensure_ascii=False) + ":" + stable_stringify(v[k]) for k in sorted(v)) + "}"
    if v is None or isinstance(v, bool) or isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return _js_number(v)
    return json.dumps(v, ensure_ascii=False, default=str)


def record_hash(rec: Any) -> str:
    return hashlib.sha256(stable_stringify(rec).encode("utf-8")).hexdigest()


def validate_schema_def(d: Any) -> str | None:
    """The origin's validateSchemaDef."""
    if not isinstance(d, dict) or not isinstance(d.get("fields"), list) or not 1 <= len(d["fields"]) <= 40:
        return "schemaDef.fields must be a non-empty array (max 40)"
    names: set[str] = set()
    for f in d["fields"]:
        name = f.get("name") if isinstance(f, dict) else None
        if not isinstance(name, str) or not FIELD_NAME.match(name):
            return f"invalid field name: {json.dumps(name)}"
        if f.get("type") not in DUCK_TYPE:
            return f"field {name}: type must be string|number|integer|boolean"
        if name in names:
            return f"duplicate field name: {name}"
        names.add(name)
    return None


def schema_reason(errors: list[dict[str, Any]]) -> str:
    """The schema gate's reason, worded as the origin's: the bad lines in order; a full list says so."""
    more = f"; … (the first {SCHEMA_ERRORS})" if len(errors) >= SCHEMA_ERRORS else ""
    return "; ".join(f"line {e['line']}: {e['error']}" for e in errors) + more


def check_record(rec: Any, schema: dict[str, Any]) -> str | None:
    """The origin's checkSchema for one record (JavaScript's typeof rules: a boolean is not a number)."""
    if not isinstance(rec, dict):
        return "not an object"
    for f in schema["fields"]:
        name, kind = f["name"], f["type"]
        v = rec.get(name)
        if v is None:
            if f.get("required") is not False:
                return f'missing required field "{name}"'
            continue
        is_num = isinstance(v, (int, float)) and not isinstance(v, bool)
        if kind == "string" and not isinstance(v, str):
            return f'"{name}" must be string'
        if kind == "boolean" and not isinstance(v, bool):
            return f'"{name}" must be boolean'
        if kind == "number" and not is_num:
            return f'"{name}" must be number'
        if kind == "number" and isinstance(v, int) and abs(v) > DOUBLE_MAX:  # stored as DOUBLE
            return f'"{name}" is out of range for a number (a 64-bit float)'
        if kind == "integer" and not (is_num and (isinstance(v, int) or float(v).is_integer())):
            return f'"{name}" must be integer'
        if kind == "integer" and not BIGINT_MIN <= int(v) <= BIGINT_MAX:  # stored as BIGINT
            return f'"{name}" is out of range for an integer ({BIGINT_MIN} to {BIGINT_MAX})'
    if schema.get("allowExtra") is False:
        known = {f["name"] for f in schema["fields"]}
        for k in rec:
            if k not in known:
                return f'unknown field "{k}"'
    return None


def stored_form(rec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """The record as the part will hold it and a read will return it: schema fields without nulls,
    integers as int, numbers as float; extra fields kept only when the schema allows them
    (``allowExtra`` true — an unset allowExtra lets them through the gate but does not store them)."""
    out: dict[str, Any] = {}
    known = set()
    for f in schema["fields"]:
        known.add(f["name"])
        v = rec.get(f["name"])
        if v is None:
            continue
        if f["type"] == "integer":
            v = int(v)
        elif f["type"] == "number":
            v = float(v)
        out[f["name"]] = v
    if schema.get("allowExtra"):
        for k, v in rec.items():
            if k not in known:
                out[k] = v
    return out


def _write_json(path: Path, obj: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """One line of JSON: every version lists every contribution's sources, so the store holds many of these."""
    path.write_text(json.dumps(manifest, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")


def fold_run(parts: list[dict[str, Any]], batch_records: int) -> int:
    """How many tail parts a new batch folds into its part: walking back from the tail, a part
    joins while it is not full and holds at most ``FOLD_FACTOR`` times the records gathered so far.
    Equal batches fold like a binary counter: each record is rewritten about log2(part/batch) times,
    and the small parts after the full ones stay few."""
    gathered = batch_records
    n = 0
    for p in reversed(parts):
        if int(p["bytes"]) >= COMPACT_MAX_BYTES or int(p["records"]) > FOLD_FACTOR * gathered:
            break
        gathered += int(p["records"])
        n += 1
    return n


def part_sources(part: dict[str, Any]) -> list[dict[str, Any]]:
    """The contributions inside a part (the origin's partSources: an old part names one, flat)."""
    if part.get("sources"):
        return list(part["sources"])
    return [{"contributionId": part.get("contributionId", ""), "agentId": part.get("agentId", ""),
             "mergedInVersion": part.get("mergedInVersion", 0), "records": int(part["records"]), "offset": 0}]


class Writer:
    """Local projects and their contributions, one lock per project. ``on_merge(slug)`` runs after
    each merge with the project's lock still held (the node prunes old versions there)."""

    def __init__(self, root: Path, follow: set[str] | None = None,
                 on_merge: Callable[[str], None] | None = None) -> None:
        self.root = root
        self.follow = follow or set()
        self.on_merge = on_merge
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._hashes: dict[str, set[str]] = {}
        self._busy = 0
        self._idle = threading.Condition()

    def _lock(self, slug: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(slug, threading.Lock())

    @contextlib.contextmanager
    def _writing(self, slug: str) -> Iterator[None]:
        """The project's lock, counted, so a node that is stopping can let the write finish."""
        with self._idle:
            self._busy += 1
        try:
            with self._lock(slug):
                yield
        finally:
            with self._idle:
                self._busy -= 1
                self._idle.notify_all()

    def wait_idle(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for writes in progress; True when none is left."""
        with self._idle:
            return self._idle.wait_for(lambda: self._busy == 0, timeout)

    # ---- projects ----
    def project_file(self, slug: str) -> Path:
        return self.root / slug / "project.json"

    def is_local(self, slug: str) -> bool:
        try:
            return bool(json.loads(self.project_file(slug).read_text(encoding="utf-8")).get("local"))
        except (OSError, ValueError):
            return False

    def create(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise WriteError(400, "body must be an object")
        unknown = sorted(set(body) - CREATE_KEYS)
        if unknown:
            raise WriteError(400, f"body must NOT have additional properties: {unknown[0]}")
        slug = body.get("slug")
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            raise WriteError(400, "body/slug must match the slug pattern (a-z, 0-9, dashes; 3-60 characters)")
        title, readme = body.get("title"), body.get("readme")
        if not isinstance(title, str) or not 4 <= len(title) <= 140:
            raise WriteError(400, "body/title must be 4-140 characters")
        if not isinstance(readme, str) or not 20 <= len(readme) <= 20000:
            raise WriteError(400, "body/readme must be 20-20000 characters")
        err = validate_schema_def(body.get("schemaDef"))
        if err:
            raise WriteError(400, err)
        license_ = body.get("license", "platform-standard")
        if not isinstance(license_, str) or len(license_) > 60:
            raise WriteError(400, "body/license must be a string of at most 60 characters")
        tags = body.get("tags", [])
        if not isinstance(tags, list) or len(tags) > 8 or not all(isinstance(t, str) and TAG.match(t) for t in tags):
            raise WriteError(400, "body/tags must be up to 8 tags of a-z, 0-9 and dashes")
        if body.get("access", "public") != "public":
            raise WriteError(400, "a node does not sell data: access must be public (paid projects live on the origin)")
        visibility = body.get("visibility", "private")
        if visibility not in ("public", "private"):
            raise WriteError(400, "body/visibility must be public or private")
        with self._writing(slug):
            if (self.root / slug).exists() or slug in self.follow:
                raise WriteError(409, "slug already exists on this node")
            (self.root / slug / "parts").mkdir(parents=True)
            project = {
                "slug": slug, "title": title, "readme": readme, "schemaDef": body["schemaDef"], "license": license_,
                "tags": tags, "access": "public", "visibility": visibility, "maintainer": "this node",
                "local": True, "createdAt": _now(),
            }
            _write_json(self.project_file(slug), project)
        return {"id": None, "slug": slug, "visibility": visibility, "local": True}

    # ---- contributions ----
    def _contrib_path(self, slug: str, cid: str) -> Path:
        return self.root / slug / "contributions" / f"{cid}.json"

    def contribution(self, slug: str, cid: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f-]{36}", cid):
            raise WriteError(400, "invalid contribution id")
        if not self._contrib_path(slug, cid).is_file() and self.is_local(slug):  # a merge that stopped after its version
            with self._writing(slug):
                self._settle(slug, self._latest(slug)[0])
        return self._view(slug, cid)

    def _view(self, slug: str, cid: str) -> dict[str, Any]:
        try:
            return json.loads(self._contrib_path(slug, cid).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WriteError(404, "contribution not found") from exc

    def _record(self, slug: str, view: dict[str, Any], idempotency: dict[str, Any] | None) -> None:
        """The contribution's answer, and its Idempotency-Key for replays."""
        (self.root / slug / "contributions").mkdir(exist_ok=True)
        _write_json(self._contrib_path(slug, view["id"]), view)
        if idempotency:
            path, keys = self._idempotency(slug)
            keys[idempotency["key"]] = {k: idempotency[k] for k in ("bodySha", "contributionId", "at")}
            path.parent.mkdir(exist_ok=True)
            _write_json(path, keys)

    def _settle(self, slug: str, version: int) -> None:
        """A version is committed when its directory is renamed into place, with a journal of the
        answer in it; the answer and the Idempotency-Key are written after that and the journal
        removed. A node stopped in between leaves the journal: finish its bookkeeping now, so a
        retry with the same key replays "merged" instead of finding every record a duplicate."""
        if version < 1:
            return
        journal = self.root / slug / f"v{version}" / COMMIT_JOURNAL
        try:
            entry = json.loads(journal.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            entry = None  # torn: the version stands, its answer cannot be rebuilt
        if isinstance(entry, dict) and isinstance(entry.get("view"), dict):
            self._record(slug, entry["view"], entry.get("idempotency"))  # rewriting what did get written is harmless
        journal.unlink()

    def _latest(self, slug: str) -> tuple[int, dict[str, Any] | None]:
        """The newest version with a manifest, looked for from the top (one stat, not one per version)."""
        d = self.root / slug
        with os.scandir(d) as it:
            found = sorted((int(e.name[1:]) for e in it if VERSION_DIR.fullmatch(e.name) and e.is_dir()), reverse=True)
        for v in found:
            try:
                return v, json.loads((d / f"v{v}" / "manifest.json").read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
        return 0, None

    def _known_hashes(self, slug: str, manifest: dict[str, Any] | None) -> set[str]:
        """Every record hash the project holds — kept in memory, rebuilt from the parts after a restart."""
        if slug not in self._hashes:
            seen: set[str] = set()
            if manifest:
                files = [self.root / slug / "parts" / f"{p['sha256']}.parquet" for p in manifest["parts"]]
                for rec in iter_records(files):
                    seen.add(record_hash(rec))
            self._hashes[slug] = seen
        return self._hashes[slug]

    def _idempotency(self, slug: str) -> tuple[Path, dict[str, Any]]:
        path = self.root / slug / "index" / "idempotency.json"
        try:
            keys = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            keys = {}
        now = _dt.datetime.now(_dt.timezone.utc).timestamp()
        return path, {k: v for k, v in keys.items() if now - float(v.get("at", 0)) < IDEMPOTENCY_TTL_S}

    def contribute(self, slug: str, body: Any, idem: str | None) -> tuple[int, dict[str, Any], bool]:
        """Run the gates and merge. Returns (HTTP status, contribution view, replayed)."""
        if not self.is_local(slug):
            raise WriteError(405, f"{slug} on this node is a copy of an origin project — write to the origin")
        if not isinstance(body, dict) or set(body) - {"records", "sourceDeclaration"}:
            raise WriteError(400, "body must be {records, sourceDeclaration?}")
        records = body.get("records")
        source = body.get("sourceDeclaration")
        if not isinstance(records, list) or not 1 <= len(records) <= MAX_BATCH_RECORDS or not all(isinstance(r, dict) for r in records):
            raise WriteError(400, f"body/records must be 1-{MAX_BATCH_RECORDS} objects (bigger batches: several calls)")
        if source is not None and (not isinstance(source, str) or len(source) > 500):
            raise WriteError(400, "body/sourceDeclaration must be at most 500 characters")
        raw = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
        if len(raw.encode("utf-8")) > MAX_BATCH_BYTES:
            raise WriteError(413, f"batch too large (max {MAX_BATCH_BYTES} bytes)")
        if idem is not None and (not idem or len(idem) > 200):
            raise WriteError(400, "Idempotency-Key must be 1-200 characters")
        body_sha = hashlib.sha256(f"{slug}\n{raw}\n{source or ''}".encode("utf-8")).hexdigest()

        with self._writing(slug):
            parent_v, parent = self._latest(slug)
            self._settle(slug, parent_v)
            _, keys = self._idempotency(slug)
            if idem and idem in keys:
                if keys[idem]["bodySha"] != body_sha:
                    raise WriteError(422, "Idempotency-Key was already used with a different project or body")
                return 200, self._view(slug, keys[idem]["contributionId"]), True

            project = json.loads(self.project_file(slug).read_text(encoding="utf-8"))
            schema = project["schemaDef"]
            cid = str(uuid.uuid4())
            view: dict[str, Any] = {"id": cid, "status": "rejected", "recordCount": len(records), "acceptedCount": None,
                                    "verdict": None, "mergedVersion": None, "createdAt": _now(), "sourceDeclaration": source}
            idempotency = ({"key": idem, "bodySha": body_sha, "contributionId": cid,
                            "at": _dt.datetime.now(_dt.timezone.utc).timestamp()} if idem else None)
            known = self._known_hashes(slug, parent)
            accepted: list[dict[str, Any]] = []
            batch: set[str] = set()
            dropped = 0
            verdict: dict[str, Any] | None = None
            schema_errors: list[dict[str, Any]] = []
            for line, rec in enumerate(records, start=1):
                err = check_record(rec, schema)
                if err:
                    schema_errors.append({"line": line, "error": err})
                    if len(schema_errors) >= SCHEMA_ERRORS:
                        break
                    continue
                if schema_errors:  # past the first bad line only the schema is read, to name the other bad lines
                    continue
                if record_has_resident_number(rec):
                    verdict = {"gate": "pii", "reason": "resident registration number pattern detected"}
                    break
                stored = stored_form(rec, schema)
                h = record_hash(stored)
                if h in known or h in batch:
                    dropped += 1
                    continue
                batch.add(h)
                accepted.append(stored)
            if schema_errors:
                verdict = {"gate": "schema", "reason": schema_reason(schema_errors), "errors": schema_errors}
            if verdict is None and not accepted:
                verdict = {"gate": "dedup", "reason": "every record already exists in the dataset"}

            if verdict is None:
                version = parent_v + 1
                view.update({"status": "merged", "acceptedCount": len(accepted), "mergedVersion": version,
                             "verdict": {"ok": True, "droppedDuplicates": dropped, "parts": 1}})
                self._merge(slug, schema, parent, version, accepted, cid, dropped,
                            journal={"view": view, "idempotency": idempotency})
                known.update(batch)
                self._settle(slug, version)  # the answer and the key, then the journal goes
            else:
                view["verdict"] = verdict
                self._record(slug, view, idempotency)
            if view["status"] == "merged" and self.on_merge is not None:
                self.on_merge(slug)
            return 201, view, False

    def _merge(self, slug: str, schema: dict[str, Any], parent: dict[str, Any] | None, version: int,
               accepted: list[dict[str, Any]], cid: str, dropped: int, journal: dict[str, Any]) -> dict[str, Any]:
        import duckdb

        parts_dir = self.root / slug / "parts"
        parent_parts = list((parent or {}).get("parts", []))
        folded = parent_parts[len(parent_parts) - fold_run(parent_parts, len(accepted)):]
        kept = parent_parts[:len(parent_parts) - len(folded)]
        base = {"contributionId": cid, "agentId": "node", "mergedInVersion": version}
        fields = schema["fields"]
        allow_extra = bool(schema.get("allowExtra"))
        columns = [(f["name"], DUCK_TYPE[f["type"]]) for f in fields] + ([(EXTRA_COLUMN, "VARCHAR")] if allow_extra else [])
        names = ", ".join(f'"{n}"' for n, _ in columns)
        known = {f["name"] for f in fields}

        def row(rec: dict[str, Any]) -> tuple[Any, ...]:
            values = [rec.get(f["name"]) for f in fields]
            if allow_extra:
                extra = {k: v for k, v in rec.items() if k not in known}
                values.append(json.dumps(extra, ensure_ascii=False) if extra else None)
            return tuple(values)

        tmp = parts_dir / f".{cid}.parquet.tmp"
        con = duckdb.connect()
        try:
            con.execute(f"CREATE TABLE t ({', '.join(f'{chr(34)}{n}{chr(34)} {ty}' for n, ty in columns)})")
            for p in folded:  # oldest first: the rows keep their order across versions
                part_file = str(parts_dir / f"{p['sha256']}.parquet").replace("'", "''")
                con.execute(f"INSERT INTO t SELECT {names} FROM read_parquet('{part_file}', union_by_name = true)")
            con.executemany(f"INSERT INTO t VALUES ({', '.join('?' for _ in columns)})", [row(r) for r in accepted])
            con.execute(f"COPY t TO '{str(tmp).replace(chr(39), chr(39) * 2)}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
        finally:
            con.close()
        sha = hashlib.sha256(tmp.read_bytes()).hexdigest()
        final = parts_dir / f"{sha}.parquet"
        if final.exists():
            tmp.unlink()
        else:
            os.replace(tmp, final)
        size = final.stat().st_size
        sources: list[dict[str, Any]] = []
        offset = 0
        for p in folded:
            sources += [{**s, "offset": offset + int(s.get("offset", 0))} for s in part_sources(p)]
            offset += int(p["records"])
        sources.append({**base, "records": len(accepted), "offset": offset})
        ref = {"sha256": sha, "bytes": size, "records": offset + len(accepted), **base, "sources": sources}
        parts = kept + [ref]
        totals = (parent or {}).get("totals", {})
        manifest = {
            "format": "parquet", "project": slug, "version": version, "parent": version - 1 if version > 1 else None,
            "createdAt": _now(),
            "schema": {"hash": hashlib.sha256(stable_stringify(schema).encode("utf-8")).hexdigest(),
                       "fields": fields, "allowExtra": allow_extra},
            "parts": parts,
            "totals": {"records": sum(int(p["records"]) for p in parts), "bytes": sum(int(p["bytes"]) for p in parts),
                       "parts": len(parts), "contributions": int(totals.get("contributions", 0) or 0) + 1},
            "fragment": {"contributionId": cid, "agentId": "node", "accepted": len(accepted), "droppedDuplicates": dropped},
            "count": sum(int(p["records"]) for p in parts), "file": "parts/<sha256>.parquet", "source": "local",
        }
        # The version appears in one step: its directory is filled under a name no reader takes for a
        # version (not v<N>), then renamed. A node stopped before that leaves the temporary directory,
        # or with 0.27.2 an empty v<N>/; neither has a manifest, so neither is a version — the next
        # merge of the same number clears it. The part is on disk first: a crash leaves at most an orphan part.
        vdir = self.root / slug / f"v{version}"
        tmp = self.root / slug / f".v{version}.tmp"
        if (vdir / "manifest.json").exists():  # cannot happen: _latest counts it, so version would be past it
            raise WriteError(500, f"v{version} already exists on this node")
        for leftover in (tmp, vdir):
            if leftover.exists():
                shutil.rmtree(leftover)
        tmp.mkdir()
        _write_manifest(tmp / "manifest.json", manifest)
        _write_json(tmp / COMMIT_JOURNAL, journal)
        os.replace(tmp, vdir)
        return manifest
