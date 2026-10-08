"""Daily status for one date: the market label, which bots are allowed, and the confirmed SIDE names.

It reads three saved files and nothing else, each the newest for the date by
the ``created_at`` inside it:

- the SPY+QQQ route decision (``regime_symbol`` "SPY+QQQ"; older SPY-only
  routes in the same folder are ignored),
- the ranked side handoff,
- the side 4-hour confirmation.

UPBot and DOWNBot are allowed exactly as the route records them; SIDEBot is
allowed only when the market label is SIDE. The confirmed names are the
confirmation's ``confirmed`` long and short names. They are listed only when
the label is SIDE and the three files chain together: the handoff names this
route, the confirmation names this handoff, and every confirmed name is planned
in it. Otherwise every channel name is blocked and each reason is recorded. A
missing route stops the run; a missing handoff or confirmation blocks the
channel names.

Every file is read with plain ``json``: like SIDEBot, nothing here imports
``extensions.router`` or ``extensions.scanbot``, because those load the Alpaca
SDK. It fetches no bars, calls no Alpaca client and places no orders. The
scans, ranker, router and bots are not touched.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

STATUS_VERSION = 1
# The saved versions this reader understands; they match strategy_router.SCHEMA_VERSION
# and side_confirm_4h.REPORT_VERSION.
ROUTE_SCHEMA_VERSION = 1
CONFIRM_REPORT_VERSION = 1
MARKET = "SPY+QQQ"
UP, DOWN, SIDE = "UP", "DOWN", "SIDE"
LABELS = (UP, DOWN, SIDE)
LONG, SHORT = "long", "short"
SIDES = (LONG, SHORT)
UP_BOT, DOWN_BOT, SIDE_BOT = "UPBot", "DOWNBot", "SIDEBot"

Found = tuple[Path, dict]


class StatusInputError(RuntimeError):
    """No SPY+QQQ route for the date, or a saved file that cannot be read or does not make sense."""


def default_route_dir() -> Path:
    return Path.home() / ".tradingagents" / "routes"


def default_handoff_dir() -> Path:
    return Path.home() / ".tradingagents" / "handoffs"


def default_report_dir() -> Path:
    return Path.home() / ".tradingagents" / "scanbot"


def default_status_dir() -> Path:
    """~/.tradingagents/status, beside the routes and reports and out of git."""
    return Path.home() / ".tradingagents" / "status"


def _newest(folder: Path, pattern: str, as_of: date, kind: str, keep=None) -> Found | None:
    """The newest file matching pattern for as_of, by the created_at inside it, or None.

    One unreadable file stops the run: skipping it could quietly pick an older
    file than the one that was meant to count.
    """
    found = []
    for path in sorted(folder.glob(pattern)):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            created = datetime.fromisoformat(data["created_at"])
            file_as_of = data["as_of"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StatusInputError(f"cannot read {kind} {path}: {exc}") from exc
        if file_as_of == as_of.isoformat() and (keep is None or keep(data)):
            found.append((created, path, data))
    if not found:
        return None
    _, path, data = max(found, key=lambda item: item[0])
    return path, data


def latest_route(as_of: date, route_dir: str | Path | None = None) -> Found:
    folder = Path(route_dir) if route_dir is not None else default_route_dir()
    found = _newest(folder, f"route_*_{as_of}_*.json", as_of, "route file",
                    keep=lambda d: d.get("regime_symbol") == MARKET)
    if found is None:
        raise StatusInputError(f"no {MARKET} route decision for {as_of} in {folder}")
    _check_route(*found)
    return found


def latest_ranked_side_handoff(as_of: date, handoff_dir: str | Path | None = None) -> Found | None:
    folder = Path(handoff_dir) if handoff_dir is not None else default_handoff_dir()
    return _newest(folder, f"ranked_side_handoff_{as_of}_*.json", as_of, "ranked side handoff")


def latest_side_confirm_4h(as_of: date, report_dir: str | Path | None = None) -> Found | None:
    folder = Path(report_dir) if report_dir is not None else default_report_dir()
    return _newest(folder, f"side_confirm4h_{as_of}_*.json", as_of, "side 4-hour confirmation")


def _check_route(path: Path, route: dict) -> None:
    if route.get("schema_version") != ROUTE_SCHEMA_VERSION:
        raise StatusInputError(f"route {path} has unsupported schema_version {route.get('schema_version')!r}")
    label = route.get("regime_label")
    if label not in LABELS:
        raise StatusInputError(f"route {path} has unknown market label {label!r}")
    up, down = route.get("up_allowed") is True, route.get("down_allowed", False) is True
    if up != (label == UP) or down != (label == DOWN):
        raise StatusInputError(f"route {path} is {label} but records up_allowed={up}, down_allowed={down}")


def _confirmed_on_file(path: Path, confirm: dict) -> dict[str, list[str]]:
    if confirm.get("report_version") != CONFIRM_REPORT_VERSION:
        raise StatusInputError(f"side 4-hour confirmation {path} has unsupported report_version "
                               f"{confirm.get('report_version')!r}")
    confirmed = confirm.get("confirmed")
    if not isinstance(confirmed, dict) or not all(isinstance(confirmed.get(side), list) for side in SIDES):
        raise StatusInputError(f"side 4-hour confirmation {path} has no confirmed long and short lists")
    return {side: list(confirmed[side]) for side in SIDES}


def _same_file(recorded: str | None, path: Path) -> bool:
    """Compare by file name: the names carry the as_of and created_at stamp, the folders may differ."""
    return bool(recorded) and Path(recorded).name == path.name


@dataclass(frozen=True)
class DailyStatus:
    as_of: date
    created_at: datetime
    market_label: str
    regime_symbol: str
    regime_labels: dict[str, str | None]
    route_reason: str
    bots: dict[str, bool]  # UPBot, DOWNBot, SIDEBot -> allowed
    trend_names: list[str]  # the route's tradeable names for the allowed trend bot; empty under SIDE
    channel_names_blocked: bool
    confirmed: dict[str, list[str]]  # long, short; empty when blocked
    blocked_reasons: list[str]
    sources: dict[str, str | None]
    warnings: list[str] = field(default_factory=list)
    orders_placed: bool = False
    status_version: int = STATUS_VERSION

    def to_dict(self) -> dict:
        data = asdict(self)
        data["as_of"] = self.as_of.isoformat()
        data["created_at"] = self.created_at.isoformat()
        return data


def build_status(
    as_of: date,
    route: Found,
    handoff: Found | None = None,
    confirm: Found | None = None,
    created_at: datetime | None = None,
) -> DailyStatus:
    """Decide which bots are allowed and which channel names are confirmed. Writes nothing."""
    route_path, r = route
    _check_route(route_path, r)
    label = r["regime_label"]
    reasons: list[str] = []
    if label != SIDE:
        reasons.append(f"market label is {label}, not SIDE; channel names are blocked")

    if handoff is None:
        reasons.append(f"no ranked side handoff for {as_of}")
    else:
        h_path, h = handoff
        if not _same_file(h.get("route_file"), route_path):
            reasons.append(f"the ranked side handoff was built from route {h.get('route_file')}, not {route_path}")
        if h.get("regime_label") != label:
            reasons.append(f"the ranked side handoff records label {h.get('regime_label')}, the route {label}")

    confirmed = {side: [] for side in SIDES}
    if confirm is None:
        reasons.append(f"no side 4-hour confirmation for {as_of}")
    else:
        c_path, c = confirm
        confirmed = _confirmed_on_file(c_path, c)
        if handoff is not None:
            if not _same_file(c.get("ranked_handoff"), h_path) or c.get("ranked_handoff_created_at") != h.get("created_at"):
                reasons.append(f"the side 4-hour confirmation checked {c.get('ranked_handoff')}, "
                               f"not the newest ranked side handoff {h_path}")
            else:
                for side in SIDES:
                    unplanned = [s for s in confirmed[side] if s not in h.get(f"planned_{side}", [])]
                    if unplanned:
                        reasons.append(f"confirmed {side} names not planned in the ranked side handoff: "
                                       f"{', '.join(unplanned)}")

    blocked = bool(reasons)
    warnings = [w for found in (handoff, confirm) if found is not None for w in found[1].get("warnings", [])]
    return DailyStatus(
        as_of=as_of,
        created_at=created_at or datetime.now(UTC),
        market_label=label,
        regime_symbol=r["regime_symbol"],
        regime_labels=dict(r.get("regime_labels", {})),
        route_reason=r.get("reason", ""),
        bots={UP_BOT: label == UP, DOWN_BOT: label == DOWN, SIDE_BOT: label == SIDE},
        trend_names=list(r.get("tradeable", [])) if label in (UP, DOWN) else [],
        channel_names_blocked=blocked,
        confirmed={side: [] for side in SIDES} if blocked else confirmed,
        blocked_reasons=reasons,
        sources={
            "route": str(route_path),
            "ranked_side_handoff": str(handoff[0]) if handoff is not None else None,
            "side_confirm4h": str(confirm[0]) if confirm is not None else None,
        },
        warnings=list(dict.fromkeys(warnings)),
    )


def status_for_date(
    as_of: date,
    route_dir: str | Path | None = None,
    handoff_dir: str | Path | None = None,
    report_dir: str | Path | None = None,
) -> DailyStatus:
    route = latest_route(as_of, route_dir)
    handoff = latest_ranked_side_handoff(as_of, handoff_dir)
    confirm = latest_side_confirm_4h(as_of, report_dir)
    return build_status(as_of, route, handoff, confirm)


def save_status(status: DailyStatus, out_dir: str | Path | None = None) -> Path:
    """Write the status as a new file and return its path. An existing status is never overwritten."""
    folder = Path(out_dir) if out_dir is not None else default_status_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"daily_status_{status.as_of}_{status.created_at:%Y%m%dT%H%M%SZ}.json"
    with path.open("x", encoding="utf-8") as fh:
        json.dump(status.to_dict(), fh, indent=2)
        fh.write("\n")
    return path
