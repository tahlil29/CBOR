# Bank Branch & ATM Simulation System

A local Flask web app for a classroom/demo banking simulation. It uses a local SQLite database, synthetic customer records, virtual money, role-based employee access, audit logs, ATM cash controls, manager approvals and CSV exports.

## Included
- One simulated branch and six ATM machines.
- 1,000 seeded synthetic customers and one active account per seeded customer.
- Six employee logins: Ankit (cash deposits), Shifa (cash withdrawals), Jyoti (ATMs/loans/lockers), Tahlil (account opening/closing), Sanket (KYC/digital support), Bhavesh (manager).
- Virtual deposits/withdrawals with balance checks, transaction references, cash ledger entries and audit logs.
- Account opening/closing requests and manager approval.
- ATM low-cash thresholds, replenishment/collection requests and manager approval.
- KYC record tracking, loan/locker/support cases, complaint register.
- Manager approval queue, audit log and CSV reports.

## Requirements
- Python 3.10 or newer
- Internet is only needed once if pip needs to download Flask dependencies. Google Fonts are optional and only affect typography.

## Run on Windows
1. Extract `bank_branch_atm_simulator.zip`.
2. Open the extracted folder in Command Prompt or PowerShell.
3. Create a virtual environment:
   ```powershell
   py -m venv .venv
   .venv\Scripts\activate
   ```
4. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
5. Start the app:
   ```powershell
   python app.py
   ```
6. Open `http://127.0.0.1:5000` in your browser.

## Run on macOS/Linux
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```
Then open `http://127.0.0.1:5000`.

## Demo logins
The default password for all seeded demo users, including `admin`, is `Demo@123`. Change these before showing the demo to others.

| Username | Employee | Department |
|---|---|---|
| `ankit` | Ankit | Cash Deposit |
| `shifa` | Shifa | Cash Withdrawal |
| `jyoti` | Jyoti | ATM, Loans & Lockers |
| `tahlil` | Tahlil | Account Management |
| `sanket` | Sanket | Digital Banking & KYC |
| `bhavesh` | Bhavesh | Branch Manager |
| `admin` | System Administrator | Administration (roles and password resets) |

Change each password after first login using **Change password**. For a classroom demo, use only on your own machine or a trusted local network.

## Useful demo flow
1. Sign in as `ankit`, open Cash transactions, and process a deposit to an active account.
2. Sign in as `shifa` and try a withdrawal; insufficient-balance requests should be rejected.
3. Sign in as `tahlil`, create an Account Opening request, then sign in as `bhavesh` and approve it.
4. Sign in as `jyoti` and submit an ATM replenishment request; Bhavesh approves it in Manager approvals.
5. Sign in as `sanket` and create/update a KYC record.
6. Open Reports and export transactions, customers, ATM status, cases or audit records as CSV.

## Database and reset
The database `bank_simulator.db` is created beside `app.py` on first startup. To reset all demo data, stop the server and delete that database file, then restart the app. A fresh set of 500 seeded customers and the default employee credentials will be recreated.

Optional database path:
- Windows PowerShell: `$env:BANK_SIM_DB="C:\\path\\to\\bank_simulator.db"`
- macOS/Linux: `export BANK_SIM_DB="/path/to/bank_simulator.db"`

## Important limitations / safety
- This is a training simulator, not real banking software. It does not connect to payment networks, banks, ATMs or identity-verification providers.
- All money is virtual. Customer records are synthetic and use reserved `.test` email domains.
- Never enter real Aadhaar/PAN numbers, OTPs, passwords, bank credentials or real customer records.
- This starter is suitable for a local project demo, not production deployment. Before any public deployment, add CSRF protection, secure secret management, HTTPS, stronger session settings, database migrations, backups, rate limiting, authorization tests and a security review.
- Loan approval is a simulated case decision only; it does not calculate eligibility or disburse real funds.
