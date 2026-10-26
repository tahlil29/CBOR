import os, sqlite3, csv, io, random
from datetime import datetime, date
from functools import wraps
from flask import Flask, g, render_template, request, redirect, url_for, session, flash, Response
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.environ.get("BANK_SIM_DB", os.path.join(BASE, "bank_simulator.db"))
app = Flask(__name__)
app.secret_key = os.environ.get("BANK_SIM_SECRET", "CHANGE-THIS-DEMO-SECRET-KEY")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

EMPLOYEES = [
    ("ankit", "Ankit", "Cash Deposit", "deposit"),
    ("shifa", "Shifa", "Cash Withdrawal", "withdrawal"),
    ("jyoti", "Jyoti", "ATM, Loans & Lockers", "atm"),
    ("tahlil", "Tahlil", "Account Management", "accounts"),
    ("sanket", "Sanket", "Digital Banking & KYC", "kyc"),
    ("bhavesh", "Bhavesh", "Branch Manager", "manager"),
    ("admin", "System Administrator", "Administration", "admin"),
]
DEMO_PASSWORD = "Demo@123"

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.context_processor
def inject_template_globals():
    emp = current_employee() if session.get("username") else None
    return {"today": date.today().strftime("%d %b %Y"), "now_time": datetime.now().strftime("%I:%M %p"), "current_role": emp["department"] if emp else "", "current_employee": emp}

@app.teardown_appcontext
def close_db(error=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS employees(
      username TEXT PRIMARY KEY, full_name TEXT NOT NULL, department TEXT NOT NULL,
      role TEXT NOT NULL, password_hash TEXT NOT NULL,
      rank TEXT NOT NULL DEFAULT 'Officer', email TEXT DEFAULT '', phone TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS customers(
      id INTEGER PRIMARY KEY AUTOINCREMENT, customer_code TEXT UNIQUE NOT NULL,
      full_name TEXT NOT NULL, email TEXT, phone TEXT, city TEXT, occupation TEXT,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS accounts(
      id INTEGER PRIMARY KEY AUTOINCREMENT, account_no TEXT UNIQUE NOT NULL,
      customer_id INTEGER NOT NULL REFERENCES customers(id), account_type TEXT NOT NULL,
      balance INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'Active',
      opened_at TEXT NOT NULL, closed_at TEXT
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
    """)
    # Lightweight migrations for existing demo databases.
    employee_cols = {row[1] for row in conn.execute("PRAGMA table_info(employees)").fetchall()}
    for col, definition in [("rank", "TEXT NOT NULL DEFAULT 'Officer'"), ("email", "TEXT DEFAULT ''"), ("phone", "TEXT DEFAULT ''")]:
        if col not in employee_cols:
            conn.execute(f"ALTER TABLE employees ADD COLUMN {col} {definition}")
    ranks = {"ankit":"Cash Officer", "shifa":"Cash Officer", "jyoti":"Operations Officer",
             "tahlil":"Account Services Officer", "sanket":"Digital Banking Officer",
             "bhavesh":"Branch Manager", "admin":"System Administrator"}
    for username, name, dept, role in EMPLOYEES:
        conn.execute("""INSERT OR IGNORE INTO employees(username,full_name,department,role,password_hash,rank,email,phone)
                     VALUES(?,?,?,?,?,?,?,?)""",
                     (username, name, dept, role, generate_password_hash(DEMO_PASSWORD),
                      ranks.get(username, "Officer"), f"{username}@cbr-udaipur.example.test", ""))
        conn.execute("UPDATE employees SET rank=COALESCE(NULLIF(rank,''),?) WHERE username=?",
                     (ranks.get(username, "Officer"), username))
    atm_rows = [
      ("ATM-01","Main Market",400000,100000), ("ATM-02","Residential Zone A",300000,75000),
      ("ATM-03","Bus Stand",350000,100000), ("ATM-04","Hospital Road",250000,60000),
      ("ATM-05","Industrial Area",300000,75000), ("ATM-06","College Road",200000,50000)
    ]
    for row in atm_rows:
        conn.execute("INSERT OR IGNORE INTO atms(atm_id,location,cash_balance,threshold) VALUES(?,?,?,?)", row)
    for i in range(1, 41):
        conn.execute("INSERT OR IGNORE INTO lockers(locker_id,size,status,rent,updated_at) VALUES(?,?,'Available',?,?)",
                     (f"L-{i:03}", "Small" if i % 3 else ("Large" if i % 2 else "Medium"), 1800 if i % 3 else 3500, now()))
    count = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    if count < 1000:
        first = ["Aarav","Aanya","Vivaan","Anika","Aditya","Diya","Arjun","Sara","Kabir","Meera","Ishaan","Zoya","Rohan","Ira","Reyansh","Aisha","Kunal","Nisha","Farhan","Priya","Dev","Shifa","Ankit","Jyoti","Sanket","Tahlil","Bhavesh","Neha","Rahul","Simran"]
        last = ["Sharma","Verma","Khan","Patel","Singh","Mehta","Joshi","Gupta","Shaikh","Jain","Yadav","Das","Rathore","Choudhary","Malik"]
        cities = ["Udaipur","Jaipur","Ajmer","Nathdwara","Chittorgarh","Rajsamand"]
        occupations = ["Student","Teacher","Shop owner","Engineer","Healthcare","Business","Service","Freelancer"]
        rng = random.Random(20261026)
        for i in range(count + 1, 1001):
            name = f"{rng.choice(first)} {rng.choice(last)}"
            cur = conn.execute("""INSERT INTO customers(customer_code,full_name,email,phone,city,occupation,created_at)
                 VALUES(?,?,?,?,?,?,?)""",
                 (f"CUST-{i:04}", name, f"customer{i}@example.test", f"9{rng.randint(100000000,999999999)}",
                  rng.choice(cities), rng.choice(occupations), now()))
            cid = cur.lastrowid
            bal = rng.randrange(1500, 250000, 500)
            conn.execute("INSERT INTO accounts(account_no,customer_id,account_type,balance,status,opened_at) VALUES(?,?,?,?,?,?)",
                         (f"SIM{100000000+i}", cid, rng.choice(["Savings","Current"]), bal, "Active", now()))
            conn.execute("INSERT INTO kyc_records(customer_id,doc_type,masked_ref,status,created_at) VALUES(?,?,?,?,?)",
                         (cid, "Demo ID", f"DEMO-****-{i:04}", rng.choice(["Verified","Verified","Pending"]), now()))
    conn.commit()
    conn.close()

def audit(action, entity_type, entity_id="", details=""):
    db().execute("INSERT INTO audit_logs(username,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
                 (session.get("username","system"), action, entity_type, str(entity_id), details[:500], now()))
    db().commit()

def current_employee():
    if not session.get("username"):
        return None
    return db().execute("SELECT * FROM employees WHERE username=?", (session["username"],)).fetchone()

def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapped

def roles_required(*roles):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            emp = current_employee()
            if not emp:
                return redirect(url_for("login"))
            if emp["role"] not in roles:
                flash("Your employee role does not have permission for that action.", "error")
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)
        return wrapped
    return decorator

def money(n):
    return "₹{:,.0f}".format(n or 0)
app.jinja_env.filters["money"] = money

@app.route("/")
def home():
    return redirect(url_for("dashboard") if session.get("username") else url_for("login"))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username","").strip().lower()
        password = request.form.get("password","")
        emp = db().execute("SELECT * FROM employees WHERE username=?", (username,)).fetchone()
        if emp and check_password_hash(emp["password_hash"], password):
            session.clear()
            session["username"] = username
            audit("LOGIN","session",username,"Employee signed in")
            return redirect(url_for("dashboard"))
        flash("Invalid demo username or password.", "error")
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    audit("LOGOUT","session",session["username"],"Employee signed out")
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    conn = db()
    stats = {
      "customers": conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
      "accounts": conn.execute("SELECT COUNT(*) FROM accounts WHERE status='Active'").fetchone()[0],
      "deposits": conn.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE txn_type='Deposit' AND date(created_at)=date('now','localtime')").fetchone()[0],
      "withdrawals": conn.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE txn_type='Withdrawal' AND date(created_at)=date('now','localtime')").fetchone()[0],
      "pending": conn.execute("SELECT COUNT(*) FROM cases WHERE status='Pending'").fetchone()[0],
      "complaints": conn.execute("SELECT COUNT(*) FROM complaints WHERE status!='Resolved'").fetchone()[0],
    }
    atms = conn.execute("SELECT * FROM atms ORDER BY atm_id").fetchall()
    recent = conn.execute("""SELECT t.*,a.account_no,c.full_name FROM transactions t JOIN accounts a ON a.id=t.account_id
      JOIN customers c ON c.id=a.customer_id ORDER BY t.id DESC LIMIT 8""").fetchall()
    return render_template("dashboard.html", stats=stats, atms=atms, recent=recent, emp=current_employee(), branch_name="Central Bank of Rajasthan", branch_location="Udaipur Branch")

@app.route("/customers")
@login_required
def customers():
    q = request.args.get("q","").strip()
    if q:
        rows = db().execute("""SELECT c.*,a.account_no,a.balance,a.status account_status FROM customers c
        LEFT JOIN accounts a ON a.customer_id=c.id WHERE c.full_name LIKE ? OR c.customer_code LIKE ? OR a.account_no LIKE ?
        ORDER BY c.id DESC LIMIT 100""", (f"%{q}%",f"%{q}%",f"%{q}%")).fetchall()
    else:
        rows = db().execute("""SELECT c.*,a.account_no,a.balance,a.status account_status FROM customers c
        LEFT JOIN accounts a ON a.customer_id=c.id ORDER BY c.id LIMIT 100""").fetchall()
    return render_template("customers.html", rows=rows, q=q)

@app.route("/transactions", methods=["GET","POST"])
@login_required
def transactions():
    emp = current_employee()
    if request.method == "POST":
        typ = request.form.get("txn_type")
        if typ == "Deposit" and emp["role"] not in ("deposit","manager"):
            flash("Only Cash Deposit staff or the manager can process deposits.", "error"); return redirect(url_for("transactions"))
        if typ == "Withdrawal" and emp["role"] not in ("withdrawal","manager"):
            flash("Only Cash Withdrawal staff or the manager can process withdrawals.", "error"); return redirect(url_for("transactions"))
        account_no = request.form.get("account_no","").strip()
        try: amount = int(request.form.get("amount","0"))
        except ValueError: amount = 0
        account = db().execute("SELECT * FROM accounts WHERE account_no=? AND status='Active'",(account_no,)).fetchone()
        if not account or amount <= 0 or amount > 500000:
            flash("Select an active account and enter a valid amount (₹1–₹5,00,000).", "error"); return redirect(url_for("transactions"))
        if typ not in ("Deposit","Withdrawal"):
            flash("Unsupported transaction type.", "error"); return redirect(url_for("transactions"))
        if typ == "Withdrawal" and account["balance"] < amount:
            flash("Declined: insufficient available balance.", "error"); audit("TRANSACTION_DECLINED","account",account_no,"Insufficient balance"); return redirect(url_for("transactions"))
        conn = db()
        ref = "TXN-" + datetime.now().strftime("%y%m%d%H%M%S%f")
        delta = amount if typ == "Deposit" else -amount
        conn.execute("UPDATE accounts SET balance=balance+? WHERE id=?", (delta, account["id"]))
        conn.execute("INSERT INTO transactions(txn_ref,account_id,txn_type,amount,channel,employee,note,created_at) VALUES(?,?,?,?,?,?,?,?)",
                     (ref,account["id"],typ,amount,"Branch Counter",emp["full_name"],request.form.get("note","").strip(),now()))
        conn.execute("INSERT INTO cash_ledger(direction,amount,source,reference,employee,created_at) VALUES(?,?,?,?,?,?)",
                     ("IN" if typ=="Deposit" else "OUT",amount,"Counter "+typ,ref,emp["full_name"],now()))
        conn.commit()
        audit(typ.upper(),"transaction",ref,f"Account {account_no}; amount {amount}")
        flash(f"{typ} completed. Reference: {ref}", "success")
        return redirect(url_for("transactions"))
    rows = db().execute("""SELECT t.*,a.account_no,c.full_name FROM transactions t JOIN accounts a ON a.id=t.account_id
      JOIN customers c ON c.id=a.customer_id ORDER BY t.id DESC LIMIT 100""").fetchall()
    accounts = db().execute("""SELECT a.account_no,c.full_name,a.balance FROM accounts a JOIN customers c ON c.id=a.customer_id
      WHERE a.status='Active' ORDER BY a.account_no LIMIT 500""").fetchall()
    return render_template("transactions.html", rows=rows, accounts=accounts, emp=emp)

@app.route("/accounts", methods=["GET","POST"])
@login_required
def account_cases():
    emp = current_employee()
    if request.method == "POST":
        if emp["role"] not in ("accounts","manager"):
            flash("Only Account Management or the manager can create account requests.", "error"); return redirect(url_for("account_cases"))
        typ = request.form.get("case_type")
        name = request.form.get("full_name","").strip()
        code = request.form.get("customer_code","").strip()
        details = request.form.get("details","").strip()
        customer = None
        if code:
            customer = db().execute("SELECT * FROM customers WHERE customer_code=?",(code,)).fetchone()
            if not customer:
                flash("Customer code not found.", "error"); return redirect(url_for("account_cases"))
        if typ == "Account Opening":
            if not name:
                flash("Name is required for a new customer.", "error"); return redirect(url_for("account_cases"))
            cur = db().execute("INSERT INTO customers(customer_code,full_name,email,phone,city,occupation,created_at) VALUES(?,?,?,?,?,?,?)",
                ("NEW-"+datetime.now().strftime("%y%m%d%H%M%S%f"),name,request.form.get("email","").strip(),
                 request.form.get("phone","").strip(),"Demo City","Not provided",now()))
            customer_id = cur.lastrowid
        elif not customer:
            flash("Enter a valid customer code for account closure.", "error"); return redirect(url_for("account_cases"))
        else:
            customer_id = customer["id"]
        cur = db().execute("INSERT INTO cases(case_type,customer_id,title,details,status,assigned_to,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (typ,customer_id,typ+" request",details,"Pending",emp["full_name"],now(),now()))
        db().commit(); audit("CREATE_REQUEST","case",cur.lastrowid,typ+" for customer "+str(customer_id))
        flash("Request submitted for manager review.", "success")
        return redirect(url_for("account_cases"))
    cases = db().execute("""SELECT cases.*,customers.customer_code,customers.full_name FROM cases
      LEFT JOIN customers ON customers.id=cases.customer_id WHERE case_type IN ('Account Opening','Account Closing')
      ORDER BY cases.id DESC LIMIT 100""").fetchall()
    return render_template("accounts.html", cases=cases, emp=emp)

@app.route("/kyc", methods=["GET","POST"])
@login_required
def kyc():
    emp = current_employee()
    if request.method == "POST":
        if emp["role"] not in ("kyc","manager"):
            flash("Only Digital Banking & KYC staff or the manager can update KYC.", "error"); return redirect(url_for("kyc"))
        code = request.form.get("customer_code","").strip()
        customer = db().execute("SELECT * FROM customers WHERE customer_code=?",(code,)).fetchone()
        status = request.form.get("status","Pending")
        if not customer or status not in ("Pending","Verified","Rejected"):
            flash("Choose a valid customer and status.", "error"); return redirect(url_for("kyc"))
        doc_type = request.form.get("doc_type","Demo ID")
        masked = request.form.get("masked_ref","DEMO-REFERENCE").strip()[:50]
        cur = db().execute("INSERT INTO kyc_records(customer_id,doc_type,masked_ref,status,checked_by,note,created_at) VALUES(?,?,?,?,?,?,?)",
            (customer["id"],doc_type,masked,status,emp["full_name"],request.form.get("note","").strip(),now()))
        db().commit(); audit("KYC_UPDATE","kyc",cur.lastrowid,f"{code}: {status}")
        flash("KYC record saved.", "success"); return redirect(url_for("kyc"))
    rows = db().execute("""SELECT k.*,c.customer_code,c.full_name FROM kyc_records k JOIN customers c ON c.id=k.customer_id
      ORDER BY k.id DESC LIMIT 100""").fetchall()
    return render_template("kyc.html", rows=rows, emp=emp)

@app.route("/atms", methods=["GET","POST"])
@login_required
def atms():
    emp = current_employee()
    if request.method == "POST":
        if emp["role"] not in ("atm","manager"):
            flash("Only ATM staff or the manager can record ATM movements.", "error"); return redirect(url_for("atms"))
        atm_id = request.form.get("atm_id")
        typ = request.form.get("movement_type")
        try: amount = int(request.form.get("amount","0"))
        except ValueError: amount = 0
        atm = db().execute("SELECT * FROM atms WHERE atm_id=?",(atm_id,)).fetchone()
        if not atm or typ not in ("Replenishment","Cash Collection","Service Update") or amount < 0 or amount > 2000000:
            flash("Invalid ATM movement details.", "error"); return redirect(url_for("atms"))
        if typ == "Service Update":
            status = request.form.get("atm_status","Online")
            if status not in ("Online","Offline","Maintenance"): status = "Online"
            db().execute("UPDATE atms SET status=?,last_service=? WHERE atm_id=?",(status,now(),atm_id))
            amount = 0
        else:
            if emp["role"] != "manager":
                cur = db().execute("INSERT INTO cases(case_type,title,details,status,assigned_to,requested_amount,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                  ("ATM Cash "+typ, f"{typ} request for {atm_id}", request.form.get("note",""), "Pending", emp["full_name"], amount, now(), now()))
                db().commit(); audit("ATM_REQUEST","case",cur.lastrowid,f"{typ} {atm_id} amount {amount}")
                flash("ATM cash movement sent to manager for approval. No cash balance changed yet.", "success")
                return redirect(url_for("atms"))
            if typ == "Replenishment":
                db().execute("UPDATE atms SET cash_balance=cash_balance+? WHERE atm_id=?",(amount,atm_id))
            else:
                if amount > atm["cash_balance"]:
                    flash("Collection cannot exceed ATM's current cash balance.", "error"); return redirect(url_for("atms"))
                db().execute("UPDATE atms SET cash_balance=cash_balance-? WHERE atm_id=?",(amount,atm_id))
        cur = db().execute("INSERT INTO atm_movements(atm_id,movement_type,amount,approved_by,employee,note,created_at) VALUES(?,?,?,?,?,?,?)",
            (atm_id,typ,amount,emp["full_name"] if emp["role"]=="manager" else None,emp["full_name"],request.form.get("note","").strip(),now()))
        db().commit(); audit("ATM_MOVEMENT","atm",atm_id,f"{typ} amount {amount}")
        flash("ATM record updated.", "success"); return redirect(url_for("atms"))
    rows = db().execute("SELECT * FROM atms ORDER BY atm_id").fetchall()
    movements = db().execute("SELECT * FROM atm_movements ORDER BY id DESC LIMIT 50").fetchall()
    pending = db().execute("SELECT * FROM cases WHERE case_type LIKE 'ATM Cash %' AND status='Pending' ORDER BY id DESC").fetchall()
    return render_template("atms.html", rows=rows, movements=movements, pending=pending, emp=emp)

@app.route("/services", methods=["GET","POST"])
@login_required
def services():
    emp = current_employee()
    if request.method == "POST":
        typ = request.form.get("case_type")
        if typ not in ("Loan Application","Locker Request","E-Banking Support","Other Verification"):
            flash("Invalid service type.", "error"); return redirect(url_for("services"))
        if typ == "Loan Application" and emp["role"] not in ("atm","manager"):
            flash("Loan applications are handled by Jyoti or the manager.", "error"); return redirect(url_for("services"))
        if typ == "Locker Request" and emp["role"] not in ("atm","manager"):
            flash("Locker requests are handled by Jyoti or the manager.", "error"); return redirect(url_for("services"))
        if typ in ("E-Banking Support","Other Verification") and emp["role"] not in ("kyc","manager"):
            flash("Digital support is handled by Sanket or the manager.", "error"); return redirect(url_for("services"))
        code = request.form.get("customer_code","").strip()
        customer = db().execute("SELECT * FROM customers WHERE customer_code=?",(code,)).fetchone()
        if not customer:
            flash("Enter a valid customer code.", "error"); return redirect(url_for("services"))
        try: amount = int(request.form.get("amount","0") or 0)
        except ValueError: amount = 0
        cur = db().execute("INSERT INTO cases(case_type,customer_id,title,details,status,assigned_to,requested_amount,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
          (typ,customer["id"],typ+" - "+customer["full_name"],request.form.get("details","").strip(),"Pending",emp["full_name"],amount,now(),now()))
        db().commit(); audit("SERVICE_REQUEST","case",cur.lastrowid,typ+" for "+code)
        flash("Request submitted. Manager approval may be required.", "success")
        return redirect(url_for("services"))
    cases = db().execute("""SELECT cases.*,customers.customer_code,customers.full_name FROM cases
      LEFT JOIN customers ON customers.id=cases.customer_id ORDER BY cases.id DESC LIMIT 100""").fetchall()
    lockers = db().execute("""SELECT l.*,c.customer_code,c.full_name FROM lockers l LEFT JOIN customers c ON c.id=l.customer_id ORDER BY l.locker_id""").fetchall()
    return render_template("services.html", cases=cases, lockers=lockers, emp=emp)

@app.route("/approvals", methods=["GET","POST"])
@login_required
@roles_required("manager")
def approvals():
    if request.method == "POST":
        case_id = request.form.get("case_id")
        decision = request.form.get("decision")
        note = request.form.get("decision_note","").strip()
        case = db().execute("SELECT * FROM cases WHERE id=? AND status='Pending'",(case_id,)).fetchone()
        if not case or decision not in ("Approved","Rejected"):
            flash("Request is no longer pending or the decision is invalid.", "error"); return redirect(url_for("approvals"))
        conn = db()
        if decision == "Approved" and case["case_type"] == "Account Opening":
            cust = conn.execute("SELECT * FROM customers WHERE id=?",(case["customer_id"],)).fetchone()
            exists = conn.execute("SELECT id FROM accounts WHERE customer_id=? AND status='Active'",(case["customer_id"],)).fetchone()
            if not exists:
                conn.execute("INSERT INTO accounts(account_no,customer_id,account_type,balance,status,opened_at) VALUES(?,?,?,?,?,?)",
                  ("SIM"+datetime.now().strftime("%y%m%d%H%M%S")+str(case["customer_id"])[-3:],case["customer_id"],"Savings",0,"Active",now()))
        elif decision == "Approved" and case["case_type"] == "Account Closing":
            acc = conn.execute("SELECT * FROM accounts WHERE customer_id=? AND status='Active' ORDER BY id LIMIT 1",(case["customer_id"],)).fetchone()
            if acc and acc["balance"] != 0:
                flash("Cannot close account: active account must have a zero balance. Resolve balance first.", "error")
                return redirect(url_for("approvals"))
            if acc:
                conn.execute("UPDATE accounts SET status='Closed',closed_at=? WHERE id=?",(now(),acc["id"]))
        elif decision == "Approved" and case["case_type"] == "Locker Request":
            locker = conn.execute("SELECT * FROM lockers WHERE status='Available' ORDER BY locker_id LIMIT 1").fetchone()
            if locker:
                conn.execute("UPDATE lockers SET status='Occupied',customer_id=?,assigned_at=?,updated_at=? WHERE locker_id=?",
                  (case["customer_id"],now(),now(),locker["locker_id"]))
            else:
                flash("No lockers are currently available; request remains pending.", "error")
                return redirect(url_for("approvals"))
        elif decision == "Approved" and case["case_type"] == "ATM Cash Replenishment":
            atm_id = case["title"].split("for ")[-1]
            atm = conn.execute("SELECT * FROM atms WHERE atm_id=?",(atm_id,)).fetchone()
            if atm:
                conn.execute("UPDATE atms SET cash_balance=cash_balance+? WHERE atm_id=?",(case["requested_amount"],atm_id))
                conn.execute("INSERT INTO atm_movements(atm_id,movement_type,amount,approved_by,employee,note,created_at) VALUES(?,?,?,?,?,?,?)",
                  (atm_id,"Replenishment",case["requested_amount"],current_employee()["full_name"],case["assigned_to"],case["details"],now()))
        elif decision == "Approved" and case["case_type"] == "ATM Cash Cash Collection":
            atm_id = case["title"].split("for ")[-1]
            atm = conn.execute("SELECT * FROM atms WHERE atm_id=?",(atm_id,)).fetchone()
            if atm and case["requested_amount"] <= atm["cash_balance"]:
                conn.execute("UPDATE atms SET cash_balance=cash_balance-? WHERE atm_id=?",(case["requested_amount"],atm_id))
                conn.execute("INSERT INTO atm_movements(atm_id,movement_type,amount,approved_by,employee,note,created_at) VALUES(?,?,?,?,?,?,?)",
                  (atm_id,"Cash Collection",case["requested_amount"],current_employee()["full_name"],case["assigned_to"],case["details"],now()))
            elif atm:
                flash("Collection exceeds current ATM balance; request remains pending.", "error")
                return redirect(url_for("approvals"))
        elif decision == "Approved" and case["case_type"] == "Loan Application":
            conn.execute("UPDATE cases SET decision_note=? WHERE id=?",(("Approved for simulation; disbursement is not automatic. "+note).strip(),case_id))
        conn.execute("UPDATE cases SET status=?,decision_note=?,updated_at=? WHERE id=?",(decision,note,now(),case_id))
        conn.commit(); audit("MANAGER_"+decision.upper(),"case",case_id,note)
        flash(f"Request #{case_id} marked {decision.lower()}.", "success")
        return redirect(url_for("approvals"))
    rows = db().execute("""SELECT cases.*,customers.customer_code,customers.full_name FROM cases
      LEFT JOIN customers ON customers.id=cases.customer_id WHERE cases.status='Pending' ORDER BY cases.id ASC""").fetchall()
    return render_template("approvals.html", rows=rows)

@app.route("/complaints", methods=["GET","POST"])
@login_required
def complaints():
    emp = current_employee()
    if request.method == "POST":
        code = request.form.get("customer_code","").strip()
        customer = db().execute("SELECT * FROM customers WHERE customer_code=?",(code,)).fetchone() if code else None
        cur = db().execute("INSERT INTO complaints(customer_id,category,details,status,assigned_to,created_at,updated_at) VALUES(?,?,?,'Open',?,?,?)",
          (customer["id"] if customer else None,request.form.get("category","Other"),request.form.get("details","").strip(),request.form.get("assigned_to",emp["full_name"]),now(),now()))
        db().commit(); audit("COMPLAINT_CREATED","complaint",cur.lastrowid,request.form.get("category","Other"))
        flash("Complaint logged.", "success"); return redirect(url_for("complaints"))
    if request.args.get("resolve") and emp["role"]=="manager":
        cid = request.args.get("resolve")
        db().execute("UPDATE complaints SET status='Resolved',resolution='Resolved by manager',updated_at=? WHERE id=?",(now(),cid))
        db().commit(); audit("COMPLAINT_RESOLVED","complaint",cid,"Resolved by manager")
        flash("Complaint resolved.", "success"); return redirect(url_for("complaints"))
    rows = db().execute("""SELECT complaints.*,customers.customer_code,customers.full_name FROM complaints
      LEFT JOIN customers ON customers.id=complaints.customer_id ORDER BY complaints.id DESC LIMIT 100""").fetchall()
    return render_template("complaints.html", rows=rows, emp=emp)

@app.route("/reports")
@login_required
def reports():
    conn = db()
    txns = conn.execute("""SELECT txn_type,COUNT(*) count,COALESCE(SUM(amount),0) total FROM transactions
      WHERE date(created_at)=date('now','localtime') GROUP BY txn_type""").fetchall()
    cash = conn.execute("""SELECT direction,COALESCE(SUM(amount),0) total FROM cash_ledger
      WHERE date(created_at)=date('now','localtime') GROUP BY direction""").fetchall()
    atm_rows = conn.execute("""SELECT a.*,COALESCE((SELECT SUM(amount) FROM transactions t WHERE t.atm_id=a.atm_id AND t.txn_type='Withdrawal'),0) withdrawals
      FROM atms a ORDER BY a.atm_id""").fetchall()
    case_rows = conn.execute("""SELECT case_type,status,COUNT(*) count FROM cases GROUP BY case_type,status ORDER BY case_type""").fetchall()
    latest = conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 30").fetchall()
    return render_template("reports.html", txns=txns, cash=cash, atm_rows=atm_rows, case_rows=case_rows, latest=latest)

@app.route("/reports/export/<kind>.csv")
@login_required
def export_report(kind):
    conn = db()
    queries = {
      "transactions": ("SELECT txn_ref,txn_type,amount,channel,atm_id,employee,note,created_at FROM transactions ORDER BY id DESC", "transactions.csv"),
      "customers": ("SELECT customer_code,full_name,email,phone,city,occupation,created_at FROM customers ORDER BY id", "customers.csv"),
      "atms": ("SELECT atm_id,location,cash_balance,threshold,status,last_service FROM atms ORDER BY atm_id", "atm_status.csv"),
      "audit": ("SELECT username,action,entity_type,entity_id,details,created_at FROM audit_logs ORDER BY id DESC", "audit_log.csv"),
      "cases": ("SELECT id,case_type,title,status,assigned_to,requested_amount,decision_note,created_at,updated_at FROM cases ORDER BY id DESC", "cases.csv"),
    }
    if kind not in queries:
        flash("Unknown report type.", "error"); return redirect(url_for("reports"))
    sql, filename = queries[kind]
    rows = conn.execute(sql).fetchall()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(rows[0].keys() if rows else [d[0] for d in conn.execute(sql).description])
    writer.writerows([tuple(r) for r in rows])
    audit("REPORT_EXPORTED","report",kind,filename)
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})

@app.route("/audit")
@login_required
@roles_required("manager")
def audit_page():
    rows = db().execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 250").fetchall()
    return render_template("audit.html", rows=rows)

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    emp = current_employee()
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()[:100]
        email = request.form.get("email", "").strip()[:120]
        phone = request.form.get("phone", "").strip()[:30]
        if not full_name:
            flash("Full name is required.", "error")
        else:
            db().execute("UPDATE employees SET full_name=?, email=?, phone=? WHERE username=?",
                         (full_name, email, phone, session["username"]))
            db().commit()
            audit("PROFILE_UPDATED", "employee", session["username"], "Updated employee profile")
            flash("Profile updated.", "success")
            return redirect(url_for("profile"))
    emp = current_employee()
    return render_template("profile.html", emp=emp)

@app.route("/admin", methods=["GET", "POST"])
@login_required
@roles_required("admin")
def admin_section():
    conn = db()
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        action = request.form.get("action", "")
        target = conn.execute("SELECT * FROM employees WHERE username=?", (username,)).fetchone()
        if not target:
            flash("Employee not found.", "error")
        elif username == "admin" and action == "role":
            flash("The primary admin role cannot be changed here.", "error")
        elif action == "role":
            new_role = request.form.get("role", "")
            allowed = {"deposit", "withdrawal", "atm", "accounts", "kyc", "manager", "admin"}
            if new_role not in allowed:
                flash("Choose a valid role.", "error")
            else:
                departments = {"deposit":"Cash Deposit", "withdrawal":"Cash Withdrawal", "atm":"ATM, Loans & Lockers",
                               "accounts":"Account Management", "kyc":"Digital Banking & KYC",
                               "manager":"Branch Manager", "admin":"Administration"}
                conn.execute("UPDATE employees SET role=?, department=? WHERE username=?",
                             (new_role, departments[new_role], username))
                conn.commit()
                audit("ROLE_CHANGED", "employee", username, f"Role changed to {new_role}")
                flash(f"Role updated for {username}.", "success")
        elif action == "password":
            new_password = request.form.get("new_password", "")
            if len(new_password) < 8:
                flash("Password must contain at least 8 characters.", "error")
            else:
                conn.execute("UPDATE employees SET password_hash=? WHERE username=?",
                             (generate_password_hash(new_password), username))
                conn.commit()
                audit("ADMIN_PASSWORD_RESET", "employee", username, "Password reset by admin")
                flash(f"Password reset for {username}.", "success")
        return redirect(url_for("admin_section"))
    employees = conn.execute("SELECT username,full_name,department,role,rank,email,phone FROM employees ORDER BY username").fetchall()
    return render_template("admin.html", employees=employees)

@app.route("/change-password", methods=["GET","POST"])
@login_required
def change_password():
    if request.method == "POST":
        old = request.form.get("old_password","")
        new = request.form.get("new_password","")
        emp = db().execute("SELECT * FROM employees WHERE username=?",(session["username"],)).fetchone()
        if not check_password_hash(emp["password_hash"],old):
            flash("Current password did not match.", "error")
        elif len(new) < 10:
            flash("Use at least 10 characters for the new password.", "error")
        else:
            db().execute("UPDATE employees SET password_hash=? WHERE username=?",(generate_password_hash(new),session["username"]))
            db().commit(); audit("PASSWORD_CHANGED","employee",session["username"],"Password updated")
            flash("Password updated.", "success"); return redirect(url_for("dashboard"))
    return render_template("change_password.html")

@app.errorhandler(500)
def server_error(error):
    return render_template("error.html", message="Unexpected application error. Check the terminal output for details."), 500

if __name__ == "__main__":
    init_db()
    print("\\nBANK BRANCH & ATM SIMULATOR")
    print("Demo login password for all users:", DEMO_PASSWORD)
    print("Open http://127.0.0.1:5000")
    app.run(debug=os.environ.get("BANK_SIM_DEBUG","0") == "1")
