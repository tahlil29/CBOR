import csv
import io
import logging
import os
import random
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE)
DB_PATH = os.environ.get("BANK_SIM_DB", os.path.join(DATA_DIR, "bank_simulator.db"))
IST = ZoneInfo("Asia/Kolkata")
VAULT_OPENING = 2_500_000
DEMO_PASSWORD = "Demo@123"
LOG = logging.getLogger("cbr")

BRANCH = {
    "name": "Central Bank of Rajasthan",
    "branch": "Udaipur Main Branch",
    "code": "001234",
    "ifsc": "CBRU0001234",
    "micr": "313029001",
    "address": "14, City Palace Road, Udaipur, Rajasthan 313001",
    "phone": "0294-2520100",
    "hours": "Monday to Friday, 10:00–16:00 IST",
    "savings_rate": "2.70% p.a.",
    "current_rate": "0.00% p.a.",
}

ROLE_DESKS = {
    "deposit": ("Cash Deposit", "Cash Officer"),
    "withdrawal": ("Cash Withdrawal", "Cash Officer"),
    "atm": ("ATM, Loans & Lockers", "Operations Officer"),
    "accounts": ("Account Management", "Account Services Officer"),
    "kyc": ("Digital Banking & KYC", "Digital Banking Officer"),
    "manager": ("Branch Manager", "Branch Manager"),
    "admin": ("Administration", "System Administrator"),
}

EMPLOYEES = [
    ("ankit", "Ankit Sharma", "Cash Deposit", "deposit", "Cash Officer", "CBR-EMP-101", "9829011101"),
    ("shifa", "Shifa Khan", "Cash Withdrawal", "withdrawal", "Cash Officer", "CBR-EMP-102", "9829011102"),
    ("jyoti", "Jyoti Rathore", "ATM, Loans & Lockers", "atm", "Operations Officer", "CBR-EMP-103", "9829011103"),
    ("tahlil", "Tahlil Shaikh", "Account Management", "accounts", "Account Services Officer", "CBR-EMP-104", "9829011104"),
    ("sanket", "Sanket Joshi", "Digital Banking & KYC", "kyc", "Digital Banking Officer", "CBR-EMP-105", "9829011105"),
    ("bhavesh", "Bhavesh Mehta", "Branch Manager", "manager", "Branch Manager", "CBR-EMP-001", "9829011001"),
    ("admin", "System Administrator", "Administration", "admin", "System Administrator", "CBR-EMP-900", "9829011900"),
]

app = Flask(__name__)
app.secret_key = os.environ.get("BANK_SIM_SECRET") or os.environ.get("SECRET_KEY") or "CHANGE-THIS-DEMO-SECRET-KEY"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
app.config["MAX_CONTENT_LENGTH"] = 1_048_576
if os.environ.get("RENDER") or os.environ.get("BANK_SIM_SECURE") == "1":
    app.config["SESSION_COOKIE_SECURE"] = True
if os.environ.get("RENDER") or os.environ.get("BANK_SIM_PROXY") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

_login_failures = {}
_db_initialized = False
_db_init_lock = threading.Lock()
_simulator_lock = threading.Lock()


def now():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def today_prefix():
    return datetime.now(IST).strftime("%Y-%m-%d")


def ensure_db():
    global _db_initialized
    if _db_initialized:
        return
    with _db_init_lock:
        if not _db_initialized:
            init_db()
            _db_initialized = True


def db():
    ensure_db()
    if "db" not in g:
        parent = os.path.dirname(os.path.abspath(DB_PATH))
        if parent:
            os.makedirs(parent, exist_ok=True)
        g.db = sqlite3.connect(DB_PATH, timeout=30)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 5000")
        g.db.execute("PRAGMA journal_mode = WAL")
    return g.db


def money(n):
    try:
        value = int(n or 0)
    except (TypeError, ValueError):
        value = 0
    sign = "-" if value < 0 else ""
    digits = str(abs(value))
    if len(digits) <= 3:
        grouped = digits
    else:
        head, tail = digits[:-3], digits[-3:]
        parts = []
        while head:
            parts.append(head[-2:])
            head = head[:-2]
        grouped = ",".join(reversed(parts)) + "," + tail
    return f"₹{sign}{grouped}"


app.jinja_env.filters["money"] = money


def ensure_column(conn, table, column, definition):
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def next_cif(conn):
    row = conn.execute(
        "SELECT customer_code FROM customers WHERE customer_code LIKE 'CBRCIF%' ORDER BY customer_code DESC LIMIT 1"
    ).fetchone()
    number = int(row[0][-6:]) + 1 if row else 1
    return f"CBRCIF{number:06d}"


def employee_email(username, full_name):
    parts = [p for p in full_name.lower().split() if p.isalpha()]
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[-1]}@cbofr.gov.in"
    return f"{username}@cbofr.gov.in"


def next_employee_code(conn):
    numbers = []
    for row in conn.execute("SELECT employee_code FROM employees").fetchall():
        code = row["employee_code"] or ""
        if code.startswith("CBR-EMP-"):
            try:
                numbers.append(int(code.rsplit("-", 1)[-1]))
            except ValueError:
                continue
    return f"CBR-EMP-{max(numbers, default=100) + 1:03d}"


def next_account_no(conn):
    row = conn.execute(
        "SELECT account_no FROM accounts WHERE account_no LIKE '001234%' ORDER BY account_no DESC LIMIT 1"
    ).fetchone()
    number = int(row[0][-8:]) + 1 if row else 1
    return f"001234{number:08d}"


def make_utr():
    return "CBRU" + datetime.now(IST).strftime("%y%m%d%H%M%S%f")[:16]


AUTO_DEMO_USER = "bhavesh"
AUTO_DEMO_TICK_SECONDS = 90


def get_setting(key, default=""):
    try:
        row = db().execute("SELECT value FROM branch_settings WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return default
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO branch_settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def auto_demo_enabled():
    return get_setting("auto_demo_mode") == "1"


def sync_vault_from_counter_txns(conn):
    today = today_prefix() + "%"
    cash_in = conn.execute(
        """SELECT COALESCE(SUM(amount),0) FROM transactions
           WHERE txn_type='Deposit' AND channel='Branch Counter' AND created_at LIKE ?""",
        (today,),
    ).fetchone()[0]
    cash_out = conn.execute(
        """SELECT COALESCE(SUM(amount),0) FROM transactions
           WHERE txn_type='Withdrawal' AND channel='Branch Counter' AND created_at LIKE ?""",
        (today,),
    ).fetchone()[0]
    conn.execute(
        "UPDATE branch_vault SET cash_balance=?, updated_at=? WHERE id=1",
        (max(500_000, VAULT_OPENING + cash_in - cash_out), now()),
    )


def simulator_auto_tick(conn, force=False):
    with _simulator_lock:
        return _simulator_auto_tick(conn, force)


def _simulator_auto_tick(conn, force=False):
    if not auto_demo_enabled() and not force:
        return False
    last = get_setting("auto_demo_last_tick")
    if not force and last:
        try:
            last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
            if (datetime.now(IST) - last_dt).total_seconds() < AUTO_DEMO_TICK_SECONDS:
                return False
        except ValueError:
            pass
    rng = random.Random(int(time.time()) // AUTO_DEMO_TICK_SECONDS)
    officers = ["Ankit Sharma", "Shifa Khan", "Bhavesh Mehta", "Sanket Joshi"]
    accounts = conn.execute(
        "SELECT id, balance FROM accounts WHERE status='Active' ORDER BY RANDOM() LIMIT 4"
    ).fetchall()
    for acc in accounts[: rng.randint(1, min(3, len(accounts)))]:
        typ = rng.choice(["Deposit", "Deposit", "Withdrawal"])
        amount = rng.randrange(500, 8_000, 100)
        balance = conn.execute("SELECT balance FROM accounts WHERE id=?", (acc["id"],)).fetchone()[0]
        if typ == "Withdrawal" and balance < amount:
            typ = "Deposit"
        delta = amount if typ == "Deposit" else -amount
        ref = make_utr()
        conn.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (delta, acc["id"]))
        conn.execute(
            """INSERT INTO transactions(txn_ref,account_id,txn_type,amount,channel,employee,note,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (ref, acc["id"], typ, amount, "Branch Counter", rng.choice(officers), "Live simulator refresh", now()),
        )
        conn.execute(
            """INSERT INTO cash_ledger(direction,amount,source,reference,employee,created_at)
               VALUES(?,?,?,?,?,?)""",
            ("IN" if typ == "Deposit" else "OUT", amount, "Simulator " + typ, ref, "System", now()),
        )
    for atm in conn.execute("SELECT atm_id, cash_balance FROM atms").fetchall():
        change = rng.randint(-12_000, 15_000)
        new_bal = max(0, min(int(atm["cash_balance"]) + change, 950_000))
        conn.execute("UPDATE atms SET cash_balance=? WHERE atm_id=?", (new_bal, atm["atm_id"]))
    sync_vault_from_counter_txns(conn)
    set_setting(conn, "auto_demo_last_tick", now())
    conn.commit()
    return True


def auto_demo_sign_in():
    emp = db().execute("SELECT username FROM employees WHERE username=?", (AUTO_DEMO_USER,)).fetchone()
    if not emp:
        return False
    session.clear()
    session["_csrf"] = secrets.token_hex(32)
    session.permanent = True
    session["username"] = AUTO_DEMO_USER
    session["auto_demo_session"] = True
    return True


def init_db():
    parent = os.path.dirname(os.path.abspath(DB_PATH))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS employees(
      username TEXT PRIMARY KEY, full_name TEXT NOT NULL, department TEXT NOT NULL,
      role TEXT NOT NULL, password_hash TEXT NOT NULL,
      rank TEXT NOT NULL DEFAULT 'Officer', email TEXT DEFAULT '', phone TEXT DEFAULT '',
      employee_code TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS customers(
      id INTEGER PRIMARY KEY AUTOINCREMENT, customer_code TEXT UNIQUE NOT NULL,
      full_name TEXT NOT NULL, email TEXT, phone TEXT, city TEXT, occupation TEXT,
      address TEXT DEFAULT '', pincode TEXT DEFAULT '', pan_masked TEXT DEFAULT '',
      aadhaar_masked TEXT DEFAULT '', dob TEXT DEFAULT '', nominee TEXT DEFAULT '',
      kyc_status TEXT NOT NULL DEFAULT 'Pending', created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS accounts(
      id INTEGER PRIMARY KEY AUTOINCREMENT, account_no TEXT UNIQUE NOT NULL,
      customer_id INTEGER NOT NULL REFERENCES customers(id), account_type TEXT NOT NULL,
      balance INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'Active',
      ifsc TEXT NOT NULL DEFAULT 'CBRU0001234', opened_at TEXT NOT NULL, closed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS transactions(
      id INTEGER PRIMARY KEY AUTOINCREMENT, txn_ref TEXT UNIQUE NOT NULL,
      account_id INTEGER NOT NULL REFERENCES accounts(id), txn_type TEXT NOT NULL,
      amount INTEGER NOT NULL, channel TEXT NOT NULL, atm_id TEXT, employee TEXT NOT NULL,
      note TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS cash_ledger(
      id INTEGER PRIMARY KEY AUTOINCREMENT, direction TEXT NOT NULL, amount INTEGER NOT NULL,
      source TEXT NOT NULL, reference TEXT NOT NULL, employee TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS branch_vault(
      id INTEGER PRIMARY KEY CHECK (id = 1), cash_balance INTEGER NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS atms(
      atm_id TEXT PRIMARY KEY, location TEXT NOT NULL, cash_balance INTEGER NOT NULL,
      threshold INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'Online', last_service TEXT
    );
    CREATE TABLE IF NOT EXISTS atm_movements(
      id INTEGER PRIMARY KEY AUTOINCREMENT, atm_id TEXT NOT NULL REFERENCES atms(atm_id),
      movement_type TEXT NOT NULL, amount INTEGER NOT NULL, approved_by TEXT,
      employee TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS cases(
      id INTEGER PRIMARY KEY AUTOINCREMENT, case_type TEXT NOT NULL, customer_id INTEGER REFERENCES customers(id),
      title TEXT NOT NULL, details TEXT, status TEXT NOT NULL DEFAULT 'Pending',
      assigned_to TEXT NOT NULL, requested_amount INTEGER DEFAULT 0, decision_note TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS kyc_records(
      id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL REFERENCES customers(id),
      doc_type TEXT NOT NULL, masked_ref TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Pending',
      checked_by TEXT, note TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS lockers(
      locker_id TEXT PRIMARY KEY, size TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Available',
      customer_id INTEGER REFERENCES customers(id), rent INTEGER NOT NULL DEFAULT 2500,
      assigned_at TEXT, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS complaints(
      id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER REFERENCES customers(id),
      category TEXT NOT NULL, details TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Open',
      assigned_to TEXT NOT NULL, resolution TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS audit_logs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, action TEXT NOT NULL,
      entity_type TEXT NOT NULL, entity_id TEXT, details TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS branch_settings(
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );
    """)
    conn.execute(
        "INSERT OR IGNORE INTO branch_settings(key,value) VALUES('auto_demo_mode','0'),('auto_demo_last_tick','')"
    )
    for col, definition in (
        ("rank", "TEXT NOT NULL DEFAULT 'Officer'"),
        ("email", "TEXT DEFAULT ''"),
        ("phone", "TEXT DEFAULT ''"),
        ("employee_code", "TEXT DEFAULT ''"),
    ):
        ensure_column(conn, "employees", col, definition)
    for col, definition in (
        ("address", "TEXT DEFAULT ''"),
        ("pincode", "TEXT DEFAULT ''"),
        ("pan_masked", "TEXT DEFAULT ''"),
        ("aadhaar_masked", "TEXT DEFAULT ''"),
        ("dob", "TEXT DEFAULT ''"),
        ("nominee", "TEXT DEFAULT ''"),
        ("kyc_status", "TEXT NOT NULL DEFAULT 'Pending'"),
    ):
        ensure_column(conn, "customers", col, definition)
    ensure_column(conn, "accounts", "ifsc", "TEXT NOT NULL DEFAULT 'CBRU0001234'")
    for username, name, dept, role, rank, code, phone in EMPLOYEES:
        conn.execute(
            """INSERT OR IGNORE INTO employees(username,full_name,department,role,password_hash,rank,email,phone,employee_code)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (username, name, dept, role, generate_password_hash(DEMO_PASSWORD), rank,
             employee_email(username, name), phone, code),
        )
        conn.execute(
            """UPDATE employees SET full_name=?, rank=?, employee_code=?,
               phone=CASE WHEN phone IS NULL OR phone='' THEN ? ELSE phone END,
               email=?
               WHERE username=?""",
            (name, rank, code, phone, employee_email(username, name), username),
        )
    atm_rows = [
        ("ATM-01", "City Palace Road", 480000, 120000, "Online"),
        ("ATM-02", "Hiran Magri, Sector 4", 265000, 80000, "Online"),
        ("ATM-03", "Udaipur City Bus Stand", 310000, 100000, "Online"),
        ("ATM-04", "Maharana Bhupal Hospital", 190000, 70000, "Online"),
        ("ATM-05", "Bhamashah Industrial Area", 240000, 80000, "Online"),
        ("ATM-06", "University College Road", 42000, 80000, "Online"),
    ]
    for row in atm_rows:
        conn.execute(
            "INSERT OR IGNORE INTO atms(atm_id,location,cash_balance,threshold,status) VALUES(?,?,?,?,?)",
            row,
        )
    for i in range(1, 41):
        size = "Small" if i % 3 else ("Large" if i % 2 else "Medium")
        rent = 1800 if size == "Small" else (3500 if size == "Medium" else 5500)
        conn.execute(
            "INSERT OR IGNORE INTO lockers(locker_id,size,status,rent,updated_at) VALUES(?,?,'Available',?,?)",
            (f"L-{i:03}", size, rent, now()),
        )
    conn.execute(
        "INSERT OR IGNORE INTO branch_vault(id,cash_balance,updated_at) VALUES(1,?,?)",
        (VAULT_OPENING, now()),
    )
    seed_customers(conn)
    seeded_txns = seed_activity(conn)
    if seeded_txns:
        today = today_prefix()
        cash_in = conn.execute(
            """SELECT COALESCE(SUM(amount),0) FROM transactions
               WHERE txn_type='Deposit' AND channel='Branch Counter' AND created_at LIKE ?""",
            (today + "%",),
        ).fetchone()[0]
        cash_out = conn.execute(
            """SELECT COALESCE(SUM(amount),0) FROM transactions
               WHERE txn_type='Withdrawal' AND channel='Branch Counter' AND created_at LIKE ?""",
            (today + "%",),
        ).fetchone()[0]
        conn.execute(
            "UPDATE branch_vault SET cash_balance=?, updated_at=? WHERE id=1",
            (max(500_000, VAULT_OPENING + cash_in - cash_out), now()),
        )
    seed_branch_work(conn)
    conn.commit()
    conn.close()


def seed_customers(conn):
    count = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    if count >= 1000:
        return
    first = ["Aarav", "Aanya", "Vivaan", "Anika", "Aditya", "Diya", "Arjun", "Sara", "Kabir", "Meera",
             "Ishaan", "Zoya", "Rohan", "Ira", "Reyansh", "Aisha", "Kunal", "Nisha", "Farhan", "Priya",
             "Dev", "Neha", "Rahul", "Simran", "Lakshya", "Ananya", "Harsh", "Pooja", "Imran", "Kavya"]
    last = ["Sharma", "Verma", "Khan", "Patel", "Singh", "Mehta", "Joshi", "Gupta", "Shaikh", "Jain",
            "Yadav", "Das", "Rathore", "Choudhary", "Malik"]
    localities = ["Fatehpura", "Hiran Magri Sector 3", "Panchwati", "Shastri Circle", "Sukher",
                  "Bhuwana", "Sector 11", "Savina", "Ambamata", "Delhigate"]
    occupations = ["Student", "Teacher", "Shop owner", "Engineer", "Healthcare worker", "Business owner",
                   "Government service", "Freelancer", "Hotel staff", "Farmer"]
    pins = ["313001", "313002", "313004", "313011"]
    rng = random.Random(20261026)
    stamp = datetime.now(IST)
    rows_c, rows_a, rows_k = [], [], []
    for i in range(count + 1, 1001):
        name = f"{rng.choice(first)} {rng.choice(last)}"
        city = "Udaipur" if rng.random() < 0.72 else rng.choice(["Jaipur", "Ajmer", "Nathdwara", "Rajsamand", "Chittorgarh"])
        opened = stamp - timedelta(days=rng.randint(40, 2200))
        opened_s = opened.strftime("%Y-%m-%d 10:30:00")
        kyc = "Verified" if rng.random() < 0.86 else "Pending"
        product = rng.choices(["Savings", "Current", "Salary"], weights=[72, 18, 10])[0]
        if product == "Current":
            balance = rng.randrange(25_000, 1_800_000, 500)
        elif product == "Salary":
            balance = rng.randrange(8_000, 90_000, 100)
        else:
            balance = rng.randrange(2_500, 450_000, 100)
        pan = "XXXXX" + f"{rng.randint(0, 9999):04d}" + "X"
        aadhaar = "XXXX-XXXX-" + f"{rng.randint(0, 9999):04d}"
        dob = date_years_ago(rng, 21, 68)
        address = f"H.No {rng.randint(1, 420)}, {rng.choice(localities)}"
        phone = str(rng.choice([6, 7, 8, 9])) + f"{rng.randint(0, 999999999):09d}"
        rows_c.append((
            f"CBRCIF{i:06d}", name, f"customer{i}@example.test", phone, city,
            rng.choice(occupations), address, rng.choice(pins), pan, aadhaar, dob,
            rng.choice(first) + " " + rng.choice(last), kyc, opened_s,
        ))
    conn.executemany(
        """INSERT INTO customers(customer_code,full_name,email,phone,city,occupation,address,pincode,
           pan_masked,aadhaar_masked,dob,nominee,kyc_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows_c,
    )
    customers = conn.execute(
        "SELECT id, customer_code, kyc_status, created_at FROM customers WHERE customer_code LIKE 'CBRCIF%'"
    ).fetchall()
    rng = random.Random(20261027)
    for customer in customers:
        serial = int(customer["customer_code"][-6:])
        product = rng.choices(["Savings", "Current", "Salary"], weights=[72, 18, 10])[0]
        if product == "Current":
            balance = rng.randrange(25_000, 1_800_000, 500)
        elif product == "Salary":
            balance = rng.randrange(8_000, 90_000, 100)
        else:
            balance = rng.randrange(2_500, 450_000, 100)
        rows_a.append((f"001234{serial:08d}", customer["id"], product, balance, "Active", BRANCH["ifsc"], customer["created_at"]))
        rows_k.append((
            customer["id"],
            "PAN",
            "XXXXX****X",
            customer["kyc_status"],
            "Branch KYC desk" if customer["kyc_status"] == "Verified" else None,
            customer["created_at"],
        ))
    conn.executemany(
        """INSERT OR IGNORE INTO accounts(account_no,customer_id,account_type,balance,status,ifsc,opened_at)
           VALUES(?,?,?,?,?,?,?)""",
        rows_a,
    )
    conn.executemany(
        """INSERT INTO kyc_records(customer_id,doc_type,masked_ref,status,checked_by,created_at)
           SELECT ?,?,?,?,?,? WHERE NOT EXISTS (
             SELECT 1 FROM kyc_records WHERE customer_id=? AND doc_type='PAN'
           )""",
        [(cid, doc, ref, status, checker, created, cid) for cid, doc, ref, status, checker, created in (
            (r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows_k
        )],
    )


def date_years_ago(rng, min_age, max_age):
    age = rng.randint(min_age, max_age)
    day = datetime.now(IST) - timedelta(days=age * 365 + rng.randint(0, 364))
    return day.strftime("%Y-%m-%d")


def seed_activity(conn):
    if conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]:
        return False
    accounts = conn.execute("SELECT id FROM accounts WHERE status='Active'").fetchall()
    if not accounts:
        return False
    rng = random.Random(20261028)
    sample = rng.sample(list(accounts), k=min(240, len(accounts)))
    stamp = datetime.now(IST)
    txn_rows = []
    seq = 1000
    for acc in sample:
        balance = 0
        for _ in range(rng.randint(2, 4)):
            seq += 1
            days = rng.randint(0, 16)
            moment = (stamp - timedelta(days=days)).replace(
                hour=rng.randint(10, 15), minute=rng.randint(0, 59), second=rng.randint(0, 59), microsecond=0
            )
            created = moment.strftime("%Y-%m-%d %H:%M:%S")
            roll = rng.random()
            if balance < 1500 or roll < 0.58:
                amount = rng.randrange(1_000, 75_000, 100)
                kind = rng.choice(["counter", "counter", "neft"])
                if kind == "counter":
                    txn_rows.append((f"CBRU{moment.strftime('%y%m%d')}{seq:08d}", acc["id"], "Deposit", amount,
                                     "Branch Counter", None, "Ankit Sharma", "Cash deposit at teller", created))
                else:
                    txn_rows.append((f"CBRU{moment.strftime('%y%m%d')}{seq:08d}", acc["id"], "Deposit", amount,
                                     "NEFT", None, "NEFT Inward", "Inward NEFT credit", created))
                balance += amount
            else:
                amount = min(balance, rng.randrange(500, 20_000, 100))
                if amount <= 0:
                    continue
                if roll < 0.82:
                    txn_rows.append((f"CBRU{moment.strftime('%y%m%d')}{seq:08d}", acc["id"], "Withdrawal", amount,
                                     "Branch Counter", None, "Shifa Khan", "Cash withdrawal at teller", created))
                else:
                    channel = rng.choice(["UPI", "IMPS", "NEFT"])
                    txn_rows.append((f"CBRU{moment.strftime('%y%m%d')}{seq:08d}", acc["id"], channel, amount,
                                     channel, None, "Sanket Joshi", f"Outward {channel}", created))
                balance -= amount
        conn.execute("UPDATE accounts SET balance=? WHERE id=?", (balance, acc["id"]))
    conn.executemany(
        """INSERT INTO transactions(txn_ref,account_id,txn_type,amount,channel,atm_id,employee,note,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        txn_rows,
    )
    return True


def seed_branch_work(conn):
    if conn.execute("SELECT COUNT(*) FROM lockers WHERE status='Occupied'").fetchone()[0] == 0:
        people = conn.execute("SELECT id FROM customers ORDER BY id LIMIT 8").fetchall()
        for index, person in enumerate(people, start=1):
            conn.execute(
                "UPDATE lockers SET status='Occupied', customer_id=?, assigned_at=?, updated_at=? WHERE locker_id=?",
                (person["id"], now(), now(), f"L-{index:03d}"),
            )
    if conn.execute("SELECT COUNT(*) FROM complaints").fetchone()[0] == 0:
        samples = conn.execute("SELECT id FROM customers ORDER BY id LIMIT 6").fetchall()
        complaints = [
            (samples[0]["id"], "ATM issue", "Card retained at ATM-03 after a balance enquiry. Card was returned by the custodian.", "Resolved", "Jyoti Rathore", "Card handed back. ATM sensor cleaned."),
            (samples[1]["id"], "Cash transaction", "Customer says a ₹5,000 deposit receipt was not handed over.", "Open", "Ankit Sharma", None),
            (samples[2]["id"], "Net banking", "UPI collect request failed twice. Account is active and KYC is verified.", "Open", "Sanket Joshi", None),
            (samples[3]["id"], "KYC verification", "Address on the masked Aadhaar does not match the account opening form.", "Open", "Sanket Joshi", None),
            (samples[4]["id"], "Account service", "Request for a cheque book of 25 leaves.", "Open", "Tahlil Shaikh", None),
            (samples[5]["id"], "Loan / locker", "Locker rent receipt for L-002 was not received by email.", "Resolved", "Jyoti Rathore", "Receipt regenerated and noted on the case."),
        ]
        for row in complaints:
            conn.execute(
                """INSERT INTO complaints(customer_id,category,details,status,assigned_to,resolution,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (*row, now(), now()),
            )
    if conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0:
        borrowers = conn.execute(
            """SELECT c.id, c.full_name, c.customer_code FROM customers c
               JOIN accounts a ON a.customer_id=c.id WHERE a.status='Active' ORDER BY c.id LIMIT 2 OFFSET 12"""
        ).fetchall()
        for person, amount, purpose in (
            (borrowers[0], 250000, "Working capital for a retail shop in Hiran Magri"),
            (borrowers[1], 750000, "Home repair loan, salary account with this branch"),
        ):
            conn.execute(
                """INSERT INTO cases(case_type,customer_id,title,details,status,assigned_to,requested_amount,created_at,updated_at)
                   VALUES('Loan Application',?,?,?,'Pending','Jyoti Rathore',?,?,?)""",
                (person["id"], f"Loan Application - {person['full_name']}", purpose, amount, now(), now()),
            )
        holder = conn.execute("SELECT id, full_name FROM customers ORDER BY id LIMIT 1 OFFSET 20").fetchone()
        conn.execute(
            """INSERT INTO cases(case_type,customer_id,title,details,status,assigned_to,requested_amount,created_at,updated_at)
               VALUES('Locker Request',?,?,?,'Pending','Jyoti Rathore',3500,?,?)""",
            (holder["id"], f"Locker Request - {holder['full_name']}", "Medium locker requested. KYC verified.", now(), now()),
        )


def audit(action, entity_type, entity_id="", details=""):
    db().execute(
        "INSERT INTO audit_logs(username,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
        (session.get("username", "system"), action, entity_type, str(entity_id), (details or "")[:500], now()),
    )
    db().commit()


def current_employee():
    if not session.get("username"):
        return None
    try:
        return db().execute("SELECT * FROM employees WHERE username=?", (session["username"],)).fetchone()
    except sqlite3.Error:
        return None


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def login_limited(ip):
    moment = time.time()
    recent = [stamp for stamp in _login_failures.get(ip, []) if moment - stamp < 600]
    _login_failures[ip] = recent
    return len(recent) >= 8


@app.context_processor
def inject_template_globals():
    stamp = datetime.now(IST)
    emp = current_employee()
    return {
        "today": stamp.strftime("%d %b %Y"),
        "now_time": stamp.strftime("%I:%M %p"),
        "branch": BRANCH,
        "csrf_token": session.get("_csrf", ""),
        "current_employee": emp,
        "current_role": emp["department"] if emp else "",
        "auto_demo_mode": auto_demo_enabled(),
        "auto_demo_last_tick": get_setting("auto_demo_last_tick"),
    }


@app.before_request
def handle_auto_demo():
    if request.endpoint in (None, "static", "healthz"):
        return None
    if auto_demo_enabled():
        try:
            simulator_auto_tick(db())
        except Exception:
            LOG.exception("simulator auto tick failed")
        if not session.get("username") and request.method == "GET":
            if request.endpoint in ("home", "demo_enter"):
                if auto_demo_sign_in():
                    audit("AUTO_DEMO_LOGIN", "session", AUTO_DEMO_USER, "Auto demo workspace entry")
                return redirect(url_for("dashboard"))
    return None


@app.before_request
def protect_post():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(32)
    if request.method != "POST":
        return None
    sent = request.form.get("csrf_token", "")
    token = session.get("_csrf", "")
    if not sent or len(sent) != len(token) or not secrets.compare_digest(sent, token):
        flash("The form expired. Open the page again and resubmit.", "error")
        return redirect(request.referrer or url_for("home"))
    return None


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    return response


@app.teardown_appcontext
def close_db(error=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def login_required(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("login"))
        return func(*args, **kwargs)
    return wrapped


def roles_required(*roles):
    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            emp = current_employee()
            if not emp:
                return redirect(url_for("login"))
            if emp["role"] not in roles:
                flash("Your desk does not have permission for that action.", "error")
                return redirect(url_for("dashboard"))
            return func(*args, **kwargs)
        return wrapped
    return decorator


def parse_amount(raw):
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def search_q():
    return request.args.get("q", "").strip()[:80]


def like_pattern(q):
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def sql_like_filter(columns, q, connector="AND"):
    like = like_pattern(q) if q else None
    if not like:
        return "", []
    clause = " OR ".join(f"{col} LIKE ? ESCAPE '\\'" for col in columns)
    return f" {connector} ({clause})", [like] * len(columns)


def filter_rows(rows, q, fields):
    if not q:
        return rows
    needle = q.lower()
    out = []
    for row in rows:
        hay = " ".join(str(row[f] or "") for f in fields).lower()
        if needle in hay:
            out.append(row)
    return out


def active_account(account_no):
    return db().execute(
        "SELECT * FROM accounts WHERE account_no=? AND status='Active'", (account_no.strip(),)
    ).fetchone()


@app.route("/healthz")
def healthz():
    try:
        ensure_db()
        db().execute("SELECT 1").fetchone()
    except Exception as exc:
        LOG.exception("health check failed")
        return jsonify(status="error", detail=str(exc)), 503
    return jsonify(status="ok", branch=BRANCH["ifsc"], service="cbr-udaipur")


@app.route("/")
def home():
    return redirect(url_for("dashboard") if session.get("username") else url_for("login"))


@app.route("/demo/enter")
def demo_enter():
    if not auto_demo_enabled():
        flash("Auto demo is off. Sign in with your employee ID and password.", "error")
        return redirect(url_for("login"))
    return redirect(url_for("home"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = client_ip()
        if login_limited(ip):
            flash("Too many sign-in attempts. Wait ten minutes and try again.", "error")
            return render_template("login.html")
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        emp = db().execute("SELECT * FROM employees WHERE username=?", (username,)).fetchone()
        if emp and check_password_hash(emp["password_hash"], password):
            _login_failures.pop(ip, None)
            session.clear()
            session["_csrf"] = secrets.token_hex(32)
            session.permanent = True
            session["username"] = username
            session.pop("auto_demo_session", None)
            audit("LOGIN", "session", username, "Employee signed in")
            return redirect(url_for("dashboard"))
        _login_failures.setdefault(ip, []).append(time.time())
        flash("The employee ID or password is not correct.", "error")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    audit("LOGOUT", "session", session["username"], "Employee signed out")
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    conn = db()
    today = today_prefix() + "%"
    stats = {
        "customers": conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
        "accounts": conn.execute("SELECT COUNT(*) FROM accounts WHERE status='Active'").fetchone()[0],
        "book": conn.execute("SELECT COALESCE(SUM(balance),0) FROM accounts WHERE status='Active'").fetchone()[0],
        "vault": conn.execute("SELECT cash_balance FROM branch_vault WHERE id=1").fetchone()[0],
        "deposits": conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE txn_type='Deposit' AND created_at LIKE ?", (today,)
        ).fetchone()[0],
        "withdrawals": conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE txn_type='Withdrawal' AND created_at LIKE ?", (today,)
        ).fetchone()[0],
        "cash_in": conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE txn_type='Deposit' AND channel='Branch Counter' AND created_at LIKE ?",
            (today,),
        ).fetchone()[0],
        "cash_out": conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE txn_type='Withdrawal' AND channel='Branch Counter' AND created_at LIKE ?",
            (today,),
        ).fetchone()[0],
        "digital": conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE txn_type IN ('NEFT','IMPS','UPI') AND created_at LIKE ?",
            (today,),
        ).fetchone()[0],
        "pending": conn.execute("SELECT COUNT(*) FROM cases WHERE status='Pending'").fetchone()[0],
        "complaints": conn.execute("SELECT COUNT(*) FROM complaints WHERE status!='Resolved'").fetchone()[0],
    }
    atms = conn.execute("SELECT * FROM atms ORDER BY atm_id").fetchall()
    stats["low_atms"] = sum(1 for atm in atms if atm["cash_balance"] <= atm["threshold"])
    recent = conn.execute(
        """SELECT t.*, a.account_no, c.full_name FROM transactions t
           JOIN accounts a ON a.id=t.account_id JOIN customers c ON c.id=a.customer_id
           ORDER BY t.id DESC LIMIT 8"""
    ).fetchall()
    return render_template("dashboard.html", stats=stats, atms=atms, recent=recent, emp=current_employee())


@app.route("/customers")
@login_required
def customers():
    q = search_q()
    base = """SELECT c.*, a.account_no, a.balance, a.account_type, a.status account_status, a.ifsc
              FROM customers c
              LEFT JOIN accounts a ON a.id = (
                SELECT id FROM accounts WHERE customer_id=c.id
                ORDER BY CASE status WHEN 'Active' THEN 0 ELSE 1 END, id LIMIT 1
              )"""
    where, params = sql_like_filter(
        ["c.full_name", "c.customer_code", "a.account_no", "c.phone", "c.email", "c.city"],
        q,
        connector="WHERE",
    )
    rows = db().execute(
        base + where + " ORDER BY c.id DESC LIMIT 100",
        params,
    ).fetchall()
    return render_template("customers.html", rows=rows, q=q)


@app.route("/transactions", methods=["GET", "POST"])
@login_required
def transactions():
    emp = current_employee()
    if request.method == "POST":
        typ = request.form.get("txn_type", "")
        cash_roles = {"Deposit": ("deposit", "manager"), "Withdrawal": ("withdrawal", "manager")}
        digital = typ in ("NEFT", "IMPS", "UPI")
        if typ in cash_roles and emp["role"] not in cash_roles[typ]:
            flash("This cash desk cannot post that entry.", "error")
            return redirect(url_for("transactions"))
        if digital and emp["role"] not in ("kyc", "manager"):
            flash("NEFT, IMPS and UPI are posted by Digital Banking or the branch manager.", "error")
            return redirect(url_for("transactions"))
        if typ not in ("Deposit", "Withdrawal", "NEFT", "IMPS", "UPI"):
            flash("That payment mode is not available at this branch.", "error")
            return redirect(url_for("transactions"))
        amount = parse_amount(request.form.get("amount"))
        limits = {"Deposit": 500_000, "Withdrawal": 500_000, "UPI": 100_000, "IMPS": 500_000, "NEFT": 1_000_000}
        account = active_account(request.form.get("account_no", ""))
        if not account or amount <= 0 or amount > limits[typ]:
            flash(f"Use an active account and an amount from ₹1 to {money(limits.get(typ, 500000))}.", "error")
            return redirect(url_for("transactions"))
        if typ in ("Deposit", "Withdrawal") and amount % 100 != 0:
            flash("Cash is accepted in multiples of ₹100.", "error")
            return redirect(url_for("transactions"))
        conn = db()
        note = request.form.get("note", "").strip()[:200]
        if typ in ("Deposit", "Withdrawal"):
            vault = conn.execute("SELECT cash_balance FROM branch_vault WHERE id=1").fetchone()
            if typ == "Withdrawal" and account["balance"] < amount:
                audit("TRANSACTION_DECLINED", "account", account["account_no"], "Insufficient balance")
                flash("Declined. The available balance does not cover this withdrawal.", "error")
                return redirect(url_for("transactions"))
            if typ == "Withdrawal" and vault and vault["cash_balance"] < amount:
                audit("TRANSACTION_DECLINED", "vault", account["account_no"], "Teller vault short")
                flash("Declined. The teller vault does not have enough cash. Ask the manager for a cash indent.", "error")
                return redirect(url_for("transactions"))
            ref = make_utr()
            delta = amount if typ == "Deposit" else -amount
            conn.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (delta, account["id"]))
            conn.execute("UPDATE branch_vault SET cash_balance=cash_balance+?, updated_at=? WHERE id=1", (delta, now()))
            conn.execute(
                """INSERT INTO transactions(txn_ref,account_id,txn_type,amount,channel,employee,note,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (ref, account["id"], typ, amount, "Branch Counter", emp["full_name"], note, now()),
            )
            conn.execute(
                """INSERT INTO cash_ledger(direction,amount,source,reference,employee,created_at)
                   VALUES(?,?,?,?,?,?)""",
                ("IN" if typ == "Deposit" else "OUT", amount, "Counter " + typ, ref, emp["full_name"], now()),
            )
            audit(typ.upper(), "transaction", ref, f"Account {account['account_no']}; amount {amount}")
            flash(f"{typ} posted. UTR {ref}", "success")
            return redirect(url_for("transactions"))
        beneficiary = active_account(request.form.get("beneficiary", ""))
        charge = 0 if typ == "UPI" else 5
        if not beneficiary or beneficiary["id"] == account["id"]:
            flash("Enter a different active account in this branch as the beneficiary.", "error")
            return redirect(url_for("transactions"))
        if account["balance"] < amount + charge:
            audit("TRANSACTION_DECLINED", "account", account["account_no"], "Insufficient balance for transfer")
            flash("Declined. The balance does not cover the transfer and charges.", "error")
            return redirect(url_for("transactions"))
        utr = make_utr()
        conn.execute("UPDATE accounts SET balance=balance-? WHERE id=?", (amount + charge, account["id"]))
        conn.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (amount, beneficiary["id"]))
        conn.execute(
            """INSERT INTO transactions(txn_ref,account_id,txn_type,amount,channel,employee,note,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (utr, account["id"], typ, amount, typ, emp["full_name"],
             f"To {beneficiary['account_no']}. Charges {money(charge)}. {note}".strip(), now()),
        )
        conn.execute(
            """INSERT INTO transactions(txn_ref,account_id,txn_type,amount,channel,employee,note,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (utr + "C", beneficiary["id"], "Deposit", amount, typ, emp["full_name"],
             f"From {account['account_no']}", now()),
        )
        audit(typ, "transaction", utr, f"{account['account_no']} to {beneficiary['account_no']}; amount {amount}")
        flash(f"{typ} settled inside the branch. UTR {utr}", "success")
        return redirect(url_for("transactions"))
    q = search_q()
    txn_sql = """SELECT t.*, a.account_no, c.full_name FROM transactions t
                 JOIN accounts a ON a.id=t.account_id JOIN customers c ON c.id=a.customer_id"""
    where, params = sql_like_filter(
        ["t.txn_ref", "c.full_name", "a.account_no", "t.txn_type", "t.channel", "t.employee", "t.note"],
        q,
        connector="WHERE",
    )
    rows = db().execute(txn_sql + where + " ORDER BY t.id DESC LIMIT 100", params).fetchall()
    accounts = db().execute(
        """SELECT a.account_no, a.account_type, c.full_name, a.balance FROM accounts a
           JOIN customers c ON c.id=a.customer_id WHERE a.status='Active'
           ORDER BY a.account_no LIMIT 500"""
    ).fetchall()
    vault = db().execute("SELECT cash_balance FROM branch_vault WHERE id=1").fetchone()
    return render_template("transactions.html", rows=rows, accounts=accounts, emp=emp, vault=vault["cash_balance"], q=q)


@app.route("/accounts", methods=["GET", "POST"])
@login_required
def account_cases():
    emp = current_employee()
    if request.method == "POST":
        if emp["role"] not in ("accounts", "manager"):
            flash("Account opening and closing are handled by Account Services or the manager.", "error")
            return redirect(url_for("account_cases"))
        typ = request.form.get("case_type")
        if typ not in ("Account Opening", "Account Closing"):
            flash("Choose account opening or account closing.", "error")
            return redirect(url_for("account_cases"))
        name = request.form.get("full_name", "").strip()[:100]
        code = request.form.get("customer_code", "").strip().upper()
        details = request.form.get("details", "").strip()[:500]
        product = request.form.get("account_type", "Savings")
        if product not in ("Savings", "Current", "Salary"):
            product = "Savings"
        customer = None
        if code:
            customer = db().execute("SELECT * FROM customers WHERE customer_code=?", (code,)).fetchone()
            if not customer:
                flash("That customer ID is not on the branch books.", "error")
                return redirect(url_for("account_cases"))
        if typ == "Account Opening":
            if len(name.split()) < 2:
                flash("Enter the customer's full name.", "error")
                return redirect(url_for("account_cases"))
            phone = request.form.get("phone", "").strip()
            if phone and not re.fullmatch(r"[6-9][0-9]{9}", phone):
                flash("Enter a 10-digit mobile number, or leave it blank.", "error")
                return redirect(url_for("account_cases"))
            pan = request.form.get("pan", "").strip().upper()
            pan_masked = ""
            if pan:
                if not re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", pan):
                    flash("PAN must look like ABCDE1234F. It is stored only in masked form.", "error")
                    return redirect(url_for("account_cases"))
                pan_masked = "XXXXX" + pan[5:9] + "X"
            city = request.form.get("city", "Udaipur").strip()[:40] or "Udaipur"
            cur = db().execute(
                """INSERT INTO customers(customer_code,full_name,email,phone,city,occupation,address,pincode,
                   pan_masked,nominee,kyc_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (next_cif(db()), name, request.form.get("email", "").strip()[:120], phone, city,
                 request.form.get("occupation", "").strip()[:60] or "Not provided",
                 request.form.get("address", "").strip()[:160], request.form.get("pincode", "").strip()[:6],
                 pan_masked, request.form.get("nominee", "").strip()[:80], "Pending", now()),
            )
            customer_id = cur.lastrowid
            details = f"Product: {product}. {details}".strip()
        elif not customer:
            flash("Enter the customer ID for the account you want to close.", "error")
            return redirect(url_for("account_cases"))
        else:
            customer_id = customer["id"]
        cur = db().execute(
            """INSERT INTO cases(case_type,customer_id,title,details,status,assigned_to,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (typ, customer_id, typ + " request", details, "Pending", emp["full_name"], now(), now()),
        )
        audit("CREATE_REQUEST", "case", cur.lastrowid, typ)
        flash("Request is with the branch manager.", "success")
        return redirect(url_for("account_cases"))
    q = search_q()
    case_sql = """SELECT cases.*, customers.customer_code, customers.full_name FROM cases
                  LEFT JOIN customers ON customers.id=cases.customer_id
                  WHERE case_type IN ('Account Opening','Account Closing')"""
    filt, params = sql_like_filter(
        ["CAST(cases.id AS TEXT)", "cases.case_type", "customers.full_name", "customers.customer_code",
         "cases.status", "cases.assigned_to", "cases.details"],
        q,
    )
    cases = db().execute(case_sql + filt + " ORDER BY cases.id DESC LIMIT 100", params).fetchall()
    return render_template("accounts.html", cases=cases, emp=emp, q=q)


@app.route("/kyc", methods=["GET", "POST"])
@login_required
def kyc():
    emp = current_employee()
    if request.method == "POST":
        if emp["role"] not in ("kyc", "manager"):
            flash("KYC updates are handled by Digital Banking or the manager.", "error")
            return redirect(url_for("kyc"))
        code = request.form.get("customer_code", "").strip().upper()
        customer = db().execute("SELECT * FROM customers WHERE customer_code=?", (code,)).fetchone()
        status = request.form.get("status", "Pending")
        doc_type = request.form.get("doc_type", "PAN")
        if not customer or status not in ("Pending", "Verified", "Rejected"):
            flash("Choose a customer on the books and a valid KYC status.", "error")
            return redirect(url_for("kyc"))
        if doc_type not in ("Aadhaar", "PAN", "Voter ID", "Passport", "Driving Licence"):
            doc_type = "PAN"
        masked = request.form.get("masked_ref", "").strip()[:40] or "XXXX"
        cur = db().execute(
            """INSERT INTO kyc_records(customer_id,doc_type,masked_ref,status,checked_by,note,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (customer["id"], doc_type, masked, status, emp["full_name"], request.form.get("note", "").strip()[:300], now()),
        )
        db().execute("UPDATE customers SET kyc_status=? WHERE id=?", (status, customer["id"]))
        audit("KYC_UPDATE", "kyc", cur.lastrowid, f"{code}: {status}")
        flash("KYC record saved.", "success")
        return redirect(url_for("kyc"))
    q = search_q()
    kyc_sql = """SELECT k.*, c.customer_code, c.full_name FROM kyc_records k
                 JOIN customers c ON c.id=k.customer_id"""
    where, params = sql_like_filter(
        ["c.full_name", "c.customer_code", "k.doc_type", "k.masked_ref", "k.status", "k.checked_by", "k.note"],
        q,
        connector="WHERE",
    )
    rows = db().execute(kyc_sql + where + " ORDER BY k.id DESC LIMIT 100", params).fetchall()
    return render_template("kyc.html", rows=rows, emp=emp, q=q)


@app.route("/atms", methods=["GET", "POST"])
@login_required
def atms():
    emp = current_employee()
    if request.method == "POST":
        if emp["role"] not in ("atm", "manager"):
            flash("ATM cash and service updates are handled by Operations or the manager.", "error")
            return redirect(url_for("atms"))
        atm_id = request.form.get("atm_id")
        typ = request.form.get("movement_type")
        amount = parse_amount(request.form.get("amount"))
        atm = db().execute("SELECT * FROM atms WHERE atm_id=?", (atm_id,)).fetchone()
        if not atm or typ not in ("Replenishment", "Cash Collection", "Service Update") or amount < 0 or amount > 2_000_000:
            flash("Check the ATM, the action and the amount.", "error")
            return redirect(url_for("atms"))
        if typ == "Service Update":
            status = request.form.get("atm_status", "Online")
            if status not in ("Online", "Offline", "Maintenance"):
                status = "Online"
            db().execute("UPDATE atms SET status=?, last_service=? WHERE atm_id=?", (status, now(), atm_id))
            amount = 0
        elif emp["role"] != "manager":
            case_type = "ATM Replenishment" if typ == "Replenishment" else "ATM Cash Collection"
            cur = db().execute(
                """INSERT INTO cases(case_type,title,details,status,assigned_to,requested_amount,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (case_type, f"{typ} for {atm_id}", request.form.get("note", "").strip()[:300], "Pending",
                 emp["full_name"], amount, now(), now()),
            )
            audit("ATM_REQUEST", "case", cur.lastrowid, f"{typ} {atm_id} amount {amount}")
            flash("Cash indent sent to the branch manager. The ATM balance is unchanged.", "success")
            return redirect(url_for("atms"))
        else:
            if typ == "Replenishment":
                db().execute("UPDATE atms SET cash_balance=cash_balance+? WHERE atm_id=?", (amount, atm_id))
            elif amount > atm["cash_balance"]:
                flash("Collection cannot exceed the cash still in the machine.", "error")
                return redirect(url_for("atms"))
            else:
                db().execute("UPDATE atms SET cash_balance=cash_balance-? WHERE atm_id=?", (amount, atm_id))
        db().execute(
            """INSERT INTO atm_movements(atm_id,movement_type,amount,approved_by,employee,note,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (atm_id, typ, amount, emp["full_name"] if emp["role"] == "manager" else None,
             emp["full_name"], request.form.get("note", "").strip()[:300], now()),
        )
        audit("ATM_MOVEMENT", "atm", atm_id, f"{typ} amount {amount}")
        flash("ATM record updated.", "success")
        return redirect(url_for("atms"))
    q = search_q()
    atm_sql = "SELECT * FROM atms"
    where, params = sql_like_filter(
        ["atm_id", "location", "status", "CAST(cash_balance AS TEXT)"],
        q,
        connector="WHERE",
    )
    rows = db().execute(atm_sql + where + " ORDER BY atm_id", params).fetchall()
    mov_sql = "SELECT * FROM atm_movements"
    mov_where, mov_params = sql_like_filter(
        ["atm_id", "movement_type", "employee", "approved_by", "note", "CAST(amount AS TEXT)"],
        q,
        connector="WHERE",
    )
    movements = db().execute(mov_sql + mov_where + " ORDER BY id DESC LIMIT 50", mov_params).fetchall()
    pending_all = db().execute(
        "SELECT * FROM cases WHERE case_type IN ('ATM Replenishment','ATM Cash Collection','ATM Cash Replenishment','ATM Cash Cash Collection') AND status='Pending' ORDER BY id DESC"
    ).fetchall()
    pending = filter_rows(pending_all, q, ["title", "details", "assigned_to", "case_type"])
    return render_template("atms.html", rows=rows, movements=movements, pending=pending, emp=emp, q=q)


@app.route("/services", methods=["GET", "POST"])
@login_required
def services():
    emp = current_employee()
    if request.method == "POST":
        typ = request.form.get("case_type")
        if typ not in ("Loan Application", "Locker Request", "Cheque Book", "E-Banking Support"):
            flash("That service is not offered on this form.", "error")
            return redirect(url_for("services"))
        if typ in ("Loan Application", "Locker Request") and emp["role"] not in ("atm", "manager"):
            flash("Loans and lockers are handled by Operations or the manager.", "error")
            return redirect(url_for("services"))
        if typ == "Cheque Book" and emp["role"] not in ("accounts", "manager"):
            flash("Cheque books are issued by Account Services or the manager.", "error")
            return redirect(url_for("services"))
        if typ == "E-Banking Support" and emp["role"] not in ("kyc", "manager"):
            flash("Digital support is handled by Digital Banking or the manager.", "error")
            return redirect(url_for("services"))
        code = request.form.get("customer_code", "").strip().upper()
        customer = db().execute("SELECT * FROM customers WHERE customer_code=?", (code,)).fetchone()
        if not customer:
            flash("Enter a customer ID from the directory, such as CBRCIF000001.", "error")
            return redirect(url_for("services"))
        amount = parse_amount(request.form.get("amount"))
        if typ == "Loan Application" and (amount < 10000 or amount > 1_000_000):
            flash("Retail loans at this branch are between ₹10,000 and ₹10,00,000.", "error")
            return redirect(url_for("services"))
        cur = db().execute(
            """INSERT INTO cases(case_type,customer_id,title,details,status,assigned_to,requested_amount,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (typ, customer["id"], f"{typ} - {customer['full_name']}", request.form.get("details", "").strip()[:500],
             "Pending", emp["full_name"], amount, now(), now()),
        )
        audit("SERVICE_REQUEST", "case", cur.lastrowid, typ + " for " + code)
        flash("Request submitted for the branch manager.", "success")
        return redirect(url_for("services"))
    q = search_q()
    case_sql = """SELECT cases.*, customers.customer_code, customers.full_name FROM cases
                  LEFT JOIN customers ON customers.id=cases.customer_id
                  WHERE case_type IN ('Loan Application','Locker Request','Cheque Book','E-Banking Support','Other Verification')"""
    filt, params = sql_like_filter(
        ["CAST(cases.id AS TEXT)", "cases.case_type", "customers.full_name", "customers.customer_code",
         "cases.status", "cases.assigned_to", "cases.details"],
        q,
    )
    cases = db().execute(case_sql + filt + " ORDER BY cases.id DESC LIMIT 100", params).fetchall()
    locker_sql = """SELECT l.*, c.customer_code, c.full_name FROM lockers l
                    LEFT JOIN customers c ON c.id=l.customer_id"""
    lock_where, lock_params = sql_like_filter(
        ["l.locker_id", "l.size", "l.status", "c.customer_code", "c.full_name"],
        q,
        connector="WHERE",
    )
    lockers = db().execute(locker_sql + lock_where + " ORDER BY l.locker_id", lock_params).fetchall()
    return render_template("services.html", cases=cases, lockers=lockers, emp=emp, q=q)


def apply_approval(conn, case, decision, note, manager_name):
    if decision != "Approved":
        return None
    if case["case_type"] == "Account Opening":
        exists = conn.execute(
            "SELECT id FROM accounts WHERE customer_id=? AND status='Active'", (case["customer_id"],)
        ).fetchone()
        if exists:
            return None
        product = "Savings"
        if case["details"] and "Product:" in case["details"]:
            product = case["details"].split("Product:", 1)[1].split(".", 1)[0].strip()
        if product not in ("Savings", "Current", "Salary"):
            product = "Savings"
        account_no = next_account_no(conn)
        conn.execute(
            """INSERT INTO accounts(account_no,customer_id,account_type,balance,status,ifsc,opened_at)
               VALUES(?,?,?,0,'Active',?,?)""",
            (account_no, case["customer_id"], product, BRANCH["ifsc"], now()),
        )
        return f"Account {account_no} opened."
    if case["case_type"] == "Account Closing":
        acc = conn.execute(
            "SELECT * FROM accounts WHERE customer_id=? AND status='Active' ORDER BY id LIMIT 1",
            (case["customer_id"],),
        ).fetchone()
        if acc and acc["balance"] != 0:
            return "hold: The account still has a balance. Bring it to zero before closing."
        if acc:
            conn.execute("UPDATE accounts SET status='Closed', closed_at=? WHERE id=?", (now(), acc["id"]))
        return None
    if case["case_type"] == "Locker Request":
        locker = conn.execute("SELECT * FROM lockers WHERE status='Available' ORDER BY locker_id LIMIT 1").fetchone()
        if not locker:
            return "hold: Every locker is allotted. The request stays pending."
        conn.execute(
            "UPDATE lockers SET status='Occupied', customer_id=?, assigned_at=?, updated_at=? WHERE locker_id=?",
            (case["customer_id"], now(), now(), locker["locker_id"]),
        )
        return f"Locker {locker['locker_id']} allotted."
    if case["case_type"] in ("ATM Replenishment", "ATM Cash Replenishment"):
        atm_id = case["title"].split("for ")[-1].strip()
        atm = conn.execute("SELECT * FROM atms WHERE atm_id=?", (atm_id,)).fetchone()
        if atm:
            conn.execute("UPDATE atms SET cash_balance=cash_balance+? WHERE atm_id=?", (case["requested_amount"], atm_id))
            conn.execute(
                """INSERT INTO atm_movements(atm_id,movement_type,amount,approved_by,employee,note,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (atm_id, "Replenishment", case["requested_amount"], manager_name, case["assigned_to"], case["details"], now()),
            )
        return None
    if case["case_type"] in ("ATM Cash Collection", "ATM Cash Cash Collection"):
        atm_id = case["title"].split("for ")[-1].strip()
        atm = conn.execute("SELECT * FROM atms WHERE atm_id=?", (atm_id,)).fetchone()
        if atm and case["requested_amount"] > atm["cash_balance"]:
            return "hold: Collection is higher than the cash in the machine. The request stays pending."
        if atm:
            conn.execute("UPDATE atms SET cash_balance=cash_balance-? WHERE atm_id=?", (case["requested_amount"], atm_id))
            conn.execute(
                """INSERT INTO atm_movements(atm_id,movement_type,amount,approved_by,employee,note,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (atm_id, "Cash Collection", case["requested_amount"], manager_name, case["assigned_to"], case["details"], now()),
            )
        return None
    if case["case_type"] == "Loan Application":
        acc = conn.execute(
            "SELECT * FROM accounts WHERE customer_id=? AND status='Active' ORDER BY id LIMIT 1",
            (case["customer_id"],),
        ).fetchone()
        amount = min(int(case["requested_amount"] or 0), 1_000_000)
        if acc and amount > 0:
            ref = make_utr()
            conn.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (amount, acc["id"]))
            conn.execute(
                """INSERT INTO transactions(txn_ref,account_id,txn_type,amount,channel,employee,note,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (ref, acc["id"], "Deposit", amount, "Loan Disbursement", manager_name,
                 "Retail loan credited to the operative account", now()),
            )
            return f"Sanctioned and credited to {acc['account_no']}. UTR {ref}"
        return "Sanction recorded. No operative account was available for credit."
    if case["case_type"] == "Cheque Book":
        return "Cheque book of 25 leaves marked for issue."
    return None


@app.route("/approvals", methods=["GET", "POST"])
@login_required
@roles_required("manager", "admin")
def approvals():
    if request.method == "POST":
        case = db().execute("SELECT * FROM cases WHERE id=? AND status='Pending'", (request.form.get("case_id"),)).fetchone()
        decision = request.form.get("decision")
        note = request.form.get("decision_note", "").strip()[:300]
        if not case or decision not in ("Approved", "Rejected"):
            flash("That request is no longer pending.", "error")
            return redirect(url_for("approvals"))
        conn = db()
        result = apply_approval(conn, case, decision, note, current_employee()["full_name"])
        if isinstance(result, str) and result.startswith("hold:"):
            flash(result[5:].strip(), "error")
            return redirect(url_for("approvals"))
        final_note = note
        if result:
            final_note = (result + " " + note).strip()
        conn.execute(
            "UPDATE cases SET status=?, decision_note=?, updated_at=? WHERE id=?",
            (decision, final_note, now(), case["id"]),
        )
        audit("MANAGER_" + decision.upper(), "case", case["id"], final_note)
        flash(f"Request #{case['id']} {decision.lower()}.", "success")
        return redirect(url_for("approvals"))
    q = search_q()
    appr_sql = """SELECT cases.*, customers.customer_code, customers.full_name FROM cases
                  LEFT JOIN customers ON customers.id=cases.customer_id
                  WHERE cases.status='Pending'"""
    filt, params = sql_like_filter(
        ["CAST(cases.id AS TEXT)", "cases.case_type", "customers.full_name", "customers.customer_code",
         "cases.title", "cases.details", "cases.assigned_to"],
        q,
    )
    rows = db().execute(appr_sql + filt + " ORDER BY cases.id ASC", params).fetchall()
    return render_template("approvals.html", rows=rows, q=q)


@app.route("/complaints", methods=["GET", "POST"])
@login_required
def complaints():
    emp = current_employee()
    if request.method == "POST" and request.form.get("resolve_id"):
        if emp["role"] not in ("manager", "admin"):
            flash("Only the branch manager can close a complaint.", "error")
            return redirect(url_for("complaints"))
        cid = request.form.get("resolve_id")
        resolution = request.form.get("resolution", "").strip()[:300] or "Closed by the branch manager."
        db().execute(
            "UPDATE complaints SET status='Resolved', resolution=?, updated_at=? WHERE id=? AND status!='Resolved'",
            (resolution, now(), cid),
        )
        audit("COMPLAINT_RESOLVED", "complaint", cid, resolution)
        flash("Complaint closed.", "success")
        return redirect(url_for("complaints"))
    if request.method == "POST":
        code = request.form.get("customer_code", "").strip().upper()
        customer = db().execute("SELECT * FROM customers WHERE customer_code=?", (code,)).fetchone() if code else None
        if code and not customer:
            flash("Customer ID was not found. Leave it blank for a general complaint.", "error")
            return redirect(url_for("complaints"))
        details = request.form.get("details", "").strip()[:500]
        if len(details) < 8:
            flash("Describe the complaint in a sentence.", "error")
            return redirect(url_for("complaints"))
        category = request.form.get("category", "Other")
        if category not in ("ATM issue", "Cash transaction", "Net banking", "KYC verification", "Account service", "Loan / locker", "Other"):
            category = "Other"
        cur = db().execute(
            """INSERT INTO complaints(customer_id,category,details,status,assigned_to,created_at,updated_at)
               VALUES(?,?,?,'Open',?,?,?)""",
            (customer["id"] if customer else None, category, details,
             request.form.get("assigned_to", emp["full_name"])[:80], now(), now()),
        )
        audit("COMPLAINT_CREATED", "complaint", cur.lastrowid, category)
        flash("Complaint logged.", "success")
        return redirect(url_for("complaints"))
    q = search_q()
    comp_sql = """SELECT complaints.*, customers.customer_code, customers.full_name FROM complaints
                  LEFT JOIN customers ON customers.id=complaints.customer_id"""
    where, params = sql_like_filter(
        ["CAST(complaints.id AS TEXT)", "customers.full_name", "customers.customer_code", "complaints.category",
         "complaints.details", "complaints.status", "complaints.assigned_to"],
        q,
        connector="WHERE",
    )
    rows = db().execute(comp_sql + where + " ORDER BY complaints.id DESC LIMIT 100", params).fetchall()
    staff = db().execute("SELECT full_name FROM employees WHERE role!='admin' ORDER BY full_name").fetchall()
    return render_template("complaints.html", rows=rows, emp=emp, staff=staff, q=q)


@app.route("/reports")
@login_required
def reports():
    q = search_q()
    conn = db()
    today = today_prefix() + "%"
    txns = conn.execute(
        """SELECT txn_type, COUNT(*) count, COALESCE(SUM(amount),0) total FROM transactions
           WHERE created_at LIKE ? GROUP BY txn_type""",
        (today,),
    ).fetchall()
    cash = conn.execute(
        """SELECT direction, COALESCE(SUM(amount),0) total FROM cash_ledger
           WHERE created_at LIKE ? GROUP BY direction""",
        (today,),
    ).fetchall()
    atm_rows = conn.execute("SELECT * FROM atms ORDER BY atm_id").fetchall()
    case_rows = conn.execute(
        "SELECT case_type, status, COUNT(*) count FROM cases GROUP BY case_type, status ORDER BY case_type"
    ).fetchall()
    latest = conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 30").fetchall()
    vault = conn.execute("SELECT cash_balance FROM branch_vault WHERE id=1").fetchone()
    txns = filter_rows(txns, q, ["txn_type"])
    atm_rows = filter_rows(atm_rows, q, ["atm_id", "location", "status"])
    case_rows = filter_rows(case_rows, q, ["case_type", "status"])
    latest = filter_rows(latest, q, ["username", "action", "entity_type", "entity_id", "details"])
    return render_template(
        "reports.html", txns=txns, cash=cash, atm_rows=atm_rows, case_rows=case_rows,
        latest=latest, vault=vault["cash_balance"], q=q,
    )


@app.route("/reports/export/<kind>.csv")
@login_required
def export_report(kind):
    conn = db()
    queries = {
        "transactions": ("SELECT txn_ref,txn_type,amount,channel,atm_id,employee,note,created_at FROM transactions ORDER BY id DESC", "transactions.csv"),
        "customers": ("SELECT customer_code,full_name,phone,city,occupation,kyc_status,pan_masked,created_at FROM customers ORDER BY id", "customers.csv"),
        "atms": ("SELECT atm_id,location,cash_balance,threshold,status,last_service FROM atms ORDER BY atm_id", "atm_status.csv"),
        "audit": ("SELECT username,action,entity_type,entity_id,details,created_at FROM audit_logs ORDER BY id DESC", "audit_log.csv"),
        "cases": ("SELECT id,case_type,title,status,assigned_to,requested_amount,decision_note,created_at,updated_at FROM cases ORDER BY id DESC", "cases.csv"),
    }
    if kind not in queries:
        flash("That report is not available.", "error")
        return redirect(url_for("reports"))
    sql, filename = queries[kind]
    rows = conn.execute(sql).fetchall()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(list(rows[0].keys()) if rows else [col[0] for col in conn.execute(sql).description])
    writer.writerows([tuple(row) for row in rows])
    audit("REPORT_EXPORTED", "report", kind, filename)
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/audit")
@login_required
@roles_required("manager", "admin")
def audit_page():
    q = search_q()
    sql = "SELECT * FROM audit_logs"
    where, params = sql_like_filter(
        ["username", "action", "entity_type", "entity_id", "details", "created_at"],
        q,
        connector="WHERE",
    )
    rows = db().execute(sql + where + " ORDER BY id DESC LIMIT 250", params).fetchall()
    return render_template("audit.html", rows=rows, q=q)


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()[:100]
        email = request.form.get("email", "").strip()[:120]
        phone = request.form.get("phone", "").strip()[:15]
        if len(full_name.split()) < 2:
            flash("Enter your full name.", "error")
        elif phone and not re.fullmatch(r"[0-9+\-\s]{8,15}", phone):
            flash("Enter a usable contact number.", "error")
        else:
            db().execute(
                "UPDATE employees SET full_name=?, email=?, phone=? WHERE username=?",
                (full_name, email, phone, session["username"]),
            )
            audit("PROFILE_UPDATED", "employee", session["username"], "Updated employee profile")
            flash("Profile updated.", "success")
            return redirect(url_for("profile"))
    return render_template("profile.html", emp=current_employee())


@app.route("/admin", methods=["GET", "POST"])
@login_required
@roles_required("admin")
def admin_section():
    conn = db()
    if request.method == "POST":
        action = request.form.get("action", "")
        username = request.form.get("username", "").strip().lower()

        if action == "toggle_auto_demo":
            enabled = request.form.get("enabled") == "1"
            set_setting(conn, "auto_demo_mode", "1" if enabled else "0")
            if enabled:
                simulator_auto_tick(conn, force=True)
                audit("AUTO_DEMO_ON", "settings", "auto_demo_mode", "Live simulator enabled")
                flash("Auto demo is ON. Visitors enter without a password; data refreshes about every 90 seconds.", "success")
            else:
                set_setting(conn, "auto_demo_last_tick", "")
                conn.commit()
                audit("AUTO_DEMO_OFF", "settings", "auto_demo_mode", "Password login required")
                flash("Auto demo is OFF. Employees must sign in with user ID and password.", "success")
        elif action == "simulator_refresh":
            if simulator_auto_tick(conn, force=True):
                audit("SIMULATOR_REFRESH", "settings", "manual", "Admin triggered data refresh")
                flash("Branch balances and activity refreshed.", "success")
            else:
                flash("Could not refresh simulator data.", "error")
        elif action == "create":
            full_name = request.form.get("full_name", "").strip()[:100]
            role = request.form.get("role", "")
            new_username = request.form.get("new_username", "").strip().lower()
            password = request.form.get("password", "").strip() or DEMO_PASSWORD
            email = request.form.get("email", "").strip()[:120]
            phone = request.form.get("phone", "").strip()[:15]
            if not re.fullmatch(r"[a-z][a-z0-9_]{2,19}", new_username):
                flash("Username must be 3–20 characters: lowercase letters, numbers or underscore.", "error")
            elif len(full_name.split()) < 2:
                flash("Enter the employee's full name.", "error")
            elif role not in ROLE_DESKS:
                flash("Choose a valid desk role.", "error")
            elif len(password) < 8:
                flash("Password must be at least 8 characters.", "error")
            elif conn.execute("SELECT 1 FROM employees WHERE username=?", (new_username,)).fetchone():
                flash("That username is already on the branch roster.", "error")
            else:
                department, default_rank = ROLE_DESKS[role]
                rank = request.form.get("rank", "").strip()[:60] or default_rank
                conn.execute(
                    """INSERT INTO employees(username,full_name,department,role,password_hash,rank,email,phone,employee_code)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        new_username, full_name, department, role, generate_password_hash(password),
                        rank, email or employee_email(new_username, full_name), phone, next_employee_code(conn),
                    ),
                )
                conn.commit()
                audit("EMPLOYEE_CREATED", "employee", new_username, f"Role {role}")
                flash(f"Employee {new_username} added.", "success")
        elif action == "delete":
            target = conn.execute("SELECT * FROM employees WHERE username=?", (username,)).fetchone()
            if not target:
                flash("Employee not found.", "error")
            elif username == "admin":
                flash("The primary admin account cannot be removed.", "error")
            elif username == session.get("username"):
                flash("You cannot remove your own account while signed in.", "error")
            else:
                conn.execute("DELETE FROM employees WHERE username=?", (username,))
                conn.commit()
                audit("EMPLOYEE_REMOVED", "employee", username, "Removed from branch roster")
                flash(f"Employee {username} removed.", "success")
        else:
            target = conn.execute("SELECT * FROM employees WHERE username=?", (username,)).fetchone()
            if not target:
                flash("Employee not found.", "error")
            elif username == "admin" and action == "role":
                flash("The primary administrator role stays with this account.", "error")
            elif action == "role":
                new_role = request.form.get("role", "")
                if new_role not in ROLE_DESKS:
                    flash("Choose a valid desk.", "error")
                else:
                    department, default_rank = ROLE_DESKS[new_role]
                    rank = request.form.get("rank", "").strip()[:60]
                    if rank:
                        conn.execute(
                            "UPDATE employees SET role=?, department=?, rank=? WHERE username=?",
                            (new_role, department, rank, username),
                        )
                    else:
                        conn.execute(
                            "UPDATE employees SET role=?, department=? WHERE username=?",
                            (new_role, department, username),
                        )
                    conn.commit()
                    audit("ROLE_CHANGED", "employee", username, f"Desk changed to {new_role}")
                    flash(f"Desk updated for {username}.", "success")
            elif action == "password":
                new_password = request.form.get("new_password", "")
                if len(new_password) < 8:
                    flash("Password must contain at least 8 characters.", "error")
                else:
                    conn.execute(
                        "UPDATE employees SET password_hash=? WHERE username=?",
                        (generate_password_hash(new_password), username),
                    )
                    conn.commit()
                    audit("ADMIN_PASSWORD_RESET", "employee", username, "Password reset by admin")
                    flash(f"Password reset for {username}.", "success")
        return redirect(url_for("admin_section"))
    q = search_q()
    emp_sql = "SELECT username,full_name,department,role,rank,email,phone,employee_code FROM employees"
    where, params = sql_like_filter(
        ["username", "full_name", "department", "role", "rank", "email", "phone", "employee_code"],
        q,
        connector="WHERE",
    )
    employees = conn.execute(emp_sql + where + " ORDER BY employee_code", params).fetchall()
    return render_template(
        "admin.html",
        employees=employees,
        role_desks=ROLE_DESKS,
        q=q,
        auto_demo_on=auto_demo_enabled(),
        auto_demo_last_tick=get_setting("auto_demo_last_tick"),
        auto_demo_user=AUTO_DEMO_USER,
    )


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        old = request.form.get("old_password", "")
        new = request.form.get("new_password", "")
        emp = db().execute("SELECT * FROM employees WHERE username=?", (session["username"],)).fetchone()
        if not check_password_hash(emp["password_hash"], old):
            flash("The current password does not match.", "error")
        elif len(new) < 10 or new == DEMO_PASSWORD:
            flash("Use at least 10 characters, and do not reuse the shared demo password.", "error")
        else:
            db().execute(
                "UPDATE employees SET password_hash=? WHERE username=?",
                (generate_password_hash(new), session["username"]),
            )
            audit("PASSWORD_CHANGED", "employee", session["username"], "Password updated")
            flash("Password updated.", "success")
            return redirect(url_for("dashboard"))
    return render_template("change_password.html")


@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/healthz"):
        return jsonify(status="missing"), 404
    return render_template("error.html", message="That page is not part of the branch portal."), 404


@app.errorhandler(500)
def server_error(error):
    LOG.exception("request failed")
    return render_template("error.html", message="The branch system could not complete that action. Try again in a moment."), 500


if app.secret_key == "CHANGE-THIS-DEMO-SECRET-KEY":
    LOG.warning("BANK_SIM_SECRET is not set. Set it before a public deployment.")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    logging.basicConfig(level=logging.INFO)
    print("Central Bank of Rajasthan · Udaipur Main Branch")
    print("IFSC", BRANCH["ifsc"], "· shared demo password:", DEMO_PASSWORD)
    print(f"Open http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("BANK_SIM_DEBUG", "0") == "1")
