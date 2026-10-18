# POB for the MT5 forex application

Source project: `C:\dashboard\forex\ChandraTrading`. No EXE was built as part of this change.

## Use POB

1. Select **POB** with any existing strategies you want to use. POB has its own symbol, timeframe and lot size; Paper/Live mode is shared.
2. Enter **POB Selected High** and **POB Selected Low**. The preview shows buy/sell entry, initial SL, T2 and T3. High must exceed Low; every calculated price must be positive.
3. Choose **POB Stop-loss**: **Default — current stop-loss** preserves the initial SL formulas below. **Buy / Sell levels** uses the **Sell entry level as BUY SL**, and the **Buy entry level as SELL SL**. The preview and saved trade plan reflect this choice. At T2, either mode still moves SL to the position's own strategy entry under the existing T2 rules. Change this setting only while POB has no open position or unresolved request.
4. Select **Retest** or **Without retest**, then save and start.
   - Retest: a completed candle must close above the buy entry or below the sell entry. A subsequent live retest enters. A later completed candle can supply the established missed-retest fallback, unless that candle also hits initial SL or T2. Orders use the current executable quote, never a fictional historical fill.
   - Without retest: a fresh live ask crossing upward through buy entry enters BUY; a fresh live bid crossing downward through sell entry enters SELL. The first quote after starting/restarting is a baseline, not an entry. Quotes beyond T2 do not open a new trade.
5. **STOP TRADING** pauses entries while open-position management continues. **CLOSE POB** closes only POB. **STOP BOT** stops the monitoring loop; broker-held SL/T3 remain, but the app must be running to manage T2.

For range `R = High - Low`, the Pine levels remain:

| Side | Entry | Initial SL | T2 | T3 |
| --- | --- | --- | --- | --- |
| Buy | High + 0.236R | High − 0.236R | High + **1.460R** | High + 2.360R |
| Sell | Low − 0.236R | Low + 0.236R | Low − **1.416R** | Low − 2.360R |

After T3 completes and the position is closed, POB pauses further entries. The pause survives restarts; changing POB configuration resets it. T1 remains in the calculation but does not cause an exit. The asymmetric T2 coefficients are intentional.

## Forex volumes and execution

- Lot size must satisfy the symbol's MT5 minimum, maximum and volume step; it is not silently rounded up.
- At T2, close half of the **confirmed entry volume** if both halves are valid broker volumes. Move the remaining SL to the calculated strategy entry only after the requested half is confirmed filled.
- If an exact split is impossible, retain the entire position and move SL to strategy entry at T2; exit all remaining volume at T3 or SL. This is the user-selected fallback. Examples with minimum/step `0.01`: `0.02` splits into `0.01 + 0.01`; `0.01` and `0.03` retain their full volume.
- Live POB uses native MT5 BUY/SELL with its own magic number `26081205`. It requires a **hedging account** so concurrent strategies cannot merge into a net position, and FOK or IOC execution support.
- Initial SL and T3 TP are sent with the entry. Broker price levels are rounded to the symbol's tick size. Broker stop/freeze-distance rules can reject an SL amendment; the desired strategy SL and last observed broker SL are shown separately. Monitoring continues to enforce the desired stop when the request outcome is known.
- Partial fills, broker-side exits and delayed history are reconciled against the position identifier and deal history. T2 retries request only the unfilled part of the half. A gap through T3 closes the remaining exposure directly.

## Restart and recovery

`data\pob_state.json` stores the saved configuration, armed retest, original levels, position, T2 progress, broker account/server/symbol, fill events and request intent. If the bot was running when the server stopped, the saved strategy runtime checkpoint automatically resumes it after account and broker checks. An explicitly stopped bot remains stopped. Older installations without a checkpoint need configuration and Start Bot.

An unresolved request blocks additional POB orders until broker evidence resolves it. If the POB panel remains pending, review the MT5 order/deal history and account connection. Do not delete the recovery file or resend an uncertain order manually. A corrupt journal, account mismatch or an owned position without a saved plan requires review before POB can run. Keep a backup of the journal when investigating.

For an upgrade on the **same account and machine**, keep the existing application's `data` directory and license beside the replacement EXE. Do not distribute another customer's runtime journal. POB configuration cannot be removed or changed while its position/request is active.

## Source verification

In PowerShell:

```powershell
Set-Location "C:\dashboard\forex\ChandraTrading"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install pytest httpx
.\.venv\Scripts\python.exe -m pytest tests -q
```

The POB tests use a fake MT5 broker; they never place live trades. Source launch with normal license validation:

```powershell
.\.venv\Scripts\python.exe launcher.py
```

Open `http://127.0.0.1:8000` and use Paper mode for initial checks.

## Build the EXE manually

These are instructions for you to run; the agent has not run them. Close the source app before replacing an installed executable. The commands below build into a separate output location and do not delete an existing release or its data.

```powershell
Set-Location "C:\dashboard\forex\ChandraTrading"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install pyinstaller
New-Item -ItemType Directory -Force "build\pob" | Out-Null

.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile `
  --name ChandraTrading `
  --distpath "dist\pob" `
  --workpath "build\pob\work" `
  --specpath "build\pob" `
  --add-data "frontend;frontend" `
  launcher.py
```

Output: `C:\dashboard\forex\ChandraTrading\dist\pob\ChandraTrading.exe`.

For an existing installation, replace only its `ChandraTrading.exe`, retaining its `license\license.key` and `data\pob_state.json`. For a new installation, put the EXE in a new folder and create sibling `license` and `data` folders; place that machine's valid signed license in `license\license.key`. Never include the private signing key.

The existing `BUILD_WINDOWS.ps1` deletes `dist`, `build` and `release`. The existing `CREATE_RELEASE.ps1` also deletes `release` and expects an onedir build. They do not match this separate onefile workflow; use the explicit commands above for this build.
