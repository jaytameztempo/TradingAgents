# Go-live runbook: BATS on the Alpaca paper account

How to bring the Tradebot live on the paper account, run it day to day, and respond when something is red.
Everything here is **paper only**: every order path refuses a key ID that doesn't start with `PK`, and refuses
any URL other than the paper URL.

## The flow

1. **Scans:** `run_daily`, which the scheduler runs at 16:30 if Automation is on, labels the market (UP, DOWN or
   SIDE) and runs the scans for that label.
2. **Queue:** the router-allowed names go into the **Tradebot queue** (status `waiting`).
3. **Arming, after the close:** a name becomes `armed` when its daily triggers hold. Those are a TTM Squeeze
   release in the trade's direction or an RSI(14) extreme, **and** light volume.
4. **Triggering, during the session:** an armed name becomes `triggered` when the 4-hour and 1-hour squeeze fire
   on complete bars.
5. **Ticket:** levels and size are set from the last completed session, and the name becomes `ticketed`.
6. **Send:** the **risk guard** checks the entry, the entry goes as a limit DAY order, and the name becomes
   `sent`. Once it fills, the next sender run places an OCO exit: the target, and a stop measured from the
   actual fill.
7. **Expiry:** a name that never triggers expires after 10 sessions. A name drops out at once if the label flips,
   it falls out of its scan, or its earnings date is close or unknown.

**Modes** (Settings → Tradebot):

- **manual:** the scheduler does nothing for the Tradebot; you click everything.
- **semi:** the scheduler runs the checks; you ticket and send.
- **auto:** the scheduler checks, tickets and sends. It sends **only while Paper auto-send is on too**.

## Before the first day

1. Move the repo out of OneDrive (to `C:\dev\TradingAgents`), recreate `.venv` there, and run:

   ```powershell
   uv pip install -e ".[ui,dev]"
   cd ui\web; npm install; npm run build; cd ..\..
   .venv\Scripts\python -m pytest
   ```

2. Run `.venv\Scripts\python -m extensions.scripts.check_setup`. It should show no **FAIL**.
3. On the Settings page:
   - **Trading settings:** save the window, Mon–Fri 09:35–15:55. Leave Paper auto-send **off** for now.
   - **Risk limits:** check the defaults: 3% daily loss, 10% drawdown, 15 positions, 60% gross exposure. The
     kill switch is off.
   - **Automation:** on, weekdays 16:30, run "Daily pipeline", at your category.
   - **Tradebot:** mode **semi**.
4. Power and wake settings:
   - no sleep on AC power during market hours,
   - wake timers allowed (Power Options → Sleep → Allow wake timers).
5. Run `powershell -ExecutionPolicy Bypass -File scripts\start_bats.ps1 -Install`. Then sign out and back in,
   and check that the Dashboard shows the scheduler **ticking**.

## Week 1: semi mode

**Each evening, after 16:30:**

- **Dashboard → Tradebot and scheduler:** the counts moved, and the scheduler is ticking.
- **Trade → Tradebot queue:** click a reason to see each check. Look at the names that armed, and spot-check that
  the reasons make sense against the chart.

**Each morning, from about 09:40, and after each intraday check:**

1. Trade → **Preview**. This shows levels and size for triggered names, and the guard's decision on ticketed
   ones. Nothing is written.
2. **Ticket triggered**, then Preview again.
3. **Send…** to send the entries the guard allows.
4. Later in the day, **Send…** again. This places exits for entries that filled, and sends nothing twice.

**At 16:05:** Dashboard → **Reconcile** should be **clean**.

**At least once during week 1, the kill switch drill** (during market hours):

1. Write one small manual ticket on the Trade page and send it.
2. Settings → Risk limits → **Flatten…**, type `FLATTEN`.
3. Check:
   - the order was cancelled, or the position closed,
   - the Dashboard ledger shows `kill_switch_on`, `cancel` and `close`,
   - the Trade page guard chip says "blocked: kill switch is on".
4. Turn the kill switch off.

## Switching to auto

Switch only when all of these held for the week:

- every evening's reconcile was clean, or each problem was understood and fixed,
- the guard decisions matched what you would have done,
- the scheduler never stopped ticking during market hours,
- the kill switch drill worked.

Then turn on Settings → Tradebot → **auto** (it asks to confirm) **and** Trading settings → **Paper auto-send**.
In auto mode the scheduler:

- at each intraday time (10:35 … 15:35): checks, tickets, sends, then reconciles,
- at each send time (09:40): tickets overnight-triggered names, sends, places exits, then reconciles,
- at 16:05: reconciles.

**Watch the first few sessions:** today's ledger, the reconcile panel, and the guard chips.

**To stop automatic sending,** turn either switch off. To stop **all** entries, including manual ones, use the
kill switch.

## When something is red

| What you see | What it means | What to do |
|---|---|---|
| Reconcile `entry_filled_no_exit` | An entry filled but its exit isn't on the account yet | Trade → **Send…**: the sender places the exit from the actual fill. It's also placed automatically at the next auto send. |
| Reconcile `position_no_stop` | A position has no protective stop. The guard blocks every new entry until it's fixed | Place the exit (as above). For a position that isn't BATS's, close it or give it a stop. |
| Reconcile `orphan_position` | A position no ticket accounts for | Check whether it was opened by hand. Close it, or accept it: it still counts toward the exposure limits. |
| Reconcile `stale_open_entry` | An entry is still open after the close | Cancel it in the Alpaca paper dashboard, then run reconcile again. |
| Reconcile `rejected` | The account refused one of our orders | Read the reason in the ledger, usually buying power or a short that can't be borrowed. The item is blocked. |
| Reconcile `queue_*` | The queue and the account disagree | Usually an order changed outside BATS. Remove the item, or let the next send update it. |
| Guard chip "blocked: daily loss …" or "drawdown …" | A loss limit was reached | No new entries today (daily loss) or until equity recovers (drawdown). Exits keep working. |
| Guard chip "skipped: … gross exposure / positions / stop-risk" | A limit leaves no room for this name | Normal; it stays ticketed and is tried again at the next send. |
| Scheduler "not ticking" | The server isn't running | Check the task (`Get-ScheduledTask -TaskName "BATS server"`) and the latest `~/.tradingagents/logs/server_*.err.log`. Start it with `scripts\start_bats.ps1`. Missed send and intraday slots run once when it comes back. |
| Red banner "data folder … inside OneDrive" | The scheduler refused to start | Move the data folder out of OneDrive and restart. |

## Where things live

| What | Where |
|---|---|
| Queue, scans, routes, tickets, settings | `~/.tradingagents/` (`queues.json`, `baskets/`, `routes/`, `tickets/`, `settings.json`) |
| Tradebot tickets | `~/.tradingagents/tickets/tradebot/tradebot_tickets_<date>_<stamp>.json` |
| Order ledger | `~/.tradingagents/orders/ledger_YYYY-MM.jsonl` |
| Reconcile reports | `~/.tradingagents/status/reconcile_<stamp>.json` |
| Peak equity (drawdown) | `~/.tradingagents/risk_state.json` |
| Server logs | `~/.tradingagents/logs/server_<stamp>.log`, `.err.log`, `.check.log` |
| Scheduler state | `~/.tradingagents/scheduler_state.json` |

**Commands** (from the repo, with `.venv\Scripts\python -m …`):

- `extensions.scripts.run_tradebot --stage eod|intraday [--ticket] [--dry-run]`
- `paper_orders.sender --tradebot [--preview | --send]`
- `paper_orders.reconcile`
- `paper_orders.flatten [--send]`
- `paper_orders.snapshot`
- `extensions.scripts.check_setup`
