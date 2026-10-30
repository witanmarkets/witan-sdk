"""Read a data file (CSV, TSV, JSON, JSON lines, Parquet) and infer the record contract a dataset project
takes — the four types the origin knows (string, number, integer, boolean) — for ``wtn publish``.

Two passes over the file and nothing held in memory but a JSON array: the first infers each column's type
and whether it is ever empty, the second writes the records as JSON lines typed by the contract."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Callable, Iterator

from .errors import WitanError

FORMATS = ("csv", "tsv", "json", "jsonl", "parquet")
MAX_FIELDS = 40  # the origin's contract allows 40 fields
_EXT = {".csv": "csv", ".tsv": "tsv", ".tab": "tsv", ".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl",
        ".parquet": "parquet", ".pq": "parquet"}
# A CSV cell is an integer or a number only as a plain decimal with no leading zero: "007" and "02134" stay text.
_INT = re.compile(r"-?(0|[1-9]\d*)")
_NUM = re.compile(r"-?((0|[1-9]\d*)(\.\d+)?|\.\d+)([eE][-+]?\d+)?")
_SAFE_INT = 2 ** 53  # past this a JavaScript number loses digits; such a column stays text
_NAME = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,60}")


def detect_format(path: Path, fmt: str | None = None) -> str:
    if fmt:
        if fmt not in FORMATS:
            raise WitanError(f"--format must be one of {', '.join(FORMATS)}")
        return fmt
    found = _EXT.get(path.suffix.lower())
    if not found:
        raise WitanError(f"cannot tell the format of {path.name} from its extension: pass --format ({', '.join(FORMATS)})")
    return found


def slug_from(path: Path) -> str:
    """A project slug from a file name: ``Daily Prices (EU).csv`` → ``daily-prices-eu``."""
    s = re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-")[:60].strip("-")
    if len(s) < 3:
        raise WitanError(f"cannot make a slug from {path.name}: pass --slug (3-60 lowercase letters, digits and dashes)")
    return s


def title_from(path: Path) -> str:
    """A project title from a file name (the origin wants 4-140 characters): ``daily_prices.csv`` → ``daily prices``."""
    title = re.sub(r"[-_\s]+", " ", path.stem).strip()[:140]
    return title if len(title) >= 4 else f"{title or 'records'} dataset"


def field_name(raw: str, taken: set[str]) -> str:
    """A column name as the contract takes it: a letter first, then letters, digits and _, 61 at most."""
    name = re.sub(r"[^a-zA-Z0-9_]+", "_", raw.strip()).strip("_")
    if not name or not name[0].isalpha():
        name = "f_" + name
    name = name[:61]
    base, n = name, 2
    while name in taken:
        suffix = f"_{n}"
        name = base[:61 - len(suffix)] + suffix
        n += 1
    return name


def _kind(v: Any, text: bool) -> str | None:
    """The kind of one value: None (empty), bool, int, num, str or json (an object or array)."""
    if v is None:
        return None
    if text:  # a CSV cell: what the text spells
        if v == "":
            return None
        low = v.lower()
        if low in ("true", "false"):
            return "bool"
        if _INT.fullmatch(v):
            return "int" if abs(int(v)) <= _SAFE_INT else "str"
        if _NUM.fullmatch(v):
            return "num"
        return "str"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int" if abs(v) <= _SAFE_INT else "str"
    if isinstance(v, float):
        return "num" if v == v and v not in (float("inf"), float("-inf")) else "str"
    if isinstance(v, (dict, list)):
        return "json"
    return "str"


def _type_of(kinds: set[str]) -> str:
    if not kinds:
        return "string"  # never filled: optional text
    if kinds == {"bool"}:
        return "boolean"
    if kinds == {"int"}:
        return "integer"
    if kinds <= {"int", "num"}:
        return "number"
    return "string"


@dataclass
class Column:
    source: str          # the name in the file
    name: str            # the name in the contract
    kinds: set[str] = field(default_factory=set)
    empty: int = 0       # rows where it is missing or empty

    @property
    def type(self) -> str:
        return _type_of(self.kinds)


@dataclass
class Inferred:
    path: Path
    format: str
    rows: int
    columns: list[Column]

    @property
    def schema(self) -> dict[str, Any]:
        return {"fields": [{"name": c.name, "type": c.type, "required": c.empty == 0} for c in self.columns],
                "allowExtra": False}

    @property
    def renamed(self) -> dict[str, str]:
        return {c.source: c.name for c in self.columns if c.source != c.name}


def _sniff(fh: IO[str], fmt: str) -> csv.Dialect | type[csv.Dialect]:
    if fmt == "tsv":
        return csv.excel_tab
    sample = fh.read(64 * 1024)
    fh.seek(0)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def _rows(path: Path, fmt: str) -> Callable[[], Iterator[dict[str, Any]]]:
    """A function that yields the file's rows (source name → value) from the start each time it is called."""
    if fmt in ("csv", "tsv"):
        def read_csv() -> Iterator[dict[str, Any]]:
            with path.open(encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh, dialect=_sniff(fh, fmt))
                for row in reader:
                    if None in row:
                        raise WitanError(f"{path.name} line {reader.line_num}: more cells than the header has columns")
                    yield row
        return read_csv
    if fmt == "jsonl":
        def read_jsonl() -> Iterator[dict[str, Any]]:
            with path.open(encoding="utf-8-sig") as fh:
                for n, line in enumerate(fh, 1):
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError as exc:
                        raise WitanError(f"{path.name} line {n}: not JSON ({exc})") from None
                    if not isinstance(rec, dict):
                        raise WitanError(f"{path.name} line {n}: not a JSON object")
                    yield rec
        return read_jsonl
    if fmt == "json":
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            raise WitanError(f"{path.name}: not JSON ({exc})") from None
        if isinstance(data, dict) and isinstance(data.get("records"), list):
            data = data["records"]
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise WitanError(f"{path.name}: expected an array of objects (or {{\"records\": [...]}})")
        return lambda: iter(data)
    return _parquet_rows(path)


def _parquet_rows(path: Path) -> Callable[[], Iterator[dict[str, Any]]]:
    try:
        import duckdb  # type: ignore[import-not-found]
    except ImportError:
        raise WitanError('reading Parquet needs DuckDB: pip install "witan-sdk[query]"') from None

    def read() -> Iterator[dict[str, Any]]:
        con = duckdb.connect()
        try:
            cur = con.execute("SELECT * FROM read_parquet(?)", [str(path)])
            names = [d[0] for d in cur.description]
            while True:
                batch = cur.fetchmany(2048)
                if not batch:
                    return
                for row in batch:
                    yield {k: _plain(v) for k, v in zip(names, row)}
        finally:
            con.close()
    return read


def _plain(v: Any) -> Any:
    """A Parquet value as JSON has it: a decimal is a number, a date or time its ISO text, bytes hex."""
    import datetime
    import decimal
    if isinstance(v, decimal.Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (datetime.date, datetime.datetime, datetime.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    if isinstance(v, (dict, list, str, int, float, bool)) or v is None:
        return v
    return str(v)


class Table:
    """A data file read twice: ``infer()`` once, ``write_jsonl()`` once."""

    def __init__(self, path: "str | Path", fmt: str | None = None):
        self.path = Path(path)
        if not self.path.is_file():
            raise WitanError(f"no such file: {self.path}")
        self.format = detect_format(self.path, fmt)
        self.text = self.format in ("csv", "tsv")
        self._rows = _rows(self.path, self.format)

    def infer(self) -> Inferred:
        cols: dict[str, Column] = {}
        taken: set[str] = set()
        rows = 0
        for row in self._rows():
            for key, value in row.items():
                col = cols.get(key)
                if col is None:
                    col = Column(key, field_name(str(key), taken), empty=rows)  # absent from every earlier row
                    taken.add(col.name)
                    cols[key] = col
                k = _kind(value, self.text)
                if k is None:
                    col.empty += 1
                else:
                    col.kinds.add(k)
            for col in cols.values():
                if col.source not in row:
                    col.empty += 1
            rows += 1
        if rows == 0:
            raise WitanError(f"{self.path.name} has no records")
        return Inferred(self.path, self.format, rows, list(cols.values()))

    def write_jsonl(self, inferred: Inferred, contract: dict[str, Any], out: Path) -> int:
        """Write the records as JSON lines typed by ``contract`` (the project's schemaDef); returns the count.
        A column whose file name maps to a contract field takes that field's type; an empty cell is left out."""
        types = {f["name"]: f["type"] for f in contract.get("fields", [])}
        names = {c.source: c.name for c in inferred.columns}
        n = 0
        with out.open("w", encoding="utf-8") as fh:
            for row in self._rows():
                rec: dict[str, Any] = {}
                for key, value in row.items():
                    if _kind(value, self.text) is None:
                        continue
                    name = names[key]
                    rec[name] = _convert(value, types.get(name) or _type_of({_kind(value, self.text)}), self.text)  # type: ignore[arg-type]
                fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
                n += 1
        return n


def _convert(v: Any, to: str, text: bool) -> Any:
    if to == "string":
        if isinstance(v, str):
            return v
        return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list, bool)) else str(v)
    if not text:
        return v
    if to == "boolean":
        return v.lower() == "true"
    if to == "integer":
        return int(v)
    return int(v) if _INT.fullmatch(v) else float(v)  # number


def fit(inferred: Inferred, contract: dict[str, Any]) -> list[str]:
    """What keeps this file from an existing project's contract, one line each (empty: it fits)."""
    fields = {f["name"]: f for f in contract.get("fields", [])}
    problems: list[str] = []
    cols = {c.name: c for c in inferred.columns}
    for c in inferred.columns:
        f = fields.get(c.name)
        label = c.source if c.source == c.name else f"{c.source} (as {c.name})"
        if f is None:
            if contract.get("allowExtra") is False:
                problems.append(f"column {label} is not in the project's schema")
            continue
        if not c.kinds:
            continue
        want, have = f["type"], c.type
        if want == "string" or want == have or (want == "number" and have == "integer"):
            continue
        problems.append(f"column {label} holds {have} values; the schema wants {want}")
    for name, f in fields.items():
        if f.get("required") is False:
            continue
        c = cols.get(name)
        if c is None:
            problems.append(f"required field {name} is not a column of the file")
        elif c.empty:
            problems.append(f"required field {name} is empty in {c.empty} of {inferred.rows} rows")
    return problems


def check_new(inferred: Inferred) -> None:
    """Refuse, before anything is sent, a file a new project's contract cannot describe."""
    if len(inferred.columns) > MAX_FIELDS:
        raise WitanError(f"{inferred.path.name} has {len(inferred.columns)} columns; a dataset's schema holds at most "
                         f"{MAX_FIELDS} — drop the ones you do not sell and publish again")
    bad = [c.name for c in inferred.columns if not _NAME.fullmatch(c.name)]
    if bad:  # field_name() makes every name fit; this guards the rule against drifting from the origin's
        raise WitanError(f"invalid field names: {', '.join(bad)}")


def readme_draft(inferred: Inferred, source: str) -> str:
    """The README a project starts with when none is given: what is in it, field by field. Edit it later
    with ``update_dataset`` (MCP) or ``PATCH /projects/<slug>``."""
    lines = [f"Records published with `wtn publish` from {inferred.path.name}: {inferred.rows:,} rows, "
             f"{len(inferred.columns)} fields.", "", "Fields:"]
    for c in inferred.columns:
        lines.append(f"- `{c.name}` — {c.type}{'' if c.empty == 0 else ', optional'}")
    lines += ["", f"How it was collected: {source}"]
    return "\n".join(lines)
