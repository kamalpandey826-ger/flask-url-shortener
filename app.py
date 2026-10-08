from flask import Flask, request, redirect, render_template, render_template_string, send_file, session
import secrets
import time
import string
import sqlite3
import os
import csv
import io
import html
import ipaddress
import threading
from datetime import datetime
from urllib.parse import urlparse
from functools import wraps
from werkzeug.security import check_password_hash

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

app = Flask(__name__)

app.secret_key = os.getenv("SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("SECRET_KEY must be set")

# Harden the authentication session cookie.
# Secure is enabled only when HTTPS is explicitly configured.
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"

DATABASE = os.getenv(
    "DATABASE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "links.db")
)

LOG_KEEP_DAYS = 30
MAX_URL_LENGTH = 2048
MAX_BULK_URLS = 100

ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:5000").rstrip("/") + "/"

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 600
_login_failures = {}

LINK_CREATE_MAX_REQUESTS = 10
LINK_CREATE_WINDOW_SECONDS = 60
LINK_CREATE_MAX_TRACKED_ADDRESSES = 10000
BULK_CREATE_MAX_URLS = 300
_link_creation_requests = {}
_bulk_creation_requests = {}
_link_creation_lock = threading.Lock()


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not ADMIN_PASSWORD_HASH:
            return "Admin authentication is not configured.", 503

        if not session.get("admin_authenticated"):
            return redirect("/login")

        return view(*args, **kwargs)

    return wrapped


def get_trusted_proxies():
    value = os.getenv("TRUSTED_PROXIES", "127.0.0.1,::1")
    networks = []

    for item in value.split(","):
        candidate = item.strip()
        if not candidate:
            continue

        try:
            if "/" in candidate:
                networks.append(ipaddress.ip_network(candidate, strict=False))
                continue

            ip = ipaddress.ip_address(candidate)
            mask = 32 if ip.version == 4 else 128
            networks.append(ipaddress.ip_network(f"{ip}/{mask}", strict=False))
        except ValueError:
            continue

    return networks or [
        ipaddress.ip_network("127.0.0.1/32"),
        ipaddress.ip_network("::1/128"),
    ]


def get_client_ip():
    remote_addr = request.remote_addr or "unknown"

    try:
        remote_ip = ipaddress.ip_address(remote_addr)
    except ValueError:
        return remote_addr

    if not any(remote_ip in network for network in get_trusted_proxies()):
        return remote_addr

    x_real_ip = request.headers.get("X-Real-IP")
    if not x_real_ip:
        return str(remote_ip)

    try:
        return str(ipaddress.ip_address(x_real_ip))
    except ValueError:
        return str(remote_ip)


def login_rate_limited(ip):
    now = time.monotonic()
    failures = _login_failures.get(ip, [])

    failures = [
        timestamp
        for timestamp in failures
        if now - timestamp < LOGIN_WINDOW_SECONDS
    ]

    _login_failures[ip] = failures
    return len(failures) >= LOGIN_MAX_FAILURES


def record_login_failure(ip):
    now = time.monotonic()
    failures = _login_failures.get(ip, [])

    failures = [
        timestamp
        for timestamp in failures
        if now - timestamp < LOGIN_WINDOW_SECONDS
    ]

    failures.append(now)
    _login_failures[ip] = failures


def clear_login_failures(ip):
    _login_failures.pop(ip, None)


def prune_rate_limit_store(store, now, window_seconds):
    for ip, timestamps in list(store.items()):
        filtered = [
            timestamp
            for timestamp in timestamps
            if now - timestamp < window_seconds
        ]

        if filtered:
            store[ip] = filtered
        else:
            del store[ip]

    if len(store) > LINK_CREATE_MAX_TRACKED_ADDRESSES:
        oldest_ips = sorted(
            store,
            key=lambda key: min(store[key])
        )[:len(store) - LINK_CREATE_MAX_TRACKED_ADDRESSES]

        for ip in oldest_ips:
            del store[ip]


def rate_limit_request(ip, store, max_requests, window_seconds, count=1):
    now = time.monotonic()

    with _link_creation_lock:
        prune_rate_limit_store(store, now, window_seconds)

        timestamps = list(store.get(ip, []))
        timestamps = [
            timestamp
            for timestamp in timestamps
            if now - timestamp < window_seconds
        ]

        if len(timestamps) + count > max_requests:
            store[ip] = timestamps
            prune_rate_limit_store(store, now, window_seconds)
            if timestamps:
                retry_after = max(1, int((min(timestamps) + window_seconds) - now))
            else:
                retry_after = 1
            return True, retry_after

        for _ in range(count):
            timestamps.append(now)

        store[ip] = timestamps
        prune_rate_limit_store(store, now, window_seconds)
        return False, 0


def validate_csrf():
    token = session.get("csrf_token")
    submitted = request.form.get("csrf_token", "")

    if not token or not submitted:
        return False

    return secrets.compare_digest(token, submitted)


def validate_url(value):
    """Return a cleaned HTTP(S) URL, or None if it is not acceptable."""
    value = value.strip()

    if not value or len(value) > MAX_URL_LENGTH:
        return None

    parsed = urlparse(value)

    if parsed.scheme.lower() not in {"http", "https"}:
        return None

    if not parsed.hostname:
        return None

    return value


def get_db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def create_database():
    """Create all tables, and upgrade older databases in place."""

    connection = get_db()

    connection.execute("""
        CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            url TEXT NOT NULL,
            clicks INTEGER DEFAULT 0,
            created_at TEXT,
            batch_id TEXT
        )
    """)

    # Databases made by older versions of this app have no batch_id column.
    columns = [row["name"] for row in connection.execute("PRAGMA table_info(links)")]
    if "batch_id" not in columns:
        connection.execute("ALTER TABLE links ADD COLUMN batch_id TEXT")

    connection.execute("""
        CREATE TABLE IF NOT EXISTS access_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip_address TEXT,
            method TEXT,
            path TEXT,
            status_code INTEGER,
            user_agent TEXT,
            created_at TEXT
        )
    """)

    connection.commit()
    connection.close()


@app.after_request
def log_request(response):
    """Record each request. A logging problem must never break the response."""

    try:
        connection = get_db()

        try:
            cursor = connection.execute(
                """
                INSERT INTO access_logs
                (ip_address, method, path, status_code, user_agent, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    get_client_ip()[:100],
                    request.method,
                    request.path[:200],
                    response.status_code,
                    request.headers.get("User-Agent", "")[:200],
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                )
            )

            # Every 500th log row, delete rows older than LOG_KEEP_DAYS.
            if cursor.lastrowid % 500 == 0:
                connection.execute(
                    "DELETE FROM access_logs "
                    "WHERE created_at < datetime('now', 'localtime', ?)",
                    (f"-{LOG_KEEP_DAYS} days",)
                )

            connection.commit()

        finally:
            connection.close()

    except sqlite3.Error:
        app.logger.exception("could not write the access log")

    return response


def generate_code(length=6):

    characters = string.ascii_letters + string.digits

    return "".join(
        secrets.choice(characters)
        for _ in range(length)
    )



@app.route("/", methods=["GET", "POST"])
def home():

    short_url = None

    if request.method == "POST":

        ip = get_client_ip()
        blocked, retry_after = rate_limit_request(
            ip,
            _link_creation_requests,
            LINK_CREATE_MAX_REQUESTS,
            LINK_CREATE_WINDOW_SECONDS,
            count=1,
        )
        if blocked:
            return (
                "Too many link creation requests. Try again later.",
                429,
                {"Retry-After": str(retry_after)},
            )

        long_url = validate_url(request.form.get("url", ""))

        if long_url is None:
            return "Invalid URL. Only HTTP and HTTPS URLs are allowed.", 400

        connection = get_db()

        while True:
            code = generate_code()

            try:
                connection.execute(
                    """
                    INSERT INTO links
                    (code, url, clicks, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        code,
                        long_url,
                        0,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    )
                )
                break

            except sqlite3.IntegrityError:
                # Code collision. Generate another code and retry.
                continue

        connection.commit()
        connection.close()

        short_url = PUBLIC_BASE_URL + code

    return render_template(
        "index.html",
        short_url=short_url
    )


@app.route("/<code>")
def redirect_url(code):

    connection = get_db()

    link = connection.execute(
        "SELECT * FROM links WHERE code = ?",
        (code,)
    ).fetchone()

    if link:

        connection.execute(
            "UPDATE links SET clicks = clicks + 1 WHERE code = ?",
            (code,)
        )

        connection.commit()
        connection.close()

        return redirect(link["url"])

    connection.close()

    return "❌ Link not found", 404


@app.route("/login", methods=["GET", "POST"])
def login():

    if not ADMIN_PASSWORD_HASH:
        return "Admin authentication is not configured.", 503

    if request.method == "POST":

        ip = get_client_ip()

        if login_rate_limited(ip):
            return "Too many login attempts. Try again later.", 429

        password = request.form.get("password", "")

        try:
            password_ok = check_password_hash(ADMIN_PASSWORD_HASH, password)
        except ValueError:
            password_ok = False

        if not password_ok:
            record_login_failure(ip)
            return "Invalid credentials.", 401

        clear_login_failures(ip)

        session.clear()
        session["admin_authenticated"] = True
        session["csrf_token"] = secrets.token_urlsafe(32)

        return redirect("/dashboard")

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/")


@app.route("/dashboard")
@admin_required
def dashboard():

    connection = get_db()

    links = connection.execute(
        "SELECT * FROM links ORDER BY id DESC"
    ).fetchall()

    connection.close()

    return render_template(
    	"dashboard.html",
        links=links
    )

@app.route("/bulk", methods=["GET", "POST"])
@admin_required
def bulk():

    results = []
    batch_id = None

    if request.method == "POST" and not validate_csrf():
        return "Invalid CSRF token.", 400

    if request.method == "POST":

        raw_urls = request.form.get("urls", "").splitlines()

        if len(raw_urls) > MAX_BULK_URLS:
            return f"Too many URLs. Maximum is {MAX_BULK_URLS}.", 400

        urls = []
        for raw_url in raw_urls:
            if not raw_url.strip():
                continue

            long_url = validate_url(raw_url)
            if long_url is None:
                return "Invalid URL in bulk input. Only HTTP and HTTPS URLs are allowed.", 400

            urls.append(long_url)

        ip = get_client_ip()
        blocked, retry_after = rate_limit_request(
            ip,
            _bulk_creation_requests,
            BULK_CREATE_MAX_URLS,
            LINK_CREATE_WINDOW_SECONDS,
            count=len(urls),
        )
        if blocked:
            return (
                "Too many bulk creation requests. Try again later.",
                429,
                {"Retry-After": str(retry_after)},
            )

        connection = get_db()

        last_batch = connection.execute(
            "SELECT batch_id FROM links WHERE batch_id IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()

        if last_batch:
            last_number = int(last_batch["batch_id"][1:])
            batch_id = f"B{last_number + 1:03d}"
        else:
            batch_id = "B001"

        for long_url in urls:

            while True:
                code = generate_code()

                try:
                    connection.execute(
                        """
                        INSERT INTO links
                        (code, url, clicks, created_at, batch_id)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            code,
                            long_url,
                            0,
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            batch_id
                        )
                    )
                    break

                except sqlite3.IntegrityError:
                    # Code collision. Generate another code and retry.
                    continue

            results.append({
                "url": long_url,
                "short_url": PUBLIC_BASE_URL + code
            })

        connection.commit()
        connection.close()

    return render_template(
        "bulk.html",
        csrf_token=session["csrf_token"],
        results=results,
        batch_id=batch_id
    )

@app.route("/bulk/<batch_id>/csv")
@admin_required
def export_csv(batch_id):

    connection = get_db()

    links = connection.execute(
        """
        SELECT code, url, clicks, created_at, batch_id
        FROM links
        WHERE batch_id = ?
        ORDER BY id ASC
        """,
        (batch_id,)
    ).fetchall()

    connection.close()

    output = io.StringIO()

    writer = csv.writer(output)

    writer.writerow([
        "Batch ID",
        "Original URL",
        "Short URL",
        "Clicks",
        "Created At"
    ])

    for link in links:
        writer.writerow([
            link["batch_id"],
            link["url"],
            PUBLIC_BASE_URL + link["code"],
            link["clicks"],
            link["created_at"]
        ])

    csv_data = output.getvalue().encode("utf-8")

    return send_file(
        io.BytesIO(csv_data),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"{batch_id}.csv"
    )

@app.route("/bulk/<batch_id>/pdf")
@admin_required
def export_pdf(batch_id):

    connection = get_db()

    links = connection.execute(
        """
        SELECT code, url, clicks, created_at, batch_id
        FROM links
        WHERE batch_id = ?
        ORDER BY id ASC
        """,
        (batch_id,)
    ).fetchall()

    connection.close()

    output = io.BytesIO()

    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        rightMargin=30,
        leftMargin=30,
        topMargin=30,
        bottomMargin=30
    )

    styles = getSampleStyleSheet()

    elements = []

    elements.append(
        Paragraph(
            f"Bulk URL Shortener - Batch {html.escape(batch_id)}",
            styles["Title"]
        )
    )

    elements.append(Spacer(1, 15))

    data = [
        [
            Paragraph("<b>Original URL</b>", styles["Normal"]),
            Paragraph("<b>Short URL</b>", styles["Normal"]),
            Paragraph("<b>Clicks</b>", styles["Normal"]),
            Paragraph("<b>Created At</b>", styles["Normal"])
        ]
    ]

    for link in links:

        original_url = Paragraph(
            html.escape(link["url"]),
            styles["Normal"]
        )

        short_url = Paragraph(
            html.escape(PUBLIC_BASE_URL + link["code"]),
            styles["Normal"]
        )

        data.append([
            original_url,
            short_url,
            str(link["clicks"]),
            link["created_at"]
        ])

    table = Table(
        data,
        colWidths=[380, 180, 60, 120],
        repeatRows=1
    )

    table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ])
    )

    elements.append(table)

    document.build(elements)

    output.seek(0)

    return send_file(
        output,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{batch_id}.pdf"
    )

create_database()  # also runs on import, so a production server gets a ready database

if __name__ == "__main__":

    # Debug mode lets anyone who can reach the page run code on this machine.
    # It stays off unless you start the app with FLASK_DEBUG=1.
    app.run(host="0.0.0.0", port=5000, debug=os.getenv("FLASK_DEBUG") == "1")
