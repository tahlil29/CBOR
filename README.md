# Central Bank of Rajasthan — Udaipur Main Branch

A Flask training simulator for a branch operations desk: customer CIFs, savings, current and salary accounts, teller cash, NEFT / IMPS / UPI inside the branch, ATM cash, KYC, loans, lockers, complaints, manager approval and an audit log.

All customers, balances and payments are fictional. Nothing here connects to a real bank, card network or identity service.

## Branch identity

| | |
|---|---|
| Branch | Udaipur Main Branch |
| IFSC | `CBRU0001234` |
| MICR | `313029001` |
| Branch code | `001234` |
| Address | 14, City Palace Road, Udaipur, Rajasthan 313001 |

Customer IDs look like `CBRCIF000001`. Account numbers are 14 digits beginning with `001234`.

## Run locally

Python 3.11 or newer.

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`.

macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

The first start creates `bank_simulator.db` beside `app.py`, with 1,000 customers, teller history, six ATMs and the employee desks. Delete that file and start again to rebuild it.

Optional database path:

```powershell
$env:BANK_SIM_DB="C:\path\to\bank_simulator.db"
```

## Demo desks

Shared password: `Demo@123`

| Username | Desk |
|---|---|
| `ankit` | Cash deposits |
| `shifa` | Cash withdrawals |
| `jyoti` | ATMs, loans and lockers |
| `tahlil` | Account opening and closing |
| `sanket` | KYC, NEFT, IMPS and UPI |
| `bhavesh` | Branch manager |
| `admin` | Roles and password resets |

## Deploy on Render

The repository includes `Procfile`, `runtime.txt`, `requirements.txt` and `render.yaml`.

1. Push this project to GitHub.
2. In Render, choose **New** → **Blueprint** and select the repository. Render reads `render.yaml`.
3. Or create a **Web Service** manually:
   - Runtime: Python
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 --access-logfile - --error-logfile - app:app`
   - Health check path: `/healthz`
4. Set environment variables:
   - `BANK_SIM_SECRET` — a long random string. The blueprint generates one.
   - `BANK_SIM_SECURE` — `1` so the session cookie is HTTPS-only.
5. Deploy and open the service URL. Sign in with any username above and `Demo@123`.

`/healthz` returns JSON when the process and database are up. The app listens on Render's `PORT` and seeds the branch database during startup, so a fresh instance is usable without a manual step.

The free plan disk is ephemeral. A new deploy or a wiped instance recreates the 1,000 fictional customers and clears payments made during the demo. To keep data, attach a persistent disk on a paid plan and set `BANK_SIM_DB` to a file on that disk, for example `/var/data/bank_simulator.db`.

Use one Gunicorn worker. SQLite is the database, and extra workers would fight over the same file.

## Useful demo

1. Sign in as `ankit` and post a cash deposit in a multiple of ₹100.
2. Sign in as `shifa`. A withdrawal larger than the balance, or larger than the teller vault, is declined.
3. Sign in as `sanket` and post a UPI or NEFT transfer between two accounts of this IFSC.
4. Sign in as `tahlil`, open an account, then sign in as `bhavesh` and approve it.
5. Sign in as `jyoti`, raise an ATM replenishment, and approve it as `bhavesh`. An approved loan is credited to the customer's operative account.
6. Open Reports and download the CSV files.

## Limits

This is a classroom simulator. Do not enter real Aadhaar numbers, PANs, passwords or customer records. Loan sanction in this app only credits a virtual balance. It is not a core-banking system and it is not meant to hold real money.
