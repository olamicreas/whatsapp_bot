import os, json, threading, time
from flask import Flask, render_template, request, redirect, url_for, session
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

app = Flask(__name__)
app.secret_key = "super_secret_key"

# ---------------------- Config ----------------------
DATA_FILE = "data.json"         # stores registered users
REF_FILE = "referrals.json"     # stores referral counts
CRED_FILE = "/etc/secrets/credentials.json"
TOKEN_FILE = "token.json"
SCOPES = ["https://www.googleapis.com/auth/contacts.readonly"]

UPDATE_INTERVAL = 300  # 5 minutes auto-update

# ---------------------- Helpers ----------------------
def load_json(path, default):
    """Safe JSON loader."""
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump(default, f)
    with open(path, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=4)

def get_credentials():
    """Load or refresh Google API credentials."""
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_FILE, "w") as token:
                token.write(creds.to_json())
            return creds
    return None

def fetch_contacts_and_update():
    """Fetch Google Contacts and update referral counts."""
    creds = get_credentials()
    if not creds:
        print("[INFO] No credentials yet. Please visit /auth once to connect Google Contacts.")
        return

    try:
        service = build("people", "v1", credentials=creds)
        results = service.people().connections().list(
            resourceName="people/me",
            personFields="names,emailAddresses",
            pageSize=1000
        ).execute()
        connections = results.get("connections", [])
        referrals = load_json(REF_FILE, {})
        users = load_json(DATA_FILE, [])

        # Build a lookup table of referral IDs
        for user in users:
            ref_id = user["ref_id"]
            count = 0
            for person in connections:
                names = person.get("names", [])
                if names:
                    name = names[0].get("displayName", "").lower()
                    if ref_id.lower() in name:
                        count += 1
            referrals[ref_id] = {
                "name": user["name"],
                "group": user.get("group", ""),
                "referrals": count
            }

        save_json(REF_FILE, referrals)
        print("[AUTO-UPDATE] Referral counts synced from Google Contacts.")
    except Exception as e:
        print(f"[ERROR] Failed to update referrals: {e}")

def background_updater():
    """Continuously update referral data in background."""
    while True:
        fetch_contacts_and_update()
        time.sleep(UPDATE_INTERVAL)

# ---------------------- Routes ----------------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/register", methods=["POST"])
def register():
    """Register a new user."""
    name = request.form["name"]
    group = request.form.get("group", "")
    ref_id = name.lower().replace(" ", "_")

    users = load_json(DATA_FILE, [])
    for user in users:
        if user["ref_id"] == ref_id:
            return redirect(url_for("progress", ref_id=ref_id))

    new_user = {"name": name, "group": group, "ref_id": ref_id}
    users.append(new_user)
    save_json(DATA_FILE, users)

    # Initialize referral entry
    referrals = load_json(REF_FILE, {})
    referrals[ref_id] = {"name": name, "group": group, "referrals": 0}
    save_json(REF_FILE, referrals)

    return redirect(url_for("progress", ref_id=ref_id))

@app.route("/progress/<ref_id>")
def progress(ref_id):
    """Individual progress page."""
    referrals = load_json(REF_FILE, {})
    if ref_id not in referrals:
        return "Invalid referral ID", 404
    return render_template("progress.html", ref=referrals[ref_id])

@app.route("/public")
def public():
    """Leaderboard page."""
    referrals = load_json(REF_FILE, {})
    return render_template("progress.html", all_refs=referrals)

@app.route("/auth")
def auth():
    """Manual one-time Google OAuth."""
    flow = Flow.from_client_secrets_file(CRED_FILE, scopes=SCOPES)
    flow.redirect_uri = url_for("oauth2callback", _external=True)
    auth_url, _ = flow.authorization_url(prompt="consent")
    return redirect(auth_url)

@app.route("/oauth2callback")
def oauth2callback():
    """OAuth callback handler."""
    flow = Flow.from_client_secrets_file(CRED_FILE, scopes=SCOPES)
    flow.redirect_uri = url_for("oauth2callback", _external=True)
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    with open(TOKEN_FILE, "w") as token:
        token.write(creds.to_json())
    return redirect(url_for("public"))

# ---------------------- Start ----------------------
if __name__ == "__main__":
    threading.Thread(target=background_updater, daemon=True).start()
    print("✅ Flask app running with automatic Google Contacts sync and data.json for users.")
    app.run(debug=True)
