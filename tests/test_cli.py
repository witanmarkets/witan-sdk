from __future__ import annotations

import json

import httpx
import pytest

from witan_sdk import Witan
from witan_sdk.cli import main

from test_client import UNIT, Fake


@pytest.fixture
def client() -> Witan:
    return Witan("km_test", base_url="http://api.test", transport=httpx.MockTransport(Fake()))


def test_search_human_and_json(client: Witan, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["search", "redis"], client=client) == 0
    out = capsys.readouterr().out
    assert UNIT in out and "witan-lab" in out
    assert main(["search", "redis", "--semantic", "--json"], client=client) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["similarity"] == "0.91"


def test_read_and_error_exit_code(client: Witan, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["read", UNIT], client=client) == 0
    assert "full text" in capsys.readouterr().out
    assert main(["read", "00000000-0000-0000-0000-000000000000"], client=client) == 1
    assert "error:" in capsys.readouterr().err


def test_submit_from_stdin_and_wait(client: Witan, capsys: pytest.CaptureFixture[str],
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("measured body"))
    assert main(["submit", "--title", "t", "--category", "infra-measurement", "--file", "-",
                 "--source", "own measurement, 2026-09-30", "--wait"], client=client) == 0
    assert "published  new-1" in capsys.readouterr().out


def test_pull_parquet_then_up_to_date(client: Witan, capsys: pytest.CaptureFixture[str], tmp_path) -> None:
    assert main(["pull", "agent-api-observatory@110", "--out", str(tmp_path)], client=client) == 0
    out = capsys.readouterr().out
    assert "3 records in 2 parts" in out and "2 parts downloaded" in out
    assert main(["pull", "agent-api-observatory", "--out", str(tmp_path)], client=client) == 0
    assert "up to date" in capsys.readouterr().out
    assert main(["pull", "agent-api-observatory", "--out", str(tmp_path), "--format", "jsonl"], client=client) == 0
    assert "records.jsonl" in capsys.readouterr().out


def test_projects_and_data_jsonl(client: Witan, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["projects"], client=client) == 0
    assert "agent-api-observatory" in capsys.readouterr().out
    assert main(["data", "agent-api-observatory", "--limit", "1"], client=client) == 0
    line = capsys.readouterr().out.strip().splitlines()[0]
    assert json.loads(line)["ok"] is True


def test_help_survives_a_legacy_code_page() -> None:
    # output redirected on Korean Windows is cp949, which has no em dash: --help used to crash there
    import os
    import subprocess
    import sys

    env = {**os.environ, "PYTHONIOENCODING": "cp949", "PYTHONUTF8": "0"}
    r = subprocess.run([sys.executable, "-c", "from witan_sdk.cli import main; main(['--help'])"],
                       env=env, capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr.decode("cp949", "replace")
    assert b"usage: wtn" in r.stdout


def test_serve_help_does_not_call_the_node_read_only(capsys: pytest.CaptureFixture[str]) -> None:
    # local projects on a node take writes unless --read-only; the help said "(read-only)"
    with pytest.raises(SystemExit) as ei:
        main(["--help"])
    assert ei.value.code == 0
    text = " ".join(capsys.readouterr().out.split())  # argparse wraps the line
    assert "(read-only)" not in text and "local projects take writes" in text, text


def test_listings_human_and_json(client: Witan, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["listings"], client=client) == 0
    out = capsys.readouterr().out
    assert f"unit     {UNIT}  published" in out and "$0.25" in out and "2 sold" in out
    assert "revision v3 u-2 is submitted" in out and "dataset  probe-latency" in out and "(private)" in out
    assert "page 1 of 1 · 1 unit(s), 1 dataset(s)" in out
    assert main(["listings", "redis", "--kind", "dataset", "--json"], client=client) == 0
    assert [r["slug"] for r in json.loads(capsys.readouterr().out)["listings"]] == ["probe-latency"]


def test_earnings_human_and_json(client: Witan, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["earnings"], client=client) == 0
    out = capsys.readouterr().out
    assert "payable   $0.040000 of the $0.050000 threshold (needs $0.010000 more)" in out
    assert "$0.080000 payable from 2026-10-12 09:00 UTC" in out and "waits until payable reaches" in out
    assert main(["earnings", "--json"], client=client) == 0
    assert json.loads(capsys.readouterr().out)["nextPayout"] == "below_threshold"


def test_search_says_price_and_how_it_matched(capsys: pytest.CaptureFixture[str]) -> None:
    hit = {"id": UNIT, "title": "Redis", "category": "databases", "score": "85", "agentName": "witan-lab"}
    answers = {
        "free": {"results": [{**hit, "price": "$0", "priceMicro": 0, "locked": False}], "mode": "keyword"},
        "paid": {"results": [{**hit, "price": "$0.25", "priceMicro": 250000, "locked": True}], "mode": "semantic"},
        "none": {"results": [], "mode": "semantic", "next": {"note": "Nothing published is close.", "mcpTool": "post_request",
                                                              "url": "/community/requests"}},
    }
    client = Witan("km_test", base_url="http://api.test",
                   transport=httpx.MockTransport(lambda req: httpx.Response(200, json=answers[req.url.params["q"]])))
    assert main(["search", "free"], client=client) == 0
    got = capsys.readouterr()
    assert "databases · witan-lab · free" in got.out and "closest by meaning" not in got.err
    assert main(["search", "paid"], client=client) == 0
    got = capsys.readouterr()
    assert "$0.25, buy before reading" in got.out and "closest by meaning" in got.err
    assert main(["search", "none"], client=client) == 0
    got = capsys.readouterr()
    assert "no results" in got.out and "Nothing published is close" in got.err and "post_request" in got.err
    assert main(["search", "free", "--json"], client=client) == 0
    assert json.loads(capsys.readouterr().out)[0]["priceMicro"] == 0  # --json is still the list of results


def test_a_404_says_where_it_was_asked(client: Witan, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["read", "00000000-0000-0000-0000-000000000000"], client=client) == 1
    assert "asked http://api.test" in capsys.readouterr().err


def test_pull_counts_one_part_in_the_singular(capsys: pytest.CaptureFixture[str], tmp_path) -> None:
    from test_client import PART_A, manifest_for

    one = manifest_for(110)
    one["parts"] = one["parts"][:1]
    one["totals"] = {**one["totals"], "records": 2, "parts": 1}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "parts.test":
            return httpx.Response(200, content=PART_A)
        if req.url.path.endswith("/manifest"):
            return httpx.Response(200, json=one)
        return httpx.Response(404, json={"error": "unmapped"})

    client = Witan("km_test", base_url="http://api.test", retries=0, transport=httpx.MockTransport(handler))
    assert main(["pull", "agent-api-observatory@110", "--store", str(tmp_path)], client=client) == 0
    out = capsys.readouterr().out
    assert "2 records in 1 part →" in out and "1 part downloaded" in out
