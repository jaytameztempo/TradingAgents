"""The backtest's pipeline rebuild: the real pipeline code on made-up assets, fundamentals and bars, through stub
fetchers. Nothing touches the network, and nothing is written outside backtest/reconstructed (and the bar cache)."""

import json
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from extensions.backtest import pit
from extensions.backtest import reconstruct as rc
from extensions.backtest.engine import coverage, run_backtest
from extensions.market_data.alpaca_bars import FOUR_HOUR_COLUMNS
from extensions.scanbot import fundamentals as fx
from extensions.scripts import run_backtest_reconstruct as cli

pytestmark = pytest.mark.unit

AS_OF = date(2026, 3, 31)  # a Tuesday
SESSIONS = pd.bdate_range(end=pd.Timestamp("2026-06-30"), periods=380)  # history, then bars after AS_OF


# --- made-up inputs --------------------------------------------------------------------------

def _asset(symbol, name="Acme Corp Common Stock", exchange="NYSE"):
    return {"symbol": symbol, "name": name, "exchange": exchange, "class": "us_equity", "status": "active",
            "tradable": True, "shortable": True, "easy_to_borrow": True}


def _snapshot(home):
    folder = home / "assets"
    folder.mkdir(parents=True)
    assets = [_asset("GOOD"), _asset("BAD"), _asset("SPY", "SPDR S&P 500 ETF Trust", "ARCA"),
              _asset("QQQ", "Invesco QQQ Trust Series 1 ETF", "NASDAQ")]
    (folder / "assets_20260401T000000Z.json").write_text(json.dumps(
        {"snapshot_version": 1, "fetched_at": "2026-04-01T00:00:00+00:00", "source": "test", "count": len(assets),
         "assets": assets}), encoding="utf-8")


def _f(start, end, val, filed, form):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": form}


def _fundamentals(home):
    def line(v):
        return [_f("2025-01-01", "2025-12-31", v, "2026-02-15", "10-K")]
    facts = {"us-gaap": {
        "EarningsPerShareDiluted": {"USD/shares": line(4.0)},
        "OperatingIncomeLoss": {"USD": line(400e6)},
        "NetCashProvidedByUsedInOperatingActivities": {"USD": line(500e6)},
    }}
    fx.write_file(home / "fundamentals", fx.FundamentalsFile(
        "GOOD", "0000000001", "GOOD Inc", datetime(2026, 4, 1, tzinfo=UTC), fx.OK, ["dei", "us-gaap"], facts))


def _series(symbol, closes):
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({"symbol": symbol, "date": SESSIONS[:len(closes)], "open": closes, "high": closes * 1.01,
                         "low": closes * 0.99, "close": closes, "volume": 2_000_000.0, "trade_count": 100.0,
                         "vwap": closes})


def _master(trend: str):
    n = len(SESSIONS)
    i = np.arange(n)
    index = 100 + 0.4 * i if trend == "up" else 100 + 2 * np.sin(i / 3)  # a steady rise, or a flat chop
    frames = [_series("SPY", index), _series("QQQ", index * 1.1), _series("GOOD", 50 + 0.05 * i),
              _series("BAD", 40 - 0.02 * i)]
    return pd.concat(frames, ignore_index=True)


def _no_4h(*_args, **_kwargs):
    return pd.DataFrame(columns=FOUR_HOUR_COLUMNS)


def _context(home, trend):
    _snapshot(home)
    _fundamentals(home)
    return rc.build_context(home, _master(trend), four_hour_fetcher=_no_4h, log=lambda _m: None)


# --- fetching and slicing --------------------------------------------------------------------

def test_the_sliced_fetcher_never_returns_a_bar_after_the_as_of():
    fetch = rc.sliced_fetcher(_master("up"), AS_OF)
    bars = fetch(["SPY", "GOOD"], date(2025, 1, 1), date(2026, 12, 31), feed="sip", cache_dir=None)
    assert bars["date"].max() == pd.Timestamp(AS_OF)
    assert set(bars["symbol"]) == {"SPY", "GOOD"}


def test_ensure_bars_fetches_only_what_the_cache_lacks_and_remembers_attempts(tmp_path):
    calls = []

    def fetcher(symbols, start, end, **kwargs):
        calls.append(list(symbols))
        frame = _master("up")
        return frame[frame["symbol"].isin(symbols) & (frame["date"] >= pd.Timestamp(start))
                     & (frame["date"] <= pd.Timestamp(end))]

    cached = _series("SPY", np.full(len(SESSIONS), 100.0))
    cached.to_csv(tmp_path / "SPY_cached.csv", index=False)
    folder = tmp_path / "cache" / "bars"
    folder.mkdir(parents=True)
    cached.assign(date=cached["date"].dt.date).to_csv(folder / "SPY_2025-01-01_2026-06-30_sip_split.csv", index=False)

    start, end = SESSIONS[0].date(), SESSIONS[-1].date()
    frames = rc.ensure_bars(tmp_path, ["SPY", "GOOD", "NONE"], start, end, fetcher=fetcher)
    assert calls == [["GOOD", "NONE"]]  # SPY is covered by the cache
    assert len(frames["GOOD"]) == len(SESSIONS) and frames["NONE"].empty
    rc.ensure_bars(tmp_path, ["SPY", "GOOD", "NONE"], start, end, fetcher=fetcher)
    assert calls == [["GOOD", "NONE"]]  # NONE was tried for this range; GOOD came back (the log says so)


# --- one rebuilt date ------------------------------------------------------------------------

def test_an_up_day_is_rebuilt_with_the_live_formats_in_its_own_folder(tmp_path):
    ctx = _context(tmp_path, "up")
    entry = rc.reconstruct_date(AS_OF, ctx)
    out = rc.recon_home(tmp_path)
    assert entry["status"] == rc.DONE and entry["label"] == "UP"
    kinds = {f.split("/")[0] if "/" in f else f.split("\\")[0] for f in entry["files"]}
    assert kinds == {"scanbot", "regimes", "baskets", "routes"}
    [route] = (out / "routes").glob("route_ALL_*.json")
    data = json.loads(route.read_text(encoding="utf-8"))
    assert (data["as_of"], data["regime_label"], data["regime_symbol"]) == (str(AS_OF), "UP", "SPY+QQQ")
    assert route.name.endswith(f"{AS_OF}_{AS_OF:%Y%m%d}T210000Z.json")  # stamped after the close on the as-of
    assert pit.known_on(pit.parse_name(route)[1]) == AS_OF
    universe = json.loads(next((out / "scanbot").glob("universe_*.json")).read_text(encoding="utf-8"))
    assert universe["spread_method"] == "not_applied" and "GOOD" in universe["survivors"]
    # nothing in the live folders
    assert not (tmp_path / "routes").exists() and not (tmp_path / "scanbot").exists()


def test_a_side_day_writes_a_side_handoff(tmp_path):
    entry = rc.reconstruct_date(AS_OF, _context(tmp_path, "side"))
    assert entry["label"] == "SIDE" and entry["handoff"] in ("ranked", "unranked")
    assert list((rc.recon_home(tmp_path) / "handoffs").glob("*side_handoff_*.json"))


def test_a_weekday_without_a_spy_bar_is_a_holiday(tmp_path):
    ctx = _context(tmp_path, "up")
    ctx.sessions.discard(AS_OF)
    assert rc.reconstruct_date(AS_OF, ctx)["status"] == rc.HOLIDAY
    ctx.sessions.clear()
    with pytest.raises(rc.ReconstructError):
        rc.reconstruct_date(AS_OF, ctx)


def test_forget_date_removes_only_that_dates_files(tmp_path):
    ctx = _context(tmp_path, "up")
    manifest = rc.read_manifest(tmp_path)
    manifest["dates"][AS_OF.isoformat()] = rc.reconstruct_date(AS_OF, ctx)
    other = AS_OF - timedelta(days=1)
    manifest["dates"][other.isoformat()] = rc.reconstruct_date(other, ctx)
    rc.forget_date(tmp_path, manifest, AS_OF)
    left = [p.name for p in rc.recon_home(tmp_path).rglob("*.json")]
    assert left and all(str(AS_OF) not in name for name in left)
    assert list(manifest["dates"]) == [other.isoformat()]


# --- which dates, and the engine on rebuilt files ---------------------------------------------

def test_pending_dates_skip_saved_and_rebuilt_dates(tmp_path):
    (tmp_path / "routes").mkdir()
    (tmp_path / "routes" / "route_ALL_2026-03-30_20260330T220000Z.json").write_text("{}", encoding="utf-8")
    rc.write_manifest(tmp_path, {"version": 1, "dates": {"2026-03-31": {"status": "done"}}})
    week = (date(2026, 3, 30), date(2026, 4, 3))
    assert rc.pending_dates(tmp_path, *week) == [date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 3)]
    assert date(2026, 3, 31) in rc.pending_dates(tmp_path, *week, rebuild=True)
    assert date(2026, 3, 30) in rc.pending_dates(tmp_path, *week, all_dates=True)
    statuses = [row["status"] for row in coverage(tmp_path, *week)]
    assert statuses == ["saved", "reconstructed", "missing", "missing", "missing"]


def test_rebuilt_trades_are_reported_as_reconstructed(tmp_path):
    ctx = _context(tmp_path, "up")
    manifest = rc.read_manifest(tmp_path)
    manifest["dates"][AS_OF.isoformat()] = rc.reconstruct_date(AS_OF, ctx)
    rc.write_manifest(tmp_path, manifest)
    # Put a name on the rebuilt route so UPBot has something to trade, with cached bars to simulate on.
    out = rc.recon_home(tmp_path)
    [route] = (out / "routes").glob("route_ALL_*.json")
    data = json.loads(route.read_text(encoding="utf-8"))
    [basket] = (out / "baskets").glob("scan-up-category-1_*.json")
    bdata = json.loads(basket.read_text(encoding="utf-8"))
    bdata["members"] = [{"ticker": "GOOD", "score": 1.0, "metrics": {"last_swing_low": 64.0, "atr_14": 1.5}}]
    basket.write_text(json.dumps(bdata), encoding="utf-8")
    data["up_passed"] = data["tradeable"] = ["GOOD"]
    route.write_text(json.dumps(data), encoding="utf-8")
    good = _master("up").query("symbol == 'GOOD'")
    folder = tmp_path / "cache" / "bars"
    folder.mkdir(parents=True)
    good.assign(date=good["date"].dt.date).to_csv(folder / "GOOD_2025-01-01_2026-06-30_sip_split.csv", index=False)

    result = run_backtest(tmp_path, AS_OF, strategy="up")
    [trade] = result["trades"]
    assert (trade["origin"], trade["symbol"], trade["entry_date"]) == ("reconstructed", "GOOD", "2026-04-01")
    rows = {(r["origin"], r["strategy"]): r for r in result["summary"]}
    assert rows[("reconstructed", "up")]["trades"] + rows[("reconstructed", "up")]["open"] == 1
    assert rows[("replay", "all")]["trades"] == 0
    assert run_backtest(tmp_path, AS_OF, strategy="up", include_reconstructed=False)["trades"] == []


# --- the command line --------------------------------------------------------------------------

def test_cli_refuses_bad_ranges(tmp_path, capsys):
    assert cli.main(["--start", "2026-03-05", "--end", "2026-03-01", "--home", str(tmp_path)]) == cli.EXIT_BAD_INPUT
    assert cli.main(["--start", "2026-03-05", "--end", "2999-01-01", "--home", str(tmp_path)]) == cli.EXIT_BAD_INPUT


def test_cli_with_nothing_pending_fetches_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(rc, "ensure_bars", lambda *a, **k: pytest.fail("must not fetch"))
    rc.write_manifest(tmp_path, {"version": 1, "dates": {"2026-03-31": {"status": "done"}}})
    assert cli.main(["--start", "2026-03-31", "--end", "2026-03-31", "--home", str(tmp_path)]) == cli.EXIT_OK
    assert "nothing to rebuild" in capsys.readouterr().out


# --- SEC download of missing fundamentals -------------------------------------------------------

def _bad_facts(_cik):
    """A company-facts document for BAD, with all three figures negative (a WEAK name)."""
    def units(unit, v):
        return {"units": {unit: [_f("2025-01-01", "2025-12-31", v, "2026-02-15", "10-K")]}}
    return {"entityName": "BAD Inc", "facts": {"us-gaap": {
        "EarningsPerShareDiluted": units("USD/shares", -1.0),
        "OperatingIncomeLoss": units("USD", -50e6),
        "NetCashProvidedByUsedInOperatingActivities": units("USD", -40e6),
    }}}


def _sec_context(home, *, facts=_bad_facts, ticker_map=None, fetch=True):
    _snapshot(home)
    _fundamentals(home)  # GOOD is cached; BAD is not
    calls = {"facts": 0, "map": 0}

    def facts_fetcher(cik):
        calls["facts"] += 1
        return facts(cik)

    def map_loader():
        calls["map"] += 1
        if ticker_map is not None:
            return ticker_map()
        return {"GOOD": ("0000000001", "GOOD Inc"), "BAD": ("0000000002", "BAD Inc")}

    ctx = rc.build_context(home, _master("up"), four_hour_fetcher=_no_4h, log=lambda _m: None,
                           fetch_fundamentals=fetch, facts_fetcher=facts_fetcher, ticker_map_loader=map_loader,
                           throttle=fx.Throttle(sleep=lambda _s: None))
    return ctx, calls


def _weak_survivors(home):
    fin = json.loads(next((rc.recon_home(home) / "scanbot").glob("financials_*.json")).read_text(encoding="utf-8"))
    return {p["financial_gate"]: p["survivors"] for p in fin["passes"]}["WEAK"]


def test_with_sec_download_off_nothing_is_downloaded(tmp_path):
    ctx, calls = _sec_context(tmp_path, fetch=False)
    entry = rc.reconstruct_date(AS_OF, ctx)
    assert calls == {"facts": 0, "map": 0}
    assert entry["fundamentals"] == {"fetch": False, "cached": 1, "fetched": 0, "missing": 1}
    assert any("SEC download is off" in w for w in entry["warnings"])
    assert not (tmp_path / "fundamentals" / "BAD.json").exists()


def test_a_missing_file_is_downloaded_saved_and_used(tmp_path):
    ctx, calls = _sec_context(tmp_path)
    entry = rc.reconstruct_date(AS_OF, ctx)
    assert calls == {"facts": 1, "map": 1}  # only BAD; GOOD came from the cache
    assert entry["fundamentals"] == {"fetch": True, "cached": 1, "fetched": 1, "missing": 0}
    assert (tmp_path / "fundamentals" / "BAD.json").exists()  # the shared cache, as the live step writes it
    assert _weak_survivors(tmp_path) == ["BAD"]
    # the next date uses the downloaded file from the cache: no second download
    rc.reconstruct_date(AS_OF - timedelta(days=1), ctx)
    assert calls == {"facts": 1, "map": 1}


def test_a_failed_download_fails_closed_and_is_not_retried_in_the_job(tmp_path):
    def unavailable(_cik):
        raise fx.FactsUnavailable("HTTP 503")
    ctx, calls = _sec_context(tmp_path, facts=unavailable)
    entry = rc.reconstruct_date(AS_OF, ctx)
    assert entry["fundamentals"]["missing"] == 1 and _weak_survivors(tmp_path) == []
    assert any("no SEC file could be downloaded" in w for w in entry["warnings"])
    rc.reconstruct_date(AS_OF - timedelta(days=1), ctx)
    assert calls["facts"] == 1


def test_an_unavailable_ticker_map_falls_back_to_the_cache(tmp_path):
    def no_map():
        raise fx.TickerMapUnavailable("www.sec.gov refused the request")
    ctx, calls = _sec_context(tmp_path, ticker_map=no_map)
    entry = rc.reconstruct_date(AS_OF, ctx)
    assert entry["status"] == rc.DONE
    assert entry["fundamentals"] == {"fetch": True, "cached": 1, "fetched": 0, "missing": 1}
    assert ctx.ticker_map_error and calls["facts"] == 0
    rc.reconstruct_date(AS_OF - timedelta(days=1), ctx)
    assert calls["map"] == 1  # not tried again in the same job


def test_a_later_end_date_fetches_only_the_tail(tmp_path):
    calls = []

    def fetcher(symbols, start, end, **kwargs):
        calls.append((list(symbols), start, end))
        frame = _master("up")
        return frame[frame["symbol"].isin(symbols) & (frame["date"] >= pd.Timestamp(start))
                     & (frame["date"] <= pd.Timestamp(end))]

    start = SESSIONS[0].date()
    first_end, later_end = SESSIONS[-40].date(), SESSIONS[-1].date()
    frames = rc.ensure_bars(tmp_path, ["GOOD"], start, first_end, fetcher=fetcher)
    assert calls == [(["GOOD"], start, first_end)]
    # The cache now ends at first_end (the fetch log says GOOD was fetched back to start).
    folder = tmp_path / "cache" / "bars"
    folder.mkdir(parents=True)
    good = frames["GOOD"]
    good.assign(date=good["date"].dt.date).to_csv(folder / f"GOOD_{start}_{first_end}_sip_split.csv", index=False)

    frames = rc.ensure_bars(tmp_path, ["GOOD"], start, later_end, fetcher=fetcher)
    assert calls[1] == (["GOOD"], first_end + timedelta(days=1), later_end)  # the tail only
    assert frames["GOOD"]["date"].iloc[-1].date() == later_end
    assert frames["GOOD"]["date"].iloc[0].date() == start


def test_four_hour_trade_bars_fetch_only_the_missing_tail(tmp_path):
    calls = []

    def fetcher(symbols, start, end, **kwargs):
        calls.append((list(symbols), start, end))
        return pd.DataFrame(columns=FOUR_HOUR_COLUMNS)

    start, cached_to, end = date(2026, 6, 1), date(2026, 10, 14), date(2026, 11, 13)
    folder = tmp_path / "cache" / "bars"
    folder.mkdir(parents=True)
    days = pd.bdate_range(start, cached_to).date
    pd.DataFrame([("CAVA", f"{d} 09:30:00", str(d), 50, 51, 49, 50, 1, 8) for d in days],
                 columns=FOUR_HOUR_COLUMNS).to_csv(folder / f"CAVA_{start}_{cached_to}_sip_split.4h_rth.csv",
                                                   index=False)
    assert rc.ensure_4h_bars(tmp_path, ["CAVA", "NEW"], start, end, fetcher=fetcher) == 2
    assert sorted(calls) == [(["CAVA"], cached_to + timedelta(days=1), end), (["NEW"], start, end)]
    assert rc.ensure_4h_bars(tmp_path, ["CAVA", "NEW"], start, end, fetcher=fetcher) == 0  # logged: not again
    assert len(calls) == 2


def test_trade_bars_extend_a_cache_that_ends_a_few_sessions_short(tmp_path):
    calls = []

    def fetcher(symbols, start, end, **kwargs):
        calls.append((list(symbols), start, end))
        return pd.DataFrame(columns=FOUR_HOUR_COLUMNS)

    start, cached_to, end = date(2026, 7, 1), date(2026, 11, 10), date(2026, 11, 15)  # a Tuesday; end a Sunday
    folder = tmp_path / "cache" / "bars"
    folder.mkdir(parents=True)
    pd.DataFrame([("BJRI", f"{d} 09:30:00", str(d), 55, 56, 54, 55, 1, 8) for d in pd.bdate_range(start, cached_to).date],
                 columns=FOUR_HOUR_COLUMNS).to_csv(folder / f"BJRI_{start}_{cached_to}_sip_split.4h_rth.csv",
                                                   index=False)
    assert rc.ensure_4h_bars(tmp_path, ["BJRI"], start, end, fetcher=fetcher) == 1
    assert calls == [(["BJRI"], cached_to + timedelta(days=1), end)]
    # one session short of Friday is still fetched; a cache reaching Friday is enough for a Sunday end
    assert rc.trade_slack(date(2026, 11, 15)) == timedelta(days=2) and rc.trade_slack(date(2026, 11, 12)) == timedelta(0)


def test_thirty_minute_bars_fetch_only_the_missing_tail(tmp_path):
    calls = []

    def fetcher(symbols, start, end, **kwargs):
        calls.append((list(symbols), start, end, kwargs["feed"]))
        return pd.DataFrame()

    start, cached_to, end = date(2026, 6, 15), date(2026, 11, 6), date(2026, 11, 13)
    folder = tmp_path / "cache" / "bars"
    folder.mkdir(parents=True)
    stamps = [f"{d} 09:30:00" for d in pd.bdate_range(start, cached_to).date]
    pd.DataFrame({"symbol": "GOOD", "timestamp": stamps, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
                  "trade_count": 1, "vwap": 1}).to_csv(folder / f"GOOD_{start}_{cached_to}_iex_split.30min.csv",
                                                       index=False)
    assert rc.ensure_30min_bars(tmp_path, ["GOOD", "NEW"], start, end, fetcher=fetcher) == 2
    assert sorted(calls) == [(["GOOD"], cached_to + timedelta(days=1), end, "iex"), (["NEW"], start, end, "iex")]
    assert rc.ensure_30min_bars(tmp_path, ["GOOD", "NEW"], start, end, fetcher=fetcher) == 0
