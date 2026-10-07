"""SCANBot step 2 data: one cached EDGAR file per symbol, reused under 7 days, refetched after,
and failed closed past 10 days when the refetch fails. Fake fetchers stand in for SEC; no test
reaches the network.
"""

from datetime import UTC, datetime, timedelta

import pytest

from extensions.scanbot import fundamentals as fx
from extensions.scanbot.fundamentals import (
    CACHE,
    FETCHED,
    NO_FACTS,
    STALE_FALLBACK,
    FactsUnavailable,
    Throttle,
    TickerMapUnavailable,
    extract,
    file_path,
    load_fundamentals,
    read_file,
    write_file,
)

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
TICKERS = {"ACME": ("0000000001", "Acme Corp"), "BRK-B": ("0000000002", "Berkshire"), "CON": ("0000000003", "Con Inc")}
RAW = {
    "entityName": "ACME CORP",
    "facts": {
        "us-gaap": {
            "OperatingIncomeLoss": {"units": {"USD": [
                {"start": "2025-01-01", "end": "2025-12-31", "val": 10, "filed": "2026-02-10",
                 "form": "10-K", "fy": 2025, "fp": "FY", "accn": "x", "frame": "CY2025"}]}},
            "SomethingElse": {"units": {"USD": [{"end": "2025-12-31", "val": 1, "filed": "2026-02-10"}]}},
        },
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
            {"end": "2026-01-31", "val": 100, "filed": "2026-02-10", "form": "10-K"}]}}},
    },
}


class Facts:
    def __init__(self, result=RAW, error=None):
        self.result, self.error, self.calls = result, error, []

    def __call__(self, cik):
        self.calls.append(cik)
        if self.error:
            raise self.error
        return self.result


def _no_wait():
    return Throttle(rate=1000, clock=lambda: 0.0, sleep=lambda s: None)


def _seed(folder, symbol, age_days):
    ff = extract(symbol, "0000000001", RAW, NOW - timedelta(days=age_days), "Acme Corp")
    write_file(folder, ff)
    return ff


def _load(folder, symbols, facts, **kw):
    return load_fundamentals(symbols, folder=folder, now=NOW, ticker_map_loader=lambda: TICKERS,
                             facts_fetcher=facts, throttle=_no_wait(), **kw)


def test_extract_keeps_only_cached_tags_and_fields():
    ff = extract("ACME", "0000000001", RAW, NOW)
    assert set(ff.facts["us-gaap"]) == {"OperatingIncomeLoss"}
    assert ff.facts["us-gaap"]["OperatingIncomeLoss"]["USD"][0] == {
        "start": "2025-01-01", "end": "2025-12-31", "val": 10, "filed": "2026-02-10", "form": "10-K"}
    assert ff.facts["dei"]["EntityCommonStockSharesOutstanding"]["shares"][0]["val"] == 100
    assert ff.taxonomies == ["dei", "us-gaap"]
    assert ff.entity_name == "ACME CORP"


def test_missing_file_is_fetched_and_written(tmp_path):
    facts = Facts()
    lookups = _load(tmp_path, ["ACME"], facts)
    assert lookups["ACME"].source == FETCHED
    assert facts.calls == ["0000000001"]
    on_disk = read_file(file_path(tmp_path, "ACME"))
    assert on_disk.fetched_at == NOW and on_disk.cik == "0000000001"


def test_file_under_7_days_is_not_refetched(tmp_path):
    _seed(tmp_path, "ACME", 6)
    facts = Facts()
    lookups = _load(tmp_path, ["ACME"], facts)
    assert lookups["ACME"].source == CACHE
    assert facts.calls == []


def test_file_8_days_old_is_refetched(tmp_path):
    _seed(tmp_path, "ACME", 8)
    facts = Facts()
    lookups = _load(tmp_path, ["ACME"], facts)
    assert lookups["ACME"].source == FETCHED
    assert read_file(file_path(tmp_path, "ACME")).fetched_at == NOW


def test_failed_refetch_uses_file_up_to_10_days(tmp_path):
    _seed(tmp_path, "ACME", 8)
    lookups = _load(tmp_path, ["ACME"], Facts(error=FactsUnavailable("HTTP 503")))
    lookup = lookups["ACME"]
    assert lookup.source == STALE_FALLBACK and lookup.file is not None
    assert "HTTP 503" in lookup.warnings[0]


def test_failed_refetch_of_file_over_10_days_fails_closed(tmp_path):
    _seed(tmp_path, "ACME", 11)
    lookups = _load(tmp_path, ["ACME"], Facts(error=FactsUnavailable("HTTP 503")))
    lookup = lookups["ACME"]
    assert lookup.file is None
    assert "over 10" in lookup.failure and "HTTP 503" in lookup.failure


def test_failed_fetch_without_file_fails_closed(tmp_path):
    lookups = _load(tmp_path, ["ACME"], Facts(error=FactsUnavailable("ReadTimeout")))
    assert lookups["ACME"].file is None
    assert "no cached file" in lookups["ACME"].failure


def test_symbol_missing_from_ticker_map_fails_closed(tmp_path):
    facts = Facts()
    lookups = _load(tmp_path, ["ZZZZ"], facts)
    assert lookups["ZZZZ"].file is None
    assert "not in SEC's ticker map" in lookups["ZZZZ"].failure
    assert facts.calls == []


def test_share_class_dot_maps_to_sec_dash(tmp_path):
    facts = Facts()
    lookups = _load(tmp_path, ["BRK.B"], facts)
    assert facts.calls == ["0000000002"]
    assert lookups["BRK.B"].file.symbol == "BRK.B"


def test_windows_device_names_get_a_safe_file_name(tmp_path):
    assert file_path(tmp_path, "CON").name == "CON_.json"
    assert file_path(tmp_path, "com1").name == "COM1_.json"
    assert file_path(tmp_path, "COLB").name == "COLB.json"
    lookups = _load(tmp_path, ["CON"], Facts())
    assert lookups["CON"].source == FETCHED
    assert read_file(tmp_path / "CON_.json").symbol == "CON"


def test_404_is_cached_as_no_facts(tmp_path):
    lookups = _load(tmp_path, ["ACME"], Facts(result=None))
    assert lookups["ACME"].file.status == NO_FACTS
    assert read_file(file_path(tmp_path, "ACME")).status == NO_FACTS


def test_offline_uses_cache_up_to_10_days_and_never_fetches(tmp_path):
    _seed(tmp_path, "ACME", 9)
    _seed(tmp_path, "OLD", 11)

    def no_map():
        raise AssertionError("offline must not load the ticker map")

    lookups = load_fundamentals(["ACME", "OLD", "NONE"], folder=tmp_path, now=NOW, offline=True,
                                ticker_map_loader=no_map, facts_fetcher=Facts(error=AssertionError()))
    assert lookups["ACME"].file is not None and "offline" in lookups["ACME"].warnings[0]
    assert lookups["OLD"].file is None and "over 10" in lookups["OLD"].failure
    assert lookups["NONE"].file is None and "no cached file" in lookups["NONE"].failure


def test_ticker_map_failure_stops_the_run(tmp_path, monkeypatch):
    def broken(url, name):
        raise fx.VendorUnavailableError("SEC EDGAR request failed (403)")

    monkeypatch.setattr(fx.sec_edgar, "_cached_json", broken)
    with pytest.raises(TickerMapUnavailable):
        load_fundamentals(["ACME"], folder=tmp_path, now=NOW, facts_fetcher=Facts(), throttle=_no_wait())


def test_unreadable_cache_file_is_a_miss(tmp_path):
    file_path(tmp_path, "ACME").write_text("{not json", encoding="utf-8")
    facts = Facts()
    lookups = _load(tmp_path, ["ACME"], facts)
    assert lookups["ACME"].source == FETCHED


class _Response:
    def __init__(self, status=200, chunks=(b'{"facts": {}}',)):
        self.status_code, self.chunks = status, chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size):
        yield from self.chunks


def test_fetch_reads_body_and_maps_404(monkeypatch):
    monkeypatch.setattr(fx.requests, "get", lambda *a, **kw: _Response(chunks=(b'{"facts"', b': {}}')))
    assert fx.fetch_company_facts("0000000001") == {"facts": {}}
    monkeypatch.setattr(fx.requests, "get", lambda *a, **kw: _Response(status=404))
    assert fx.fetch_company_facts("0000000001") is None
    monkeypatch.setattr(fx.requests, "get", lambda *a, **kw: _Response(status=503))
    with pytest.raises(FactsUnavailable, match="HTTP 503"):
        fx.fetch_company_facts("0000000001")


def test_fetch_gives_up_on_a_trickling_download(monkeypatch):
    ticks = iter(range(0, 1000, 50))  # each clock read is 50 seconds later
    monkeypatch.setattr(fx.requests, "get", lambda *a, **kw: _Response(chunks=[b" "] * 10))
    with pytest.raises(FactsUnavailable, match="exceeded 120s"):
        fx.fetch_company_facts("0000000001", clock=lambda: next(ticks))


def test_throttle_spaces_requests():
    clock = [0.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds

    throttle = Throttle(rate=8, clock=lambda: clock[0], sleep=sleep)
    throttle.wait()
    throttle.wait()
    assert slept == [pytest.approx(0.125)]
