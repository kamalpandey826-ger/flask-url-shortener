from flask import Flask, request, redirect, render_template, render_template_string, send_file
import random
import string
import sqlite3
import os
import csv
import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

app = Flask(__name__)

DATABASE =os.getenv("DATABASE", "/data/links.db")


def get_db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def create_database():
    connection = get_db()

    connection.execute("""
        CREATE TABLE IF NOT EXISTS links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            url TEXT NOT NULL,
            clicks INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)

    connection.commit()
    connection.close()


def generate_code(length=6):

    characters = string.ascii_letters + string.digits

    return "".join(
        random.choice(characters)
        for _ in range(length)
    )



@app.route("/", methods=["GET", "POST"])
def home():

    short_url = None

    if request.method == "POST":

        long_url = request.form["url"]

        connection = get_db()

        code = generate_code()

        while True:

            existing = connection.execute(
                "SELECT * FROM links WHERE code = ?",
                (code,)
            ).fetchone()

            if not existing:
                break

            code = generate_code()

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

        connection.commit()
        connection.close()

        short_url = request.host_url + code

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


@app.route("/dashboard")
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
def bulk():

    results = []
    batch_id = None

    if request.method == "POST":

        urls = request.form.get("urls", "").splitlines()

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

            long_url = long_url.strip()

            if not long_url:
                continue

            code = generate_code()

            while connection.execute(
                "SELECT 1 FROM links WHERE code = ?",
                (code,)
            ).fetchone():
                code = generate_code()

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

            results.append({
                "url": long_url,
                "short_url": request.host_url + code
            })

        connection.commit()
        connection.close()

    return render_template(
        "bulk.html",
        results=results,
        batch_id=batch_id
    )

@app.route("/bulk/<batch_id>/csv")
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
            request.host_url + link["code"],
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
            f"Bulk URL Shortener - Batch {batch_id}",
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
            link["url"],
            styles["Normal"]
        )

        short_url = Paragraph(
            request.host_url + link["code"],
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

if __name__ == "__main__":

    create_database()

    app.run(host="0.0.0.0", port=5000, debug=True)
