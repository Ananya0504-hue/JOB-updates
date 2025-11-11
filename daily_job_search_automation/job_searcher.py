"""
job_searcher.py

Daily job search using Google Custom Search API, email results, and persist deduplication
state in the repository by updating sent_links.json via the GitHub Contents API.

Environment variables (set as GitHub Secrets or repo secrets as described in README):
- GCP_API_KEY              (required) Google API key with Custom Search API enabled
- CSE_ID                  (required) Google Custom Search Engine ID (cx)
- GITHUB_REPOSITORY       (provided in Actions, e.g. "owner/repo")
- GITHUB_TOKEN            (provided in Actions as secret or provided automatically in workflow)
- FROM_EMAIL              (required) email address to send from
- TO_EMAIL                (required) email address to send to (can be same as FROM_EMAIL)
- SMTP_HOST               (optional) SMTP host (e.g., smtp.gmail.com)
- SMTP_PORT               (optional) SMTP port (e.g., 587)
- SMTP_USER               (optional) SMTP username
- SMTP_PASS               (optional) SMTP password
- SENDGRID_API_KEY        (optional) alternative to SMTP — if provided send via SendGrid
- MAX_RESULTS_PER_QUERY   (optional) defaults to 10 (Google returns up to 10 per page)
- FILE_PATH               (optional) path in repo for dedupe file, default "sent_links.json"
"""

import os
import json
import base64
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import List, Dict, Any

# ---------- Config / queries ----------
GCP_API_KEY = os.environ.get("GCP_API_KEY")
CSE_ID = os.environ.get("CSE_ID")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY")  # owner/repo
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
FROM_EMAIL = os.environ.get("FROM_EMAIL")
TO_EMAIL = os.environ.get("TO_EMAIL")
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY")
MAX_RESULTS_PER_QUERY = int(os.environ.get("MAX_RESULTS_PER_QUERY", "10"))
FILE_PATH = os.environ.get("FILE_PATH", "sent_links.json")

QUERIES = [
    '("entry level" OR "junior") "data analyst" ("startup" OR "early-stage" OR "seed")',
    '("entry level" OR "junior") "software engineer" ("startup" OR "early-stage" OR "seed")'
]

# ---------- Helpers for Google Custom Search ----------
def google_custom_search(query: str, start: int = 1) -> List[Dict[str, Any]]:
    """
    Performs a single Google Custom Search API call.
    start is 1-based (1..)
    """
    if not GCP_API_KEY or not CSE_ID:
        raise RuntimeError("GCP_API_KEY and CSE_ID must be set.")
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": GCP_API_KEY,
        "cx": CSE_ID,
        "q": query,
        "start": start,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("items", [])


# ---------- GitHub repo file helpers ----------
def get_repo_file(path: str) -> Dict[str, Any]:
    """Get file content & metadata from the repo using the GitHub API. Returns dict or None if not found."""
    owner_repo = GITHUB_REPOSITORY
    api_url = f"https://api.github.com/repos/{owner_repo}/contents/{path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
    r = requests.get(api_url, headers=headers, timeout=30)
    if r.status_code == 200:
        return r.json()
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return None

def create_or_update_repo_file(path: str, content_bytes: bytes, message: str, sha: str = None):
    """Create or update a file in the repo. content_bytes will be base64 encoded."""
    owner_repo = GITHUB_REPOSITORY
    api_url = f"https://api.github.com/repos/{owner_repo}/contents/{path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("utf-8")
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(api_url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


# ---------- Email sending ----------
def send_email_smtp(subject: str, html_body: str, text_body: str = ""):
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        raise RuntimeError("SMTP_HOST, SMTP_USER, SMTP_PASS must be set for SMTP sending.")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = FROM_EMAIL
    msg["To"] = TO_EMAIL
    part1 = MIMEText(text_body or "See HTML body", "plain")
    part2 = MIMEText(html_body, "html")
    msg.attach(part1)
    msg.attach(part2)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(FROM_EMAIL, [TO_EMAIL], msg.as_string())

def send_email_sendgrid(subject: str, html_body: str, text_body: str = ""):
    if not SENDGRID_API_KEY:
        raise RuntimeError("SENDGRID_API_KEY must be set to use SendGrid.")
    url = "https://api.sendgrid.com/v3/mail/send"
    payload = {
        "personalizations": [{"to": [{"email": TO_EMAIL}], "subject": subject}],
        "from": {"email": FROM_EMAIL},
        "content": [
            {"type": "text/plain", "value": text_body or "See HTML body"},
            {"type": "text/html", "value": html_body}
        ]
    }
    headers = {"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r

def send_email(subject: str, html_body: str, text_body: str = ""):
    if SENDGRID_API_KEY:
        return send_email_sendgrid(subject, html_body, text_body)
    else:
        return send_email_smtp(subject, html_body, text_body)


# ---------- Main flow ----------
def load_sent_links() -> List[str]:
    """Load the list of previously sent links from the repo file (if exists)."""
    obj = get_repo_file(FILE_PATH)
    if not obj:
        return []
    content_b64 = obj.get("content", "")
    decoded = base64.b64decode(content_b64).decode("utf-8")
    try:
        data = json.loads(decoded)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []

def save_sent_links(new_list: List[str], sha: str = None):
    """Save (create or update) sent_links.json in the repo."""
    content_bytes = json.dumps(new_list, indent=2).encode("utf-8")
    message = f"Update sent links at {datetime.utcnow().isoformat()}Z"
    return create_or_update_repo_file(FILE_PATH, content_bytes, message, sha=sha)

def build_email_html(results: List[Dict[str, Any]]):
    date_str = datetime.now().strftime("%Y-%m-%d")
    html = f"<h2>Job search results — {date_str}</h2>"
    if not results:
        html += "<p>No new results.</p>"
        return html
    html += "<ol>"
    for r in results:
        html += f"<li><strong>{r.get('title')}</strong><br>"
        html += f"{r.get('snippet', '')}<br>"
        html += f'<a href="{r.get("link")}" target="_blank">{r.get("link")}</a><br><small>query: {r.get("query")}</small></li><br>'
    html += "</ol>"
    return html

def run():
    if not GCP_API_KEY or not CSE_ID:
        raise RuntimeError("GCP_API_KEY and CSE_ID required.")
    if not FROM_EMAIL or not TO_EMAIL:
        raise RuntimeError("FROM_EMAIL and TO_EMAIL must be set.")

    # load existing file metadata to obtain sha for update (if exists)
    remote_obj = get_repo_file(FILE_PATH)
    existing_links = []
    remote_sha = None
    if remote_obj:
        remote_sha = remote_obj.get("sha")
        try:
            existing_links = json.loads(base64.b64decode(remote_obj.get("content", "")).decode("utf-8"))
            if not isinstance(existing_links, list):
                existing_links = []
        except Exception:
            existing_links = []

    sent_set = set(existing_links)
    found_results = []
    added_links = []

    # For each query, fetch results (1 page only by default)
    for q in QUERIES:
        try:
            items = google_custom_search(q, start=1)
        except Exception as e:
            print(f"Error searching query '{q}': {e}")
            items = []
        for it in items:
            link = it.get("link")
            title = it.get("title")
            snippet = it.get("snippet")
            if not link:
                continue
            if link in sent_set:
                continue
            # Basic filter: only keep if it appears like a job post (heuristic)
            # Many listings have "apply", "hiring", "job", etc. We'll not be strict to avoid false negatives.
            # Add to output
            found_results.append({
                "query": q,
                "title": title,
                "snippet": snippet,
                "link": link
            })
            sent_set.add(link)
            added_links.append(link)

    # Send email if there are new results
    if found_results:
        html = build_email_html(found_results)
        subject = f"Daily job search — {datetime.utcnow().strftime('%Y-%m-%d')} — {len(found_results)} new"
        try:
            send_email(subject, html, text_body=f"{len(found_results)} new results; see HTML.")
            print("Email sent.")
        except Exception as e:
            print("Failed to send email:", e)
            raise

        # Save updated dedupe file back to repo (create or update)
        new_list = sorted(list(sent_set))
        try:
            save_sent_links(new_list, sha=remote_sha)
            print(f"Updated {FILE_PATH} in repo with {len(added_links)} new links.")
        except Exception as e:
            print("Failed to save dedupe file to repo:", e)
            raise
    else:
        print("No new results; nothing to send or update.")

if __name__ == "__main__":
    run()

