"""Redelivery (pay/src/redeliver.ts): the wallet that paid signs WITAN's redelivery statement, built here, and gets
what it bought again at no charge — a dataset sold with an update period also the versions published within it. And
the 409 of an x402 dataset purchase whose version the wallet already has that way. No network."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from witan_sdk import AlreadyCovered, NotCovered, Witan, WitanError
from witan_sdk.errors import AuthError, ConflictError, NotFoundError, PaymentRequiredError, RateLimitError

eth_account = pytest.importorskip("eth_account")
from eth_account import Account  # noqa: E402
from eth_account.messages import encode_defunct  # noqa: E402

from test_client import PART_A, PART_B, SHA_A, SHA_B, manifest_for  # noqa: E402
from test_purchases import KEY, NOW, TX, WALLET, PayService, offer  # noqa: E402

SLUG = "agent-api-observatory"
UNTIL = "2026-12-14T00:00:00.000Z"
UNIT = {"id": "5e5fc8dd-af67-4f34-839b-b366ef05d43d", "title": "t", "body": "the full unit", "paid": True,
        "redelivered": True, "transaction": TX, "split": None}


def text(t: int = NOW, version: str | None = None, tx: str = TX) -> str:
    """pay/src/redeliver.ts redeliverStatement, written out again here so a drift in the SDK's copy fails a test."""
    return (f"WITAN redeliver\ntransaction: {tx}\nwallet: {WALLET}\norigin: http://pay.test\ntime: {t}"
            + (f"\nversion: {version}" if version else ""))


def dataset(version: int, bought: int = 110) -> dict:
    return {**manifest_for(version), "project": SLUG, "version": version, "boughtVersion": bought, "updatesUntil": UNTIL,
            "paid": True, "redelivered": True, "transaction": TX, "split": "0x" + "33" * 20}


class Redeliver:
    """The pay service's /redeliver routes and the object store; ``answer`` is what POST /redeliver says."""

    def __init__(self, answer: tuple[int, dict], statement=None) -> None:
        self.answer = answer
        self.statement = statement
        self.calls: list[httpx.Request] = []

    def posted(self) -> list[dict]:
        return [json.loads(r.content) for r in self.calls if r.method == "POST"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert "authorization" not in request.headers  # the API key never goes to the pay service
        if request.url.host == "parts.test":
            return httpx.Response(200, content={"/a": PART_A, "/b": PART_B}[request.url.path])
        if request.url.path == "/redeliver/statement":
            v = request.url.params.get("version")
            issued = self.statement(v) if self.statement else text(version=v)
            return httpx.Response(200, json={"statement": issued, "wallet": WALLET, "time": NOW, "expiresIn": 300,
                                             **({"version": v} if v else {})})
        assert request.url.path == "/redeliver" and request.method == "POST"
        status, body = self.answer
        return httpx.Response(status, json=body)


def client(service) -> Witan:
    return Witan("km_test", base_url="http://api.test", pay_url="http://pay.test", transport=httpx.MockTransport(service))


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WITAN_TRUST_FILE", str(tmp_path / "trust" / "trust.json"))
    for k in ("WITAN_VERIFY", "WITAN_WALLET_KEY", "WITAN_MAX_PRICE", "WITAN_X402_NETWORKS", "WITAN_ACCEPT_TERMS"):
        monkeypatch.delenv(k, raising=False)


# ---- statement → signature → POST ---------------------------------------------------------------

def test_redeliver_signs_the_issued_statement_and_posts_it() -> None:
    service = Redeliver((200, UNIT))
    r = client(service).redeliver(TX.upper().replace("0X", "0x"), private_key=KEY)
    assert r["body"] == "the full unit" and r["redelivered"] is True
    asked, posted = service.calls
    assert asked.url.host == "pay.test" and dict(asked.url.params) == {"transaction": TX, "wallet": WALLET}
    body = json.loads(posted.content)
    assert set(body) == {"transaction", "wallet", "time", "signature"}  # no version unless one is asked for
    assert body["transaction"] == TX and body["wallet"] == WALLET and body["time"] == NOW
    assert Account.recover_message(encode_defunct(text=text()), signature=body["signature"]).lower() == WALLET


@pytest.mark.parametrize("asked,named", [("latest", "latest"), (" Latest ", "latest"), (112, "112"), ("112", "112")])
def test_a_version_is_named_in_both_calls_and_in_what_is_signed(asked, named: str) -> None:
    service = Redeliver((200, dataset(112)))
    r = client(service).redeliver(TX, version=asked, private_key=KEY)
    assert r["version"] == 112 and r["boughtVersion"] == 110 and r["updatesUntil"] == UNTIL
    assert service.calls[0].url.params["version"] == named
    [body] = service.posted()
    assert body["version"] == named
    signer = Account.recover_message(encode_defunct(text=text(version=named)), signature=body["signature"])
    assert signer.lower() == WALLET  # the signature names the version: it is good for no other


@pytest.mark.parametrize("issued", [
    lambda v: text(),                      # the version dropped: a signature for the version bought
    lambda v: text(version="999"),         # another version
    lambda v: text(version=v, tx="0x" + "cd" * 32),  # another purchase
])
def test_a_statement_for_another_version_or_purchase_is_not_signed(issued) -> None:
    service = Redeliver((200, UNIT), statement=issued)
    with pytest.raises(WitanError, match="something other"):
        client(service).redeliver(TX, version="latest", private_key=KEY)
    assert service.posted() == []


def test_a_version_outside_the_period_raises_not_covered_with_the_route_that_sells_it() -> None:
    refused = {"error": f"v120 was published after this purchase's update period ended on {UNTIL} — nothing was charged",
               "version": 120, "boughtVersion": 110, "updatesUntil": UNTIL, "buy": f"/paid/dataset?slug={SLUG}&version=120"}
    with pytest.raises(NotCovered, match="update period ended") as raised:
        client(Redeliver((403, refused))).redeliver(TX, version=120, private_key=KEY)
    err = raised.value
    assert isinstance(err, AuthError) and err.status == 403 and err.body == refused
    assert err.buy == refused["buy"] and err.version == 120 and err.bought_version == 110 and err.updates_until == UNTIL


@pytest.mark.parametrize("status,body,cls,match", [
    (401, {"error": "the signed statement has expired — ask for a new one", "how": "..."}, AuthError, "expired"),
    (404, {"error": "no settled purchase by this wallet with that transaction"}, NotFoundError, "no settled purchase"),
    (404, {"error": "split payments are not switched on: every sale is paid to the platform wallet", "splits": False},
     NotFoundError, "not switched on"),
    (429, {"error": "too many redeliveries from this address — try again later"}, RateLimitError, "too many"),
    (400, {"error": "version is for a dataset purchase; this transaction bought a knowledge unit"}, WitanError, "bought a knowledge unit"),
])
def test_the_services_refusals_raise_typed_errors(status: int, body: dict, cls: type, match: str) -> None:
    with pytest.raises(cls, match=match) as raised:
        client(Redeliver((status, body))).redeliver(TX, version=3 if status == 400 else None, private_key=KEY)
    assert raised.value.status == status and not isinstance(raised.value, NotCovered)


@pytest.mark.parametrize("tx,version,match", [
    ("0x1234", None, "settlement tx hash"),
    (TX, 0, "positive integer"),
    (TX, "newest", "positive integer"),
    (TX, True, "positive integer"),
])
def test_a_bad_transaction_or_version_is_refused_before_any_request(tx: str, version, match: str) -> None:
    service = Redeliver((200, UNIT))
    with pytest.raises(WitanError, match=match):
        client(service).redeliver(tx, version=version, private_key=KEY)
    assert service.calls == []


def test_no_wallet_key_is_an_error_before_any_request() -> None:
    service = Redeliver((200, UNIT))
    with pytest.raises(PaymentRequiredError, match="no wallet key"):
        client(service).redeliver(TX)
    assert service.calls == []


# ---- laid out on disk ---------------------------------------------------------------------------

def test_pull_redelivered_lays_out_a_dataset_and_keeps_the_delivery_off_disk(tmp_path: Path) -> None:
    service = Redeliver((200, dataset(112)))
    m = client(service).projects.pull_redelivered(TX, tmp_path, version="latest", private_key=KEY)
    assert m["version"] == 112 and m["downloaded"] == 2 and m["redelivered"] is True and m["transaction"] == TX
    assert m["boughtVersion"] == 110 and m["updatesUntil"] == UNTIL
    parts = tmp_path / SLUG / "parts"
    assert (parts / f"{SHA_A}.parquet").read_bytes() == PART_A and (parts / f"{SHA_B}.parquet").read_bytes() == PART_B
    on_disk = json.loads((tmp_path / SLUG / "v112" / "manifest.json").read_text(encoding="utf-8"))
    assert not {"redelivered", "transaction", "split", "boughtVersion", "updatesUntil", "paid"} & set(on_disk)


def test_pull_redelivered_returns_a_unit_as_redeliver_does(tmp_path: Path) -> None:
    assert client(Redeliver((200, UNIT))).projects.pull_redelivered(TX, tmp_path, private_key=KEY)["body"] == "the full unit"
    assert list(tmp_path.iterdir()) == []


def test_a_signed_paid_or_redelivered_manifest_verifies_with_the_delivery_fields(tmp_path: Path) -> None:
    from test_trust import ORIGIN, keys_doc, signed
    from witan_sdk import trust

    trust.add(ORIGIN, keys_doc()["keys"])
    published = {k: v for k, v in manifest_for(110).items()}
    published["project"] = SLUG
    signed_m = signed(published)
    delivery = {"updatesUntil": UNTIL, "contract": {"seller": {"listed_name": "s"}}, "receipt_url": "https://x/receipts/r"}
    receipt = {"success": True, "transaction": TX, "network": "eip155:84532", "payer": WALLET}
    w = client(Redeliver((200, {**signed_m, **delivery, "boughtVersion": 110, "redelivered": True, "transaction": TX,
                                "split": None, "paid": True})))
    w.buy_dataset = lambda slug, **kw: {**signed_m, **delivery, "paid": True, "x402": receipt}  # type: ignore[method-assign]
    m = w.projects.pull_paid(SLUG, tmp_path / "paid", verify=True, private_key=KEY)
    assert m["verified"] == "verified" and m["updatesUntil"] == UNTIL and m["receipt_url"] and m["x402"] == receipt
    on_disk = json.loads((tmp_path / "paid" / SLUG / "v110" / "manifest.json").read_text(encoding="utf-8"))
    assert not {"updatesUntil", "contract", "receipt_url", "x402"} & set(on_disk)
    assert trust.check(on_disk, require=True)["status"] == "verified"
    again = w.projects.pull_redelivered(TX, tmp_path / "again", verify=True, private_key=KEY)
    assert again["verified"] == "verified" and again["redelivered"] is True


# ---- the 409 of an x402 dataset purchase ---------------------------------------------------------

class CoveredService(PayService):
    """A pay service whose paid /paid/dataset answers 409: the wallet has the version through an update period."""

    def __init__(self) -> None:
        super().__init__(offer())
        self.redeliver = Redeliver((200, dataset(112)))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/redeliver") or request.url.host == "parts.test":
            self.requests.append(request)
            return self.redeliver(request)
        if "payment-signature" in request.headers:
            self.requests.append(request)
            return httpx.Response(409, json={
                "error": "this wallet already has v112 of this dataset, through the update period of an earlier purchase: "
                         "nothing was charged — get it at no charge with POST /redeliver {version: 112}",
                "transaction": TX, "version": 112, "updatesUntil": UNTIL,
                "redeliver": f"GET /redeliver/statement?transaction={TX}&wallet={WALLET}&version=112, sign it ...",
            })
        return super().__call__(request)


@pytest.fixture
def x402() -> None:
    pytest.importorskip("x402")


def test_buy_dataset_of_a_covered_version_raises_already_covered(x402: None) -> None:
    service = CoveredService()
    with pytest.raises(AlreadyCovered, match="nothing was charged") as raised:
        client(service).buy_dataset(SLUG, private_key=KEY)
    err = raised.value
    assert isinstance(err, ConflictError) and err.status == 409
    assert err.transaction == TX and err.version == 112 and err.updates_until == UNTIL
    assert len(service.signed) == 1  # the one payment, which the service cancelled


def test_a_plain_409_of_a_purchase_stays_a_witan_error(x402: None) -> None:
    class Busy(PayService):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if "payment-signature" in request.headers:
                return httpx.Response(409, json={"error": "this payment authorization is already in use — sign a new one"})
            return super().__call__(request)

    with pytest.raises(WitanError, match="already in use") as raised:
        client(Busy(offer())).buy_dataset(SLUG, private_key=KEY)
    assert not isinstance(raised.value, AlreadyCovered) and raised.value.status == 409


def test_pull_paid_redelivers_a_covered_version_instead_of_paying(x402: None, tmp_path: Path) -> None:
    service = CoveredService()
    m = client(service).projects.pull_paid(SLUG, tmp_path, private_key=KEY)
    assert m["version"] == 112 and m["redelivered"] is True and m["transaction"] == TX and m["downloaded"] == 2
    [body] = service.redeliver.posted()
    assert body["version"] == "112"
    assert (tmp_path / SLUG / "v112" / "manifest.json").is_file()


def test_the_cli_pull_says_nothing_was_charged(x402: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture) -> None:
    from witan_sdk.cli import main

    monkeypatch.setenv("WITAN_WALLET_KEY", KEY)
    assert main(["pull", SLUG, "--paid", "--out", str(tmp_path)], client=client(CoveredService())) == 0
    err = capsys.readouterr().err
    assert "already yours" in err and "nothing charged" in err and TX in err


# ---- wtn redeliver ------------------------------------------------------------------------------

def test_wtn_redeliver_downloads_a_dataset_and_prints_a_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                            capsys: pytest.CaptureFixture) -> None:
    from witan_sdk.cli import main

    monkeypatch.setenv("WITAN_WALLET_KEY", KEY)
    service = Redeliver((200, dataset(112)))
    assert main(["redeliver", TX, "--version", "latest", "--out", str(tmp_path)], client=client(service)) == 0
    out = capsys.readouterr().out
    assert f"{SLUG} v112 (bought v110)" in out and "nothing charged" in out and "updates until 2026-12-14" in out
    assert service.posted()[0]["version"] == "latest" and (tmp_path / SLUG / "parts" / f"{SHA_A}.parquet").is_file()
    assert main(["redeliver", TX], client=client(Redeliver((200, UNIT)))) == 0
    assert "the full unit" in capsys.readouterr().out
    assert main(["redeliver", TX, "--json"], client=client(Redeliver((200, UNIT)))) == 0
    assert json.loads(capsys.readouterr().out)["redelivered"] is True


def test_wtn_redeliver_errors(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    from witan_sdk.cli import main

    assert main(["redeliver", TX], client=client(Redeliver((200, UNIT)))) == 1
    assert "no wallet key: pass private_key= or set WITAN_WALLET_KEY" in capsys.readouterr().err  # as wtn purchases says
    monkeypatch.setenv("WITAN_WALLET_KEY", KEY)
    refused = {"error": "this purchase has no update period — nothing was charged", "version": 111, "boughtVersion": 110,
               "updatesUntil": None, "buy": f"/paid/dataset?slug={SLUG}&version=111"}
    assert main(["redeliver", TX, "--version", "111"], client=client(Redeliver((403, refused)))) == 1
    assert "no update period" in capsys.readouterr().err
    with pytest.raises(SystemExit) as usage:
        main(["redeliver", TX, "--version", "newest"])
    assert usage.value.code == 2
