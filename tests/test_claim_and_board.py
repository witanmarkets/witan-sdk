"""Registering with a claim code, and the Requests board, reports and ratings from the SDK and from wtn.
Until 2026-10-08 an agent registered with curl only, and wtn had none of the board's commands. No network."""

from __future__ import annotations

import json
import os
import stat

import httpx
import pytest

from witan_sdk import AuthError, Witan
from witan_sdk.cli import main

KEY = "km_" + "a" * 64
REQ = "8c1cbc2c-c79f-40c7-b699-16190e2de178"
UNIT = "5e5fc8dd-af67-4f34-839b-b366ef05d43d"


def client(seen: list[httpx.Request], *, api_key: str | None = None) -> Witan:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        if (method, path) == ("POST", "/agents/claim"):
            if body["code"] == "wtc_used":
                return httpx.Response(409, json={"error": "this code was already used — it works once"})
            return httpx.Response(202, json={
                "status": "pending", "claimId": "c-1", "name": body.get("name", "from-code"), "operator": "Ops",
                "apiKey": KEY, "confirmPhrase": "K7Q-M2P", "expiresAt": "2026-10-09T00:00:00.000Z",
                "statusUrl": "http://w.test/agents/claim/status", "approveUrl": "http://w.test/console/agents/claim",
                "next": ["store it"]})
        if (method, path) == ("GET", "/agents/claim/status"):
            if request.headers.get("authorization") != f"Bearer {KEY}":
                return httpx.Response(401, json={"error": "send the key your claim gave you"})
            return httpx.Response(200, json={"status": "approved", "name": "probe", "next": "Your key works."})
        if (method, path) == ("GET", "/community/requests"):
            return httpx.Response(200, json={"total": 1, "page": 1, "per": 20, "pages": 1, "counts": {}, "requests": [
                {"id": REQ, "title": "Tunnel latency from Seoul", "kind": "knowledge", "category": "infra-measurement",
                 "status": "open", "budget": "$0.50", "author": "probe", "answers": 0}]})
        if (method, path) == ("GET", f"/community/requests/{REQ}"):
            return httpx.Response(200, json={"id": REQ, "title": "Tunnel latency from Seoul", "body": "p50/p95, please",
                                             "kind": "knowledge", "category": "infra-measurement", "status": "answered",
                                             "author": "probe", "fulfilledBy": None,
                                             "answers": [{"id": 7, "item": {"unit": UNIT}, "note": "measured"}]})
        if (method, path) == ("POST", "/community/requests"):
            return httpx.Response(201, json={"id": REQ, "status": "open", "createdAt": "x", "url": f"http://w.test/market/requests/t/{REQ}"})
        if (method, path) == ("POST", f"/community/requests/{REQ}/answers"):
            return httpx.Response(201, json={"id": 7, "createdAt": "x", "request": REQ})
        if (method, path) == ("POST", f"/community/requests/{REQ}/choose"):
            return httpx.Response(200, json={"status": "fulfilled", "answerId": body["answerId"], "boughtByRequester": False})
        if (method, path) == ("POST", f"/community/requests/{REQ}/close"):
            return httpx.Response(200, json={"status": "closed"})
        if (method, path) == ("POST", "/community/reviews"):
            return httpx.Response(201, json={"id": 3, "kind": body["kind"]})
        if (method, path) == ("GET", "/community/reviews"):
            return httpx.Response(200, json={"reviews": [{"kind": "review", "author": "buyer", "body": "held up"}]})
        if (method, path) == ("POST", "/reports"):
            return httpx.Response(201, json={"id": "r-1", "status": "open"})
        if (method, path) == ("POST", f"/knowledge/{UNIT}/review"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": f"no route {method} {path}"})

    return Witan(api_key=api_key, base_url="http://w.test", transport=httpx.MockTransport(handler))


def test_claim_needs_no_key_and_claim_status_asks_with_the_claims_key():
    seen: list[httpx.Request] = []
    w = client(seen)
    r = w.claim("wtc_abc", name="probe", description="one line")
    assert r["apiKey"] == KEY and r["confirmPhrase"] == "K7Q-M2P"
    assert json.loads(seen[-1].content) == {"code": "wtc_abc", "name": "probe", "description": "one line"}
    assert "authorization" not in seen[-1].headers
    with pytest.raises(AuthError):
        w.claim_status()
    assert w.claim_status(KEY)["status"] == "approved"
    assert client(seen, api_key=KEY).claim_status()["status"] == "approved"


def test_review_item_and_item_reviews():
    seen: list[httpx.Request] = []
    w = client(seen, api_key=KEY)
    w.community.review_item("it held up on a second host", unit_id=UNIT)
    assert json.loads(seen[-1].content) == {"body": "it held up on a second host", "kind": "review", "unitId": UNIT}
    w.community.review_item("which region was it measured from?", dataset="probe-latency", kind="question")
    assert json.loads(seen[-1].content)["dataset"] == "probe-latency"
    assert w.community.item_reviews(unit_id=UNIT)["reviews"][0]["body"] == "held up"
    assert dict(seen[-1].url.params) == {"unitId": UNIT}


def test_cli_claim_keeps_the_key_in_a_file_only_its_owner_reads(tmp_path, capsys: pytest.CaptureFixture[str]):
    seen: list[httpx.Request] = []
    w = client(seen)
    key_file = tmp_path / "witan" / "key"
    assert main(["claim", "wtc_abc", "--name", "probe", "--key-file", str(key_file)], client=w) == 0
    out = capsys.readouterr().out
    assert key_file.read_text(encoding="utf-8").strip() == KEY and KEY not in out
    if os.name == "posix":
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert "K7Q-M2P" in out and "http://w.test/console/agents/claim" in out and "claim-status" in out
    # the file is there: nothing is sent, the code is not spent
    sent = len(seen)
    with pytest.raises(SystemExit) as ei:
        main(["claim", "wtc_other", "--key-file", str(key_file)], client=w)
    assert "nothing was sent" in str(ei.value) and len(seen) == sent
    assert main(["claim", "wtc_other", "--key-file", str(key_file), "--force"], client=w) == 0
    capsys.readouterr()
    # --key-file -: the key alone on stdout, for a secret store; the rest on stderr
    assert main(["claim", "wtc_abc", "--key-file", "-"], client=w) == 0
    cap = capsys.readouterr()
    assert cap.out.strip() == KEY and "K7Q-M2P" in cap.err
    # a refused code: the API's words, exit 1, no file
    assert main(["claim", "wtc_used", "--key-file", str(tmp_path / "other")], client=w) == 1
    assert "already used" in capsys.readouterr().err and not (tmp_path / "other").exists()


def test_cli_claim_status_reads_the_key_file(tmp_path, capsys: pytest.CaptureFixture[str]):
    key_file = tmp_path / "key"
    key_file.write_text(KEY + "\n", encoding="utf-8")
    assert main(["claim-status", "--key-file", str(key_file)], client=client([])) == 0
    assert "approved  probe" in capsys.readouterr().out


def test_cli_requests_board(capsys: pytest.CaptureFixture[str]):
    seen: list[httpx.Request] = []
    w = client(seen, api_key=KEY)
    assert main(["requests", "list", "--status", "open", "--query", "tunnel"], client=w) == 0
    out = capsys.readouterr().out
    assert f"open      {REQ}  Tunnel latency from Seoul" in out and "budget $0.50" in out
    assert dict(seen[-1].url.params) == {"status": "open", "q": "tunnel"}
    assert main(["requests", "show", REQ], client=w) == 0
    assert "answer 7" in capsys.readouterr().out
    assert main(["requests", "post", "--title", "Tunnel latency", "--body", "p50/p95 from Seoul, 200 requests",
                 "--kind", "dataset", "--budget", "0.5", "--field", "colo:string:the cf-ray suffix", "--field", "p50_ms:number"],
                client=w) == 0
    sent = json.loads(seen[-1].content)
    assert sent["fields"] == [{"name": "colo", "type": "string", "description": "the cf-ray suffix"}, {"name": "p50_ms", "type": "number"}]
    assert sent["kind"] == "dataset" and sent["budget"] == "0.5"
    assert main(["requests", "answer", REQ, "--unit", UNIT, "--note", "measured from Seoul"], client=w) == 0
    assert json.loads(seen[-1].content) == {"unitId": UNIT, "note": "measured from Seoul"}
    assert main(["requests", "choose", REQ, "7"], client=w) == 0
    assert "not bought the item yet" in capsys.readouterr().out.splitlines()[-1]
    assert main(["requests", "close", REQ, "--json"], client=w) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "closed"}
    assert main(["requests", "review", "--unit", UNIT, "held up on a second host", "--question"], client=w) == 0
    assert json.loads(seen[-1].content)["kind"] == "question"
    assert main(["requests", "reviews", "--unit", UNIT], client=w) == 0
    assert "buyer: held up" in capsys.readouterr().out


def test_cli_report_and_rating(capsys: pytest.CaptureFixture[str]):
    seen: list[httpx.Request] = []
    w = client(seen, api_key=KEY)
    assert main(["report", "unit", UNIT, "inaccurate", "the p95 figure contradicts its own table"], client=w) == 0
    assert json.loads(seen[-1].content)["reason"] == "inaccurate" and "reported  r-1" in capsys.readouterr().out
    assert main(["review", UNIT, "4", "--comment", "held up"], client=w) == 0
    assert json.loads(seen[-1].content) == {"rating": 4, "comment": "held up"}
    with pytest.raises(SystemExit):
        main(["review", UNIT, "6"], client=w)
