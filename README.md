# Flask URL Shortener

A Python Flask and SQLite URL shortener with bulk URL creation, click tracking, batch IDs, CSV/PDF exports, authentication, and rate limiting.

## Architecture

Internet → Cloudflare Tunnel → Nginx → Gunicorn → Flask → SQLite

- **Cloudflare Tunnel:** Public access without router port forwarding.
- **Nginx:** Reverse proxy on port 80.
- **Gunicorn:** WSGI server bound to `127.0.0.1:5000`.
- **Flask:** URL shortening and application logic.
- **SQLite:** Persistent application data.

## Features

### URL Management
- Secure random short-code generation
- HTTP/HTTPS URL validation
- Collision-safe code generation
- Bulk URL shortening
- Batch IDs

### Tracking and Exports
- Access logging
- Client IP and User-Agent logging
- CSV exports
- PDF report generation
- Escaping of user-controlled report data

### Security
- Protected admin dashboard
- Werkzeug password hashing
- Session-based authentication
- CSRF protection
- Login rate limiting
- Separate bulk-creation rate limiting
- Host-header poisoning protection
- Configurable secure session cookies
- URL validation and database integrity protections

### Deployment
- Flask and Gunicorn
- Nginx reverse proxy
- SQLite persistence
- Docker and Docker Compose configuration
- GitHub Actions CI
- Optional Cloudflare Tunnel for public access

## Requirements

- Python 3
- Flask
- SQLite
- Gunicorn
- Nginx for native reverse-proxy deployment
- Optional: Docker, Docker Compose, and `cloudflared`

## Local Setup

Create and activate a virtual environment:

    python3 -m venv venv
    source venv/bin/activate
    python -m pip install -r requirements.txt

Configure the required environment variables:

    export SECRET_KEY="replace-with-a-strong-secret"
    export ADMIN_PASSWORD_HASH="$(python -c 'from werkzeug.security import generate_password_hash; print(generate_password_hash(input("Admin password: ")))')"
    export PUBLIC_BASE_URL="http://127.0.0.1/"

Run the development server:

    python app.py

## Running with Gunicorn

With the virtual environment activated and environment variables configured:

    python -m gunicorn --config gunicorn.conf.py app:app

Gunicorn listens on `127.0.0.1:5000`. Nginx proxies requests to it.

## Cloudflare Quick Tunnel

For temporary public testing:

    cloudflared tunnel --url http://127.0.0.1:80

Cloudflare will display a temporary `trycloudflare.com` hostname. Set `PUBLIC_BASE_URL` to the current HTTPS hostname, including the trailing slash, and restart Gunicorn:

    export PUBLIC_BASE_URL="https://your-current-hostname.trycloudflare.com/"

Quick Tunnel hostnames may change when the tunnel restarts. Keep the tunnel process running while using the public URL. This setup is intended for testing, not a permanent hostname.

## Testing

Run the test suite:

    pytest

## Environment Variables

| Variable | Purpose |
| --- | --- |
| `SECRET_KEY` | Flask session/security key |
| `ADMIN_PASSWORD_HASH` | Hashed admin password |
| `PUBLIC_BASE_URL` | Base URL used to generate short links |
| `SESSION_COOKIE_SECURE` | Controls the Secure session-cookie flag |

Never commit real secrets, `.env` files, password hashes, or `links.db` to Git.

## Docker and CI

Docker configuration is included for containerized development/deployment. Docker is optional for the native Gunicorn and Nginx setup.

GitHub Actions provides continuous integration through the repository workflow.

## Production Notes

Before permanent public deployment, review production credentials, HTTPS and proxy configuration, rate limits, session-cookie settings, logging, and service persistence.
