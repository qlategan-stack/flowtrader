# FlowTrader — Operations Runbook

Operational procedures and incident responses. Created 2026-06-16 in response to
the daily audit; covers the gaps that audit flagged as undocumented.

---

## API key rotation (C-1)

The bot reads `ANTHROPIC_API_KEY` from `trading-bot/trading-bot/.env` via
`load_dotenv`. **`load_dotenv` does NOT override an already-set OS environment
variable.** This caused a ~25h outage on 2026-06-15/16: a stale, invalid key was
set at the Windows *Machine* scope and silently overrode the valid `.env` key, so
every decision returned `401 invalid x-api-key` while the bot kept SKIPping.

### To rotate the key
1. Put the new key in `.env`:  `ANTHROPIC_API_KEY=sk-ant-...`
2. **Check for shadowing OS env vars** (this is the step that bit us):
   ```powershell
   [System.Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY','Machine')
   [System.Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY','User')
   ```
   If either is set, delete it (the `.env` value should be the single source of truth):
   ```powershell
   [System.Environment]::SetEnvironmentVariable('ANTHROPIC_API_KEY',$null,'Machine')
   [System.Environment]::SetEnvironmentVariable('ANTHROPIC_API_KEY',$null,'User')
   ```
   Note: a *running* shell keeps the old value in its Process scope; new processes
   (Task Scheduler launches) are clean. The scheduled bot picks up the change on
   its next cycle with no restart needed.
3. Update the GitHub Actions secret if the cloud workflows use it:
   `gh secret set ANTHROPIC_API_KEY -R qlategan-stack/flowtrader --body "sk-ant-..."`
4. Verify: `curl -s -o /dev/null -w "%{http_code}" https://api.anthropic.com/v1/messages -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" -H "content-type: application/json" -d '{"model":"claude-haiku-4-5-20251001","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}'` → expect `200`.

The bot already alerts via Telegram on the first API failure of each kind
(`main.py` Step 6b, rate-limited 24h). The watchdog below is the backstop.

---

## Liveness watchdog (C-2)

The June 10–13 blackout (4 days, no cycles) went unnoticed because the bot can't
alert about its own death. `scripts/heartbeat_watchdog.py` runs on a SEPARATE
Task Scheduler entry and alerts if `trades.jsonl` has had no new entry within
`--max-silence-mins` (default 90 = three missed cycles). It makes no API/exchange
calls, so it survives the exact failure modes that took the bot down.

### Schedule it
Create a Windows Task Scheduler task (separate from run_bot):
- Program: `scripts\run_watchdog.bat`
- Trigger: every 30–60 min, 24/7
- Start in: `trading-bot\trading-bot`

State in `journal/last_heartbeat_alert.json`; re-alerts every 12h while down.

---

## Git ref corruption (H-2)

A failed/interrupted push to the dashboard repo can leave `refs/heads/main`
pointing at an invalid object (`cannot lock ref 'HEAD': ... reference broken`),
silently failing every subsequent commit until repaired. `push_journal.py` now
calls `_ensure_repo_healthy()` at the start of every push, which detects this and
re-points `main` at `origin/main` automatically. If it ever can't auto-repair, it
logs loudly and skips the push (rather than failing silently).

Manual repair, if ever needed:
```bash
cd flowtrader-dashboard
git fetch origin
printf "$(git rev-parse origin/main)\n" > .git/refs/heads/main   # if update-ref can't lock
git fsck --full   # confirm clean
```

---

## Risk profile `updated_at` vs file mtime (H-4 — NOT a bug)

`run_bot.bat` does `copy /Y` of `risk_profile.json` from the dashboard repo into
the bot's journal each cycle. The copy updates the file's **mtime** but not its
inner **`updated_at`** stamp. So a recent mtime with an older `updated_at` is
expected and correct: `updated_at` = when the *value* last changed (set by the
dashboard writer), mtime = when it was last *synced*. They are not supposed to
match. The active profile takes effect on the next cycle after the value changes.

---

## Journal field semantics (H-1, H-5)

- `quantity` / `entry_price` reflect what the **executor actually placed/filled**,
  not the AI decision's narrative number. The decision's intent is preserved in
  `intended_quantity` / `intended_entry_price`.
- `open_positions` is the **watchlist-filtered** count (the cap-relevant number).
  `open_positions_raw` is the unfiltered exchange count incl. testnet dust. Use
  `open_positions` for risk/cap reasoning, `open_positions_raw` only for debugging
  exchange-vs-bot discrepancies.
