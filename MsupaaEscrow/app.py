import csv
import io
import os
import secrets
import sqlite3
import uuid
import click
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (
    Flask, Response, abort, flash, g, redirect, render_template,
    request, session, url_for
)
from werkzeug.security import check_password_hash, generate_password_hash

APP_NAME = "MsupaaEscrow"

app = Flask(__name__, instance_relative_config=True)
os.makedirs(app.instance_path, exist_ok=True)
app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY", "dev-secret-change-before-production"),
    DATABASE=os.getenv("MSUPAA_DATABASE", os.path.join(app.instance_path, "msupaaescrow.db")),
    AUTO_RELEASE_DAYS=int(os.getenv("AUTO_RELEASE_DAYS", "3")),
)

def utcnow():
    return datetime.now(timezone.utc)

def iso_now():
    return utcnow().isoformat()

def parse_iso(value):
    return datetime.fromisoformat(value) if value else None

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('buyer','seller','admin')),
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL UNIQUE,
        buyer_id INTEGER NOT NULL REFERENCES users(id),
        seller_id INTEGER NOT NULL REFERENCES users(id),
        item_name TEXT NOT NULL,
        description TEXT,
        amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
        currency TEXT NOT NULL DEFAULT 'KES',
        status TEXT NOT NULL CHECK(status IN (
            'PENDING_PAYMENT','HELD','SHIPPED','DISPUTED','RELEASED','REFUNDED'
        )),
        tracking_number TEXT,
        created_at TEXT NOT NULL,
        funded_at TEXT,
        shipped_at TEXT,
        delivered_at TEXT,
        auto_release_at TEXT
    );

    CREATE TABLE IF NOT EXISTS disputes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL REFERENCES orders(id),
        opened_by INTEGER NOT NULL REFERENCES users(id),
        reason TEXT NOT NULL,
        details TEXT,
        status TEXT NOT NULL CHECK(status IN ('OPEN','RESOLVED')),
        resolution TEXT,
        admin_note TEXT,
        created_at TEXT NOT NULL,
        resolved_at TEXT
    );

    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL REFERENCES orders(id),
        event_type TEXT NOT NULL,
        amount_cents INTEGER NOT NULL DEFAULT 0,
        note TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        is_read INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER REFERENCES users(id),
        action TEXT NOT NULL,
        entity_type TEXT,
        entity_id TEXT,
        metadata TEXT,
        created_at TEXT NOT NULL
    );
    """)
    db.commit()

def money(cents, currency="KES"):
    return f"{currency} {cents / 100:,.2f}"

app.jinja_env.globals["money"] = money
app.jinja_env.globals["APP_NAME"] = APP_NAME

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return get_db().execute(
        "SELECT id,name,email,role,created_at FROM users WHERE id=?", (uid,)
    ).fetchone()

@app.context_processor
def inject_globals():
    user = current_user()
    unread = 0
    if user:
        unread = get_db().execute(
            "SELECT COUNT(*) c FROM notifications WHERE user_id=? AND is_read=0",
            (user["id"],)
        ).fetchone()["c"]
    return {"current_user": user, "unread_notifications": unread, "csrf_token": session.get("_csrf_token", "")}

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please sign in to continue.", "warning")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped

def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            user = current_user()
            if user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator

def notify(user_id, title, message):
    get_db().execute(
        "INSERT INTO notifications(user_id,title,message,is_read,created_at) VALUES (?,?,?,?,?)",
        (user_id, title, message, 0, iso_now())
    )

def record_transaction(order_id, event_type, amount_cents=0, note=""):
    get_db().execute(
        "INSERT INTO transactions(order_id,event_type,amount_cents,note,created_at) VALUES (?,?,?,?,?)",
        (order_id, event_type, amount_cents, note, iso_now())
    )

def audit(action, entity_type=None, entity_id=None, metadata=""):
    get_db().execute(
        "INSERT INTO audit_logs(user_id,action,entity_type,entity_id,metadata,created_at) VALUES (?,?,?,?,?,?)",
        (session.get("user_id"), action, entity_type,
         str(entity_id) if entity_id is not None else None, metadata, iso_now())
    )

def get_order(public_id):
    return get_db().execute("""
        SELECT o.*,
               b.name buyer_name, b.email buyer_email,
               s.name seller_name, s.email seller_email
        FROM orders o
        JOIN users b ON b.id=o.buyer_id
        JOIN users s ON s.id=o.seller_id
        WHERE o.public_id=?
    """, (public_id,)).fetchone()

def can_access_order(user, order):
    return (
        user["role"] == "admin" or
        (user["role"] == "buyer" and order["buyer_id"] == user["id"]) or
        (user["role"] == "seller" and order["seller_id"] == user["id"])
    )

def order_scope_sql(user, alias="o"):
    if user["role"] == "admin":
        return "1=1", ()
    if user["role"] == "buyer":
        return f"{alias}.buyer_id=?", (user["id"],)
    return f"{alias}.seller_id=?", (user["id"],)

def auto_release_due_orders():
    db = get_db()
    now = utcnow()
    rows = db.execute("""
        SELECT o.*, b.name buyer_name, s.name seller_name
        FROM orders o
        JOIN users b ON b.id=o.buyer_id
        JOIN users s ON s.id=o.seller_id
        WHERE o.status='SHIPPED' AND o.auto_release_at IS NOT NULL
    """).fetchall()

    changed = False
    for order in rows:
        due = parse_iso(order["auto_release_at"])
        if due and due <= now:
            if db.execute("SELECT 1 FROM disputes WHERE order_id=? AND status='OPEN'", (order["id"],)).fetchone():
                continue
            db.execute(
                "UPDATE orders SET status='RELEASED', delivered_at=? WHERE id=?",
                (iso_now(), order["id"])
            )
            record_transaction(order["id"], "AUTO_RELEASE", order["amount_cents"],
                               "Funds automatically released after the release timer expired.")
            notify(order["buyer_id"], "Escrow automatically released",
                   f"{order['public_id']} reached its release deadline. Funds were released.")
            notify(order["seller_id"], "Funds released",
                   f"{money(order['amount_cents'])} was automatically released for {order['public_id']}.")
            changed = True
    if changed:
        db.commit()

@app.before_request
def before_request():
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_urlsafe(32)
    if request.method == "POST":
        submitted = request.form.get("_csrf", "")
        if not secrets.compare_digest(submitted, session.get("_csrf_token", "")):
            abort(400, description="Invalid or missing CSRF token.")
    if not request.path.startswith("/static/"):
        auto_release_due_orders()

@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template("index.html")

@app.route("/register", methods=["GET","POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        name = request.form.get("name","").strip()
        email = request.form.get("email","").strip().lower()
        password = request.form.get("password","")
        role = request.form.get("role","")
        errors = []
        if len(name) < 2: errors.append("Name must be at least 2 characters.")
        if "@" not in email: errors.append("Enter a valid email address.")
        if len(password) < 8: errors.append("Password must be at least 8 characters.")
        if role not in {"buyer","seller"}: errors.append("Choose Buyer or Seller.")
        db = get_db()
        if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
            errors.append("An account with that email already exists.")
        if errors:
            for e in errors: flash(e, "danger")
            return render_template("register.html")
        cur = db.execute(
            "INSERT INTO users(name,email,password_hash,role,created_at) VALUES (?,?,?,?,?)",
            (name, email, generate_password_hash(password), role, iso_now())
        )
        db.commit()
        session.clear()
        session["user_id"] = cur.lastrowid
        session["_csrf_token"] = secrets.token_urlsafe(32)
        audit("REGISTER", "user", cur.lastrowid, role)
        db.commit()
        flash(f"Welcome to {APP_NAME}. Your account is ready.", "success")
        return redirect(url_for("dashboard"))
    return render_template("register.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email","").strip().lower()
        password = request.form.get("password","")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Incorrect email or password.", "danger")
            return render_template("login.html")
        session.clear()
        session["user_id"] = user["id"]
        session["_csrf_token"] = secrets.token_urlsafe(32)
        audit("LOGIN", "user", user["id"])
        db.commit()
        flash(f"Welcome back, {user['name']}.", "success")
        return redirect(request.args.get("next") or url_for("dashboard"))
    return render_template("login.html")

@app.post("/logout")
@login_required
def logout():
    user = current_user()
    audit("LOGOUT", "user", user["id"])
    get_db().commit()
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    where_sql, params = order_scope_sql(user)
    db = get_db()
    orders = db.execute(f"""
        SELECT o.*, b.name buyer_name, s.name seller_name
        FROM orders o
        JOIN users b ON b.id=o.buyer_id
        JOIN users s ON s.id=o.seller_id
        WHERE {where_sql}
        ORDER BY o.created_at DESC LIMIT 8
    """, params).fetchall()
    stats = db.execute(f"""
        SELECT COUNT(*) total,
          COALESCE(SUM(CASE WHEN status IN ('HELD','SHIPPED','DISPUTED') THEN amount_cents ELSE 0 END),0) held,
          COALESCE(SUM(CASE WHEN status='RELEASED' THEN amount_cents ELSE 0 END),0) released,
          COALESCE(SUM(CASE WHEN status='REFUNDED' THEN amount_cents ELSE 0 END),0) refunded
        FROM orders o WHERE {where_sql}
    """, params).fetchone()
    if user["role"] == "admin":
        open_disputes = db.execute("SELECT COUNT(*) c FROM disputes WHERE status='OPEN'").fetchone()["c"]
    else:
        open_disputes = db.execute(f"""
            SELECT COUNT(*) c FROM disputes d
            JOIN orders o ON o.id=d.order_id
            WHERE d.status='OPEN' AND {where_sql}
        """, params).fetchone()["c"]
    return render_template("dashboard.html", orders=orders, stats=stats, open_disputes=open_disputes)

@app.route("/orders")
@login_required
def orders():
    user = current_user()
    where_sql, params = order_scope_sql(user)
    rows = get_db().execute(f"""
        SELECT o.*, b.name buyer_name, s.name seller_name
        FROM orders o
        JOIN users b ON b.id=o.buyer_id
        JOIN users s ON s.id=o.seller_id
        WHERE {where_sql}
        ORDER BY o.created_at DESC
    """, params).fetchall()
    return render_template("orders.html", orders=rows)

@app.route("/orders/new", methods=["GET","POST"])
@roles_required("buyer")
def new_order():
    db = get_db()
    sellers = db.execute("SELECT id,name,email FROM users WHERE role='seller' ORDER BY name").fetchall()
    if request.method == "POST":
        item_name = request.form.get("item_name","").strip()
        description = request.form.get("description","").strip()
        seller_id = request.form.get("seller_id", type=int)
        amount = request.form.get("amount","").strip()
        try:
            amount_cents = int(round(float(amount)*100))
        except (ValueError, TypeError):
            amount_cents = 0
        seller = db.execute("SELECT id,name FROM users WHERE id=? AND role='seller'", (seller_id,)).fetchone()
        if not item_name or amount_cents <= 0 or not seller:
            flash("Enter an item, a valid amount, and a seller.", "danger")
            return render_template("new_order.html", sellers=sellers)
        public_id = "ME-" + uuid.uuid4().hex[:8].upper()
        user = current_user()
        cur = db.execute("""
            INSERT INTO orders(public_id,buyer_id,seller_id,item_name,description,amount_cents,currency,status,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (public_id,user["id"],seller_id,item_name,description,amount_cents,"KES","PENDING_PAYMENT",iso_now()))
        record_transaction(cur.lastrowid, "ORDER_CREATED", 0, f"Order created by {user['name']}.")
        notify(seller_id, "New escrow order",
               f"{user['name']} created {public_id} for {money(amount_cents)}. Waiting for buyer funding.")
        audit("CREATE_ORDER","order",public_id,item_name)
        db.commit()
        flash("Order created. Fund it to place the money in escrow.", "success")
        return redirect(url_for("order_detail", public_id=public_id))
    return render_template("new_order.html", sellers=sellers)

@app.route("/orders/<public_id>")
@login_required
def order_detail(public_id):
    order = get_order(public_id)
    if not order: abort(404)
    user = current_user()
    if not can_access_order(user, order): abort(403)
    db = get_db()
    transactions = db.execute("SELECT * FROM transactions WHERE order_id=? ORDER BY created_at DESC",
                              (order["id"],)).fetchall()
    disputes = db.execute("""
        SELECT d.*, u.name opened_by_name FROM disputes d
        JOIN users u ON u.id=d.opened_by
        WHERE d.order_id=? ORDER BY d.created_at DESC
    """, (order["id"],)).fetchall()
    return render_template("order_detail.html", order=order, transactions=transactions, disputes=disputes)

@app.post("/orders/<public_id>/fund")
@roles_required("buyer")
def fund_order(public_id):
    order = get_order(public_id); user = current_user()
    if not order: abort(404)
    if order["buyer_id"] != user["id"]: abort(403)
    if order["status"] != "PENDING_PAYMENT":
        flash("This order cannot be funded in its current state.", "warning")
        return redirect(url_for("order_detail", public_id=public_id))
    db = get_db()
    db.execute("UPDATE orders SET status='HELD', funded_at=? WHERE id=?", (iso_now(),order["id"]))
    record_transaction(order["id"],"ESCROW_HOLD",order["amount_cents"],
                       "Test payment accepted and funds moved to HELD status.")
    notify(order["seller_id"],"Escrow funded",
           f"{public_id} is funded. {money(order['amount_cents'])} is now held in escrow.")
    notify(order["buyer_id"],"Payment held safely",
           f"{money(order['amount_cents'])} is now held for {public_id}.")
    audit("FUND_ORDER","order",public_id,"test-payment")
    db.commit()
    flash("Test payment completed. Funds are now HELD in escrow.", "success")
    return redirect(url_for("order_detail", public_id=public_id))

@app.post("/orders/<public_id>/ship")
@roles_required("seller")
def ship_order(public_id):
    order = get_order(public_id); user = current_user()
    if not order: abort(404)
    if order["seller_id"] != user["id"]: abort(403)
    if order["status"] != "HELD":
        flash("Only funded/held orders can be marked as shipped.", "warning")
        return redirect(url_for("order_detail", public_id=public_id))
    tracking = request.form.get("tracking_number","").strip() or "Not provided"
    shipped = utcnow()
    release_at = shipped + timedelta(days=app.config["AUTO_RELEASE_DAYS"])
    db = get_db()
    db.execute("""UPDATE orders SET status='SHIPPED',tracking_number=?,shipped_at=?,auto_release_at=? WHERE id=?""",
               (tracking,shipped.isoformat(),release_at.isoformat(),order["id"]))
    record_transaction(order["id"],"ORDER_SHIPPED",0,
                       f"Tracking: {tracking}. Auto-release scheduled for {release_at.isoformat()}.")
    notify(order["buyer_id"],"Your order was shipped",
           f"{public_id} was marked as shipped. Tracking: {tracking}.")
    audit("SHIP_ORDER","order",public_id,tracking)
    db.commit()
    flash("Order marked as shipped. The buyer can now confirm delivery.", "success")
    return redirect(url_for("order_detail", public_id=public_id))

@app.post("/orders/<public_id>/confirm")
@roles_required("buyer")
def confirm_delivery(public_id):
    order = get_order(public_id); user = current_user()
    if not order: abort(404)
    if order["buyer_id"] != user["id"]: abort(403)
    if order["status"] != "SHIPPED":
        flash("Delivery can only be confirmed after shipment.", "warning")
        return redirect(url_for("order_detail", public_id=public_id))
    db = get_db()
    db.execute("UPDATE orders SET status='RELEASED',delivered_at=? WHERE id=?", (iso_now(),order["id"]))
    record_transaction(order["id"],"ESCROW_RELEASE",order["amount_cents"],
                       "Buyer confirmed delivery; funds released to seller.")
    notify(order["seller_id"],"Funds released",f"{money(order['amount_cents'])} was released for {public_id}.")
    notify(order["buyer_id"],"Transaction completed",f"{public_id} is complete. Funds were released.")
    audit("CONFIRM_DELIVERY","order",public_id)
    db.commit()
    flash("Delivery confirmed. Escrow funds have been released.", "success")
    return redirect(url_for("order_detail", public_id=public_id))

@app.post("/orders/<public_id>/dispute")
@login_required
def raise_dispute(public_id):
    order = get_order(public_id); user = current_user()
    if not order: abort(404)
    if not can_access_order(user, order) or user["role"] == "admin": abort(403)
    if order["status"] not in {"HELD","SHIPPED"}:
        flash("A dispute cannot be opened for this order state.", "warning")
        return redirect(url_for("order_detail", public_id=public_id))
    reason = request.form.get("reason","").strip()
    details = request.form.get("details","").strip()
    if len(reason) < 3:
        flash("Enter a clear dispute reason.", "danger")
        return redirect(url_for("order_detail", public_id=public_id))
    db = get_db()
    if db.execute("SELECT id FROM disputes WHERE order_id=? AND status='OPEN'",(order["id"],)).fetchone():
        flash("This order already has an open dispute.", "warning")
        return redirect(url_for("order_detail", public_id=public_id))
    db.execute("INSERT INTO disputes(order_id,opened_by,reason,details,status,created_at) VALUES (?,?,?,?,?,?)",
               (order["id"],user["id"],reason,details,"OPEN",iso_now()))
    db.execute("UPDATE orders SET status='DISPUTED' WHERE id=?",(order["id"],))
    record_transaction(order["id"],"DISPUTE_OPENED",0,f"Dispute opened by {user['name']}: {reason}")
    notify(order["buyer_id"],"Order disputed",f"{public_id} is under dispute. Funds remain locked.")
    notify(order["seller_id"],"Order disputed",f"{public_id} is under dispute. Funds remain locked.")
    for admin in db.execute("SELECT id FROM users WHERE role='admin'").fetchall():
        notify(admin["id"],"New dispute requires review",f"{public_id}: {reason}")
    audit("OPEN_DISPUTE","order",public_id,reason)
    db.commit()
    flash("Dispute opened. Funds are locked until resolution.", "success")
    return redirect(url_for("order_detail", public_id=public_id))

@app.route("/disputes")
@login_required
def disputes():
    user = current_user(); db = get_db()
    if user["role"] == "admin":
        rows = db.execute("""
            SELECT d.*,o.public_id,o.item_name,o.amount_cents,o.currency,
                   b.name buyer_name,s.name seller_name,u.name opened_by_name
            FROM disputes d
            JOIN orders o ON o.id=d.order_id
            JOIN users b ON b.id=o.buyer_id
            JOIN users s ON s.id=o.seller_id
            JOIN users u ON u.id=d.opened_by
            ORDER BY CASE d.status WHEN 'OPEN' THEN 0 ELSE 1 END,d.created_at DESC
        """).fetchall()
    else:
        where_sql, params = order_scope_sql(user)
        rows = db.execute(f"""
            SELECT d.*,o.public_id,o.item_name,o.amount_cents,o.currency,
                   b.name buyer_name,s.name seller_name,u.name opened_by_name
            FROM disputes d
            JOIN orders o ON o.id=d.order_id
            JOIN users b ON b.id=o.buyer_id
            JOIN users s ON s.id=o.seller_id
            JOIN users u ON u.id=d.opened_by
            WHERE {where_sql}
            ORDER BY CASE d.status WHEN 'OPEN' THEN 0 ELSE 1 END,d.created_at DESC
        """, params).fetchall()
    return render_template("disputes.html", disputes=rows)

@app.post("/disputes/<int:dispute_id>/resolve")
@roles_required("admin")
def resolve_dispute(dispute_id):
    action = request.form.get("resolution","")
    note = request.form.get("admin_note","").strip()
    if action not in {"REFUND_BUYER","RELEASE_SELLER"}: abort(400)
    db = get_db()
    dispute = db.execute("""
        SELECT d.*,o.public_id,o.amount_cents,o.buyer_id,o.seller_id,o.status order_status
        FROM disputes d JOIN orders o ON o.id=d.order_id WHERE d.id=?
    """,(dispute_id,)).fetchone()
    if not dispute: abort(404)
    if dispute["status"] != "OPEN":
        flash("This dispute has already been resolved.", "warning")
        return redirect(url_for("disputes"))
    if action == "REFUND_BUYER":
        new_status,event,message = "REFUNDED","ESCROW_REFUND","Funds refunded to buyer."
    else:
        new_status,event,message = "RELEASED","ESCROW_RELEASE","Funds released to seller."
    db.execute("UPDATE orders SET status=? WHERE id=?",(new_status,dispute["order_id"]))
    db.execute("""UPDATE disputes SET status='RESOLVED',resolution=?,admin_note=?,resolved_at=? WHERE id=?""",
               (action,note,iso_now(),dispute_id))
    record_transaction(dispute["order_id"],event,dispute["amount_cents"],
                       f"Administrator resolution: {message} {note}".strip())
    notify(dispute["buyer_id"],"Dispute resolved",f"{dispute['public_id']}: {message}")
    notify(dispute["seller_id"],"Dispute resolved",f"{dispute['public_id']}: {message}")
    audit("RESOLVE_DISPUTE","dispute",dispute_id,action)
    db.commit()
    flash(f"Dispute resolved. {message}", "success")
    return redirect(url_for("disputes"))

@app.route("/notifications")
@login_required
def notifications():
    user = current_user(); db = get_db()
    rows = db.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 100",
                      (user["id"],)).fetchall()
    db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?",(user["id"],))
    db.commit()
    return render_template("notifications.html", notifications=rows)

@app.post("/notifications/clear")
@login_required
def clear_notifications():
    user = current_user()
    get_db().execute("DELETE FROM notifications WHERE user_id=?",(user["id"],))
    get_db().commit()
    flash("Notifications cleared.", "info")
    return redirect(url_for("notifications"))

@app.route("/reports")
@login_required
def reports():
    user = current_user()
    where_sql, params = order_scope_sql(user)
    db = get_db()
    rows = db.execute(f"""
        SELECT t.*,o.public_id,o.item_name,o.currency
        FROM transactions t JOIN orders o ON o.id=t.order_id
        WHERE {where_sql}
        ORDER BY t.created_at DESC LIMIT 300
    """, params).fetchall()
    totals = db.execute(f"""
        SELECT
          COALESCE(SUM(CASE WHEN o.status IN ('HELD','SHIPPED','DISPUTED') THEN o.amount_cents ELSE 0 END),0) held,
          COALESCE(SUM(CASE WHEN o.status='RELEASED' THEN o.amount_cents ELSE 0 END),0) released,
          COALESCE(SUM(CASE WHEN o.status='REFUNDED' THEN o.amount_cents ELSE 0 END),0) refunded,
          COUNT(*) orders
        FROM orders o WHERE {where_sql}
    """, params).fetchone()
    return render_template("reports.html", transactions=rows, totals=totals)

@app.route("/reports.csv")
@login_required
def reports_csv():
    user = current_user()
    where_sql, params = order_scope_sql(user)
    rows = get_db().execute(f"""
        SELECT o.public_id,o.item_name,o.status,o.amount_cents,o.currency,
               t.event_type,t.amount_cents event_amount,t.note,t.created_at
        FROM transactions t JOIN orders o ON o.id=t.order_id
        WHERE {where_sql}
        ORDER BY t.created_at DESC
    """, params).fetchall()
    buf = io.StringIO(); writer = csv.writer(buf)
    writer.writerow(["Order ID","Item","Order Status","Order Amount","Currency","Event","Event Amount","Note","Timestamp"])
    for row in rows:
        writer.writerow([row["public_id"],row["item_name"],row["status"],
                         f"{row['amount_cents']/100:.2f}",row["currency"],row["event_type"],
                         f"{row['event_amount']/100:.2f}",row["note"] or "",row["created_at"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":"attachment; filename=msupaaescrow-report.csv"})

@app.route("/admin/users")
@roles_required("admin")
def admin_users():
    rows = get_db().execute("""
        SELECT u.*,(SELECT COUNT(*) FROM orders o WHERE o.buyer_id=u.id OR o.seller_id=u.id) order_count
        FROM users u ORDER BY u.created_at DESC
    """).fetchall()
    return render_template("admin_users.html", users=rows)

@app.errorhandler(400)
def bad_request(error):
    return render_template("error.html", code=400, message=getattr(error,"description","Bad request.")),400

@app.errorhandler(403)
def forbidden(_error):
    return render_template("error.html", code=403, message="You do not have permission to access this page."),403

@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", code=404, message="The requested page or record was not found."),404

@app.cli.command("create-admin")
@click.option("--name", prompt="Administrator name")
@click.option("--email", prompt="Administrator email")
@click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
def create_admin(name, email, password):
    """Create the first administrator account without any seeded credentials."""
    name = name.strip()
    email = email.strip().lower()
    if len(name) < 2:
        raise click.ClickException("Name must be at least 2 characters.")
    if "@" not in email:
        raise click.ClickException("Enter a valid email address.")
    if len(password) < 8:
        raise click.ClickException("Password must be at least 8 characters.")

    db = get_db()
    if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
        raise click.ClickException("An account with that email already exists.")

    db.execute(
        "INSERT INTO users(name,email,password_hash,role,created_at) VALUES (?,?,?,?,?)",
        (name, email, generate_password_hash(password), "admin", iso_now())
    )
    db.commit()
    click.echo(f"Administrator account created for {email}.")

with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT","5000")), debug=os.getenv("FLASK_DEBUG","0")=="1")
