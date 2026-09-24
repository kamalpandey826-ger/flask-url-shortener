from flask import Flask, request, redirect, render_template, render_template_string
import random
import string
import sqlite3
from datetime import datetime

app = Flask(__name__)

DATABASE = "/data/links.db"


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

    return render_template_string(
        """
        <h1>📊 Link Dashboard</h1>

        <table border="1" cellpadding="10">

        <tr>
            <th>Short Code</th>
            <th>Original URL</th>
            <th>Clicks</th>
            <th>Created</th>
        </tr>

        {% for link in links %}

        <tr>

            <td>
                <a href="/{{ link.code }}">
                    {{ link.code }}
                </a>
            </td>

            <td>{{ link.url }}</td>

            <td>{{ link.clicks }}</td>

            <td>{{ link.created_at }}</td>

        </tr>

        {% endfor %}

        </table>
        """,
        links=links
    )


if __name__ == "__main__":

    create_database()

    app.run(host="0.0.0.0", port=5000, debug=True)
