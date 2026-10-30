"""``wtn publish``: a data file becomes a dataset — schema inferred, project made or versioned, records pushed."""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import httpx
import pytest

from witan_sdk import Witan
from witan_sdk.cli import main
from witan_sdk.errors import WitanError
from witan_sdk.infer import Table, field_name, fit, slug_from, title_from


class Origin:
    """Just enough of the origin for publish: a project lookup, create, the upload and the merge."""

    def __init__(self, existing: dict | None = None):
        self.existing = existing
        self.created: dict | None = None
        self.init: dict | None = None
        self.uploaded = b""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "parts.test":
            self.uploaded += request.content
            return httpx.Response(200, headers={"ETag": '"e1"'})
        if request.method == "GET" and path.startswith("/projects/") and path.count("/") == 2:
            if self.existing is None and self.created is None:
                return httpx.Response(404, json={"error": "project not found"})
            return httpx.Response(200, json=self.existing or {"schemaDef": self.created["schemaDef"]})
        if request.method == "POST" and path == "/projects":
            self.created = json.loads(request.content)
            return httpx.Response(201, json={"slug": self.created["slug"], "visibility": "public"})
        if path.endswith("/uploads"):
            self.init = json.loads(request.content)
            return httpx.Response(201, json={"uploadId": "u-1", "parts": [{"n": 1, "url": "http://parts.test/up/1"}],
                                             "expiresAt": "2099-01-01T00:00:00Z"})
        if path.endswith("/uploads/u-1/complete"):
            return httpx.Response(201, json={"contributionId": "c-1", "status": "submitted"})
        if path.endswith("/contributions/c-1"):
            return httpx.Response(200, json={"id": "c-1", "status": "merged", "acceptedCount": 3, "recordCount": 3,
                                             "mergedVersion": 1})
        return httpx.Response(404, json={"error": f"unexpected {request.method} {path}"})

    def records(self) -> list[dict]:
        return [json.loads(line) for line in gzip.decompress(self.uploaded).decode().splitlines()]


def client(origin: Origin) -> Witan:
    return Witan("km_test", base_url="http://api.test", transport=httpx.MockTransport(origin))


CSV = "Ticker,Close Price,Volume,Halted,Zip,Note\nAAPL,227.5,1200,false,02134,\nMSFT,410,900,TRUE,10001,split\nNVDA,1.2e2,15,false,94105,\n"


def test_infer_four_types_and_names(tmp_path: Path) -> None:
    f = tmp_path / "Daily Prices.csv"
    f.write_text(CSV, encoding="utf-8")
    got = Table(f).infer()
    assert got.rows == 3 and got.format == "csv"
    assert got.schema == {"allowExtra": False, "fields": [
        {"name": "Ticker", "type": "string", "required": True},
        {"name": "Close_Price", "type": "number", "required": True},
        {"name": "Volume", "type": "integer", "required": True},
        {"name": "Halted", "type": "boolean", "required": True},
        {"name": "Zip", "type": "string", "required": True},  # a leading zero keeps it text
        {"name": "Note", "type": "string", "required": False},
    ]}
    assert got.renamed == {"Close Price": "Close_Price"}
    assert slug_from(f) == "daily-prices" and title_from(f) == "Daily Prices"


def test_names_fit_the_contract() -> None:
    taken: set[str] = set()
    assert field_name("2024 total", taken) == "f_2024_total"
    taken.add("a_b")
    assert field_name("a-b", taken) == "a_b_2"
    assert len(field_name("x" * 90, set())) == 61


def test_json_jsonl_and_missing_keys(tmp_path: Path) -> None:
    f = tmp_path / "rows.json"
    f.write_text(json.dumps({"records": [{"id": 1, "v": 1.5, "tags": ["a"]}, {"id": 2, "extra": True}]}), encoding="utf-8")
    got = {c["name"]: c for c in Table(f).infer().schema["fields"]}
    assert got["id"] == {"name": "id", "type": "integer", "required": True}
    assert got["v"]["type"] == "number" and got["v"]["required"] is False
    assert got["tags"]["type"] == "string"  # an array goes as JSON text
    assert got["extra"] == {"name": "extra", "type": "boolean", "required": False}  # absent from the first row
    j = tmp_path / "rows.jsonl"
    j.write_text('{"a": "x"}\n\n{"a": "y"}\n', encoding="utf-8")
    assert Table(j).infer().rows == 2
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"a": 1}\n[1]\n', encoding="utf-8")
    with pytest.raises(WitanError, match="line 2: not a JSON object"):
        Table(bad).infer()


def test_too_many_cells_and_no_rows(tmp_path: Path) -> None:
    f = tmp_path / "x.csv"
    f.write_text("a,b\n1,2,3\n", encoding="utf-8")
    with pytest.raises(WitanError, match="more cells than the header"):
        Table(f).infer()
    e = tmp_path / "empty.csv"
    e.write_text("a,b\n", encoding="utf-8")
    with pytest.raises(WitanError, match="no records"):
        Table(e).infer()
    dat = tmp_path / "x.dat"
    dat.write_text("a\n1\n", encoding="utf-8")
    with pytest.raises(WitanError, match="--format"):
        Table(dat)


def test_publish_creates_the_project_and_pushes_typed_records(tmp_path: Path) -> None:
    f = tmp_path / "daily-prices.csv"
    f.write_text(CSV, encoding="utf-8")
    origin = Origin()
    r = client(origin).projects.publish(f, source_declaration="nightly export from our pricing DB")
    assert r["created"] and r["slug"] == "daily-prices" and r["mergedVersion"] == 1
    assert origin.created["schemaDef"]["fields"][1] == {"name": "Close_Price", "type": "number", "required": True}
    assert origin.created["title"] == "daily prices" and "Close_Price" in origin.created["readme"]
    assert origin.init["sourceDeclaration"] == "nightly export from our pricing DB"
    recs = origin.records()
    assert recs[0] == {"Ticker": "AAPL", "Close_Price": 227.5, "Volume": 1200, "Halted": False, "Zip": "02134"}
    assert recs[1]["Halted"] is True and recs[1]["Note"] == "split" and recs[1]["Close_Price"] == 410
    assert recs[2]["Close_Price"] == 120.0


def test_publish_to_an_existing_project_checks_the_contract_first(tmp_path: Path) -> None:
    f = tmp_path / "prices.csv"
    f.write_text("sym,px\nA,1\nB,2\n", encoding="utf-8")
    contract = {"fields": [{"name": "sym", "type": "string"}, {"name": "px", "type": "number"}], "allowExtra": False}
    origin = Origin(existing={"schemaDef": contract})
    r = client(origin).projects.publish(f)
    assert not r["created"] and origin.created is None and origin.records()[0] == {"sym": "A", "px": 1}
    bad = tmp_path / "prices2.csv"
    bad.write_text("sym,px,venue\nA,x,LSE\n", encoding="utf-8")
    origin = Origin(existing={"schemaDef": contract})
    with pytest.raises(WitanError) as exc:
        client(origin).projects.publish(bad, "prices")
    assert "venue is not in the project's schema" in str(exc.value) and "px holds string values" in str(exc.value)
    assert origin.init is None  # nothing sent


def test_fit_required_and_widening() -> None:
    from witan_sdk.infer import Column, Inferred
    inf = Inferred(Path("f.csv"), "csv", 4, [Column("n", "n", {"int"}), Column("s", "s", {"str"}, empty=1)])
    contract = {"fields": [{"name": "n", "type": "number"}, {"name": "s", "type": "string"}, {"name": "t", "type": "string"}]}
    assert fit(inf, contract) == ["required field s is empty in 1 of 4 rows", "required field t is not a column of the file"]


def test_cli_dry_run_sends_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "Daily Prices.csv"
    f.write_text(CSV, encoding="utf-8")
    origin = Origin()
    assert main(["publish", str(f), "--dry-run"], client=client(origin)) == 0
    out = capsys.readouterr().out
    assert "column 'Close Price' → Close_Price" in out and "new project daily-prices" in out and "nothing sent" in out
    assert origin.created is None and origin.init is None


def test_cli_publish_prints_the_version_and_page(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "prices.csv"
    f.write_text("sym,px\nA,1\nB,2\nC,3\n", encoding="utf-8")
    assert main(["publish", str(f), "--private"], client=client(Origin())) == 0
    out = capsys.readouterr().out
    assert "created prices · v1 · 3 new record(s) of 3" in out
    assert "http://api.test/market/datasets/prices" in out and "README is a draft" in out


def test_parquet_types(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    f = tmp_path / "x.parquet"
    duckdb.sql(f"COPY (SELECT 'a' AS s, 1::INT AS i, 2.5::DECIMAL(5,2) AS d, DATE '2026-10-30' AS dt, NULL::BOOLEAN AS b "
               f"UNION ALL SELECT 'b', 2, 3, DATE '2026-10-31', true) TO '{f}' (FORMAT parquet)")
    t = Table(f)
    got = t.infer()
    assert [(c["name"], c["type"], c["required"]) for c in got.schema["fields"]] == [
        ("s", "string", True), ("i", "integer", True), ("d", "number", True), ("dt", "string", True), ("b", "boolean", False)]
    out = tmp_path / "o.jsonl"
    t.write_jsonl(got, got.schema, out)
    assert json.loads(out.read_text().splitlines()[1]) == {"s": "b", "i": 2, "d": 3, "dt": "2026-10-31", "b": True}
