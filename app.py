# app.py
import os
import json
import threading
import time
import re
import base64
import requests
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, abort
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "super_secret_key")

# ---------------------- Config ----------------------
DATA_FILE = "data.json"
REF_FILE = "referrals.json"

# prefer Render secret file path if present, else local credentials.json
CRED_FILE = "/etc/secrets/credentials.json" if os.path.exists("/etc/secrets/credentials.json") else "credentials.json"
TOKEN_FILE = "token.json"
SCOPES = ["https://www.googleapis.com/auth/contacts.readonly"]

UPDATE_INTERVAL = int(os.getenv("UPDATE_INTERVAL", 300))  # seconds
TEAMS_PER_GROUP = int(os.getenv("TEAMS_PER_GROUP", 10))

# Optional admin key to protect /sync-now and /migrate-team-links
ADMIN_KEY = os.getenv("ADMIN_KEY", None)

# GitHub auto-push config (set these as environment variables)
GITHUB_TOKEN = os.getenv("GITHUB_PAT")                # required for auto-push
GITHUB_REPO = os.getenv("GITHUB_REPO", "olamicreas/whatsapp_bot")  # owner/repo
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "master")    # branch to commit to

# ---------------------- WhatsApp referral links ----------------------
TEAM_LINKS = {
    1: "https://wa.link/0a7pj3",
    2: "https://wa.link/uiv1az",
    3: "https://wa.link/i7rwku",
    4: "https://wa.link/47ly4h",
    5: "https://wa.link/xfq5gn"
}

SOLO_LINKS = {
    1: "https://wa.link/b6kecz",  # Ref 001
    2: "https://wa.link/stv0mr",  # Ref 002
    3: "https://wa.link/yup4kc",  # Ref 003 / Mr Heep
    4: "https://wa.link/ze4vj4",  # Ref 004
    5: "https://wa.link/109mvf"   # Ref 005
}

TEAMS_PER_GROUP = 5
SOLO_COUNT = 5

# ---------------------- Helpers: file + GitHub ----------------------
def load_json(path, default):
    """Load JSON file, return default if missing/corrupt."""
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump(default, f)
        return default
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default

def _github_api_headers():
    """Return headers for GitHub API if token present."""
    if not GITHUB_TOKEN:
        return None
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "referral-app-bot"
    }

def _github_get_file_content(repo, path, branch=GITHUB_BRANCH):
    """
    Return tuple (decoded_text_or_None, sha_or_None).
    Uses GitHub contents API to fetch file content and sha.
    """
    headers = _github_api_headers()
    if not headers:
        return None, None
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            content_b64 = data.get("content", "")
            sha = data.get("sha")
            # API sometimes returns content with newlines; decode robustly
            try:
                decoded = base64.b64decode(content_b64).decode("utf-8")
            except Exception:
                # fallback: try to load as-is
                decoded = content_b64
            return decoded, sha
        # Not found or other response -> no remote
        return None, None
    except Exception as e:
        print(f"[GITHUB] GET file failed: {e}")
        return None, None

def _github_put_file(repo, path, content_bytes, message, branch=GITHUB_BRANCH, sha=None):
    """
    Create or update a file on GitHub via REST API.
    Expects content_bytes (raw bytes) which will be base64 encoded.
    Returns response JSON on success or raises RuntimeError.
    """
    headers = _github_api_headers()
    if not headers:
        raise RuntimeError("GITHUB_TOKEN not configured")
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("utf-8"),
        "branch": branch
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(url, headers=headers, json=payload, timeout=20)
    if r.status_code in (200, 201):
        return r.json()
    else:
        raise RuntimeError(f"GitHub API error {r.status_code}: {r.text}")

def push_file_to_github(path, commit_message=None):
    """
    Push local file `path` to GitHub repo configured by GITHUB_REPO.
    - If file exists remotely, uses its sha to update (prevents accidental overwrite).
    - Returns dict with result info or {'skipped': True} when not configured.
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("[GITHUB] Skipping push: GITHUB_TOKEN or GITHUB_REPO not set.")
        return {"skipped": True}

    if not os.path.exists(path):
        print(f"[GITHUB] Local file {path} not found, skipping push.")
        return {"skipped": True}

    try:
        with open(path, "rb") as f:
            content = f.read()
    except Exception as e:
        print(f"[GITHUB] Failed to read {path}: {e}")
        return {"error": str(e)}

    repo = GITHUB_REPO
    branch = GITHUB_BRANCH
    commit_message = commit_message or f"Auto-update {os.path.basename(path)}"

    # Get remote sha (if exists)
    _, remote_sha = _github_get_file_content(repo, path, branch=branch)
    try:
        result = _github_put_file(repo, path, content, commit_message, branch=branch, sha=remote_sha)
        print(f"[GITHUB] Pushed {path} to {repo}@{branch} (sha: {result.get('content',{}).get('sha')})")
        return {"ok": True, "response": result}
    except Exception as e:
        print(f"[GITHUB] Push failed for {path}: {e}")
        return {"error": str(e)}

def save_json(path, data, push_to_github=True):
    """
    Save JSON locally and optionally push to GitHub.
    When pushing DATA_FILE or REF_FILE, first attempt to merge with remote contents to avoid accidental overwrites.
    - DATA_FILE expected to be a list of user dicts. Merge unique by 'ref_id' (local wins).
    - REF_FILE expected to be a dict; merged with remote dict (local wins).
    """
    # Defensive: ensure directories exist
    try:
        # Merge with remote if pushing
        if push_to_github and path in (DATA_FILE, REF_FILE) and GITHUB_TOKEN and GITHUB_REPO:
            remote_text, remote_sha = _github_get_file_content(GITHUB_REPO, path, branch=GITHUB_BRANCH)
            if remote_text:
                try:
                    remote_data = json.loads(remote_text)
                except Exception:
                    remote_data = None
            else:
                remote_data = None

            # Merge logic
            if path == DATA_FILE:
                # expect list
                local_list = data if isinstance(data, list) else []
                remote_list = remote_data if isinstance(remote_data, list) else []
                # Build map remote by ref_id
                merged_map = {}
                for item in remote_list:
                    if isinstance(item, dict):
                        rid = item.get("ref_id")
                        if rid:
                            merged_map[rid] = item
                # Overlay with local items (local wins)
                for item in local_list:
                    if isinstance(item, dict):
                        rid = item.get("ref_id")
                        if rid:
                            merged_map[rid] = item
                merged_list = list(merged_map.values())
                # write merged_list locally
                with open(path, "w") as f:
                    json.dump(merged_list, f, indent=4)
                # push merged content
                try:
                    return push_file_to_github(path, commit_message=f"Auto-update {path}")
                except Exception as e:
                    print(f"[WARN] GitHub push failed after merging: {e}")
                    return {"error": str(e)}
            else:
                # REF_FILE or other object file -> merge dicts
                local_dict = data if isinstance(data, dict) else {}
                remote_dict = remote_data if isinstance(remote_data, dict) else {}
                merged = remote_dict.copy()
                merged.update(local_dict)  # local wins on conflicts
                with open(path, "w") as f:
                    json.dump(merged, f, indent=4)
                try:
                    return push_file_to_github(path, commit_message=f"Auto-update {path}")
                except Exception as e:
                    print(f"[WARN] GitHub push failed after merging dict: {e}")
                    return {"error": str(e)}
        else:
            # Normal local save only
            with open(path, "w") as f:
                json.dump(data, f, indent=4)
            return {"saved_local": True}
    except Exception as e:
        print(f"[ERROR] Failed saving {path}: {e}")
        raise

# ---------------------- Utilities ----------------------
def normalize_ref_id(s):
    """Lowercase, trim, replace whitespace with underscore, remove unusual chars."""
    if not s:
        return ""
    s = s.strip().lower()
    s = re.sub(r"\s+", "_", s)
    # keep a-z0-9_ only
    s = re.sub(r"[^a-z0-9_]", "", s)
    return s

# ---------------------- Team & Registration logic ----------------------
def assign_link(reg_type):
    """
    Round-robin assign:
     - for reg_type == "team" -> TEAM links (1..TEAMS_PER_GROUP)
     - for reg_type == "solo" -> SOLO links (1..SOLO_COUNT)
    Returns: (number, link)
    """
    reg_type = (reg_type or "team").strip().lower()
    users = load_json(DATA_FILE, [])
    if reg_type == "team":
        team_users = [u for u in users if u.get("registration_type") == "team"]
        team_number = (len(team_users) % TEAMS_PER_GROUP) + 1
        return team_number, TEAM_LINKS.get(team_number)
    elif reg_type == "solo":
        solo_users = [u for u in users if u.get("registration_type") == "solo"]
        solo_number = (len(solo_users) % SOLO_COUNT) + 1
        return solo_number, SOLO_LINKS.get(solo_number)
    else:
        return 1, TEAM_LINKS.get(1)

def team_label(group_name, team_number):
    # unified team label format: TEAM1, TEAM2, ...
    return f"TEAM{int(team_number)}"

# ---------------------- Google contact mention helpers ----------------------
def contact_mentions_team(contact, team_number):
    """
    True if contact contains 'TEAM{team_number}' in any scanned field.
    Accepts 'TEAM1', 'TEAM 1', 'team-01', etc.
    """
    token_pattern = re.compile(r"\bteam[\s_\-]*0*{}\b".format(int(team_number)), flags=re.I)
    texts = []
    for field in ("names", "biographies", "organizations", "userDefined"):
        if field in contact:
            for item in contact.get(field, []):
                if isinstance(item, dict):
                    # common keys
                    for key in ("displayName", "value", "name", "title"):
                        val = item.get(key)
                        if val:
                            texts.append(str(val))
                else:
                    texts.append(str(item))
    combined = " ".join([t for t in texts if t]).lower()
    # remove punctuation so that 'team-1' or 'team.1' still matches
    combined_clean = re.sub(r"[^\w\s]", " ", combined)
    return bool(token_pattern.search(combined_clean)) or (f"team{team_number}" in combined_clean.replace(" ", ""))

def contact_mentions_ref(contact, ref_number):
    """
    True if contact contains 'REF{zero_padded}' or 'REF {n}' etc.
    e.g. ref_number=1 -> matches REF001, REF 001, ref1, ref 1
    """
    # allow zero padding
    token_pattern = re.compile(r"\bref[\s_\-]*0*{}\b".format(int(ref_number)), flags=re.I)
    texts = []
    for field in ("names", "biographies", "organizations", "userDefined"):
        if field in contact:
            for item in contact.get(field, []):
                if isinstance(item, dict):
                    for key in ("displayName", "value", "name", "title"):
                        val = item.get(key)
                        if val:
                            texts.append(str(val))
                else:
                    texts.append(str(item))
    combined = " ".join([t for t in texts if t]).lower()
    combined_clean = re.sub(r"[^\w\s]", " ", combined)
    if token_pattern.search(combined_clean):
        return True
    # also check compact form like 'ref001'
    if f"ref{str(ref_number).zfill(3)}" in combined.replace(" ", ""):
        return True
    return False

# ---------------------- Sync & aggregation ----------------------
def fetch_contacts_and_update():
    """
    Reads Google Contacts, counts occurrences of team labels (per group) and REFxxx for solos,
    then writes referrals to REF_FILE (and optionally pushes to GitHub).
    """
    creds = None
    try:
        creds = get_credentials()
    except Exception:
        creds = None

    if not creds:
        print("[INFO] No credentials yet. Visit /auth to connect Google Contacts.")
        return {"status": "no-credentials"}

    try:
        service = build("people", "v1", credentials=creds)
        results = service.people().connections().list(
            resourceName="people/me",
            personFields="names,emailAddresses,organizations,biographies,userDefined",
            pageSize=2000   # max allowed
        ).execute()

        connections = results.get("connections", [])
        users = load_json(DATA_FILE, [])

        # prepare groups -> teams structure from registered users
        groups = {}
        for u in users:
            group = (u.get("group") or "").strip() or "ALL"
            team_num = u.get("team_number") if u.get("team_number") is not None else u.get("assigned_number", 1)
            # ensure integer key (we will stringify on save)
            try:
                team_num = int(team_num)
            except Exception:
                team_num = 1
            groups.setdefault(group, {})
            groups[group].setdefault(team_num, {"team_label": f"TEAM{team_num}", "count": 0})

        # prepare SOLO refs (REF001..REF005)
        SOLO_MAX = SOLO_COUNT
        solo_refs = {i: {"ref_label": f"REF{str(i).zfill(3)}", "count": 0} for i in range(1, SOLO_MAX + 1)}

        # scan contacts and increment team counts and solo refs when matched
        for contact in connections:
            # team matches
            for group, teams in groups.items():
                for team_num in list(teams.keys()):
                    if contact_mentions_team(contact, team_num):
                        teams[team_num]["count"] += 1
                        try:
                            name = contact.get("names", [{"displayName": "Unknown"}])[0].get("displayName", "Unknown")
                        except Exception:
                            name = "Unknown"
                        print(f"[MATCH] {name} counted for {group} TEAM{team_num}")

            # solo refs REF001..REF005
            for i in range(1, SOLO_MAX + 1):
                if contact_mentions_ref(contact, i):
                    solo_refs[i]["count"] += 1
                    try:
                        name = contact.get("names", [{"displayName": "Unknown"}])[0].get("displayName", "Unknown")
                    except Exception:
                        name = "Unknown"
                    print(f"[MATCH] {name} counted for SOLO {solo_refs[i]['ref_label']}")

        # build referrals dict saved to REF_FILE
        referrals = {}
        for group, teams in groups.items():
            referrals[group] = {}
            for team_num, info in teams.items():
                referrals[group][str(team_num)] = {
                    "team_label": info["team_label"],
                    "referrals": info["count"]
                }

        # add SOLO group with REF001..REF005 counts
        referrals.setdefault("SOLO", {})
        for i, info in solo_refs.items():
            key = f"REF{str(i).zfill(3)}"
            referrals["SOLO"][key] = {
                "team_label": info["ref_label"],
                "referrals": info["count"]
            }

        # save locally and push to GitHub (if configured)
        save_json(REF_FILE, referrals, push_to_github=True)
        print("[AUTO-UPDATE] Referral counts per group/team and SOLO synced from Google Contacts.")
        return {"status": "ok", "groups": len(referrals)}

    except Exception as e:
        print(f"[ERROR] Failed to update referrals: {e}")
        return {"status": "error", "message": str(e)}

def background_updater():
    while True:
        try:
            fetch_contacts_and_update()
        except Exception as e:
            print("[WARN] background_updater error:", e)
        time.sleep(UPDATE_INTERVAL)

# ---------------------- Routes ----------------------
@app.route("/")
def index():
    # index.html should allow registration_type to be either team or solo (we default to team)
    return render_template("index.html")

@app.route("/register", methods=["POST"])
def register():
    name = request.form.get("name", "").strip()
    reg_type = request.form.get("registration_type", "team").strip().lower()
    if not name:
        return redirect(url_for("index"))

    ref_id = normalize_ref_id(name)

    # Load existing users
    users = load_json(DATA_FILE, [])
    existing = next((u for u in users if u.get("ref_id") == ref_id), None)
    if existing:
        return redirect(url_for("progress", ref_id=ref_id))

    # Assign number & link
    try:
        assigned_number, assigned_link = assign_link(reg_type)
    except Exception:
        assigned_number, assigned_link = (1, TEAM_LINKS.get(1))

    # Build label
    if reg_type == "team":
        label = f"TEAM{int(assigned_number)}"
    else:
        label = f"REF{int(assigned_number):03d}"

    new_user = {
        "name": name,
        "ref_id": ref_id,
        "registration_type": reg_type,
        "assigned_number": int(assigned_number),
        "team_number": int(assigned_number) if reg_type == "team" else None,
        "team_label": label,
        "team_link": assigned_link,
        "registered_at": int(time.time())
    }

    # Append to current users (local) and save (will merge with remote if configured)
    users.append(new_user)
    save_json(DATA_FILE, users, push_to_github=True)

    # Initialize or update referrals for team users
    referrals = load_json(REF_FILE, {})
    referrals.setdefault("ALL", {})

    if reg_type == "team":
        referrals["ALL"].setdefault(str(assigned_number), {"team_label": label, "referrals": 0})
    else:
        referrals.setdefault("SOLO", {})
        # For solos we store by ref_id under SOLO so progress can find it if desired
        referrals["SOLO"].setdefault(ref_id, {"team_label": label, "referrals": 0})

    save_json(REF_FILE, referrals, push_to_github=True)

    return redirect(url_for("progress", ref_id=ref_id))

@app.route("/progress/<ref_id>", methods=["GET", "POST"])
def progress(ref_id):
    # normalize incoming ref_id so /progress/Oye or /progress/oye both work
    ref_id_norm = normalize_ref_id(ref_id)

    # try a quick sync so progress shows latest counts (safe: fetch is idempotent)
    try:
        fetch_contacts_and_update()
    except Exception as e:
        print("[WARN] Auto-sync failed:", e)

    # load users and find the requested user (case-insensitive)
    users = load_json(DATA_FILE, [])
    user = next((u for u in users if normalize_ref_id(u.get("ref_id", "")) == ref_id_norm), None)
    if not user:
        return "Invalid referral ID", 404

    # load referrals (saved by fetch_contacts_and_update)
    referrals = load_json(REF_FILE, {})

    # Use default group key "ALL" if user.group missing
    group_key = (user.get("group") or "").strip() or "ALL"

    # get group data safely (may be empty)
    raw_group_data = referrals.get(group_key, {}) or {}

    # ensure keys are strings and referrals values are ints for templates
    group_data = {}
    for k, v in raw_group_data.items():
        try:
            group_data[str(k)] = {
                "team_label": v.get("team_label"),
                "referrals": int(v.get("referrals", 0))
            }
        except Exception:
            group_data[str(k)] = {"team_label": v.get("team_label"), "referrals": 0}

    # team_number: prefer explicit team_number; fall back to assigned_number (for solo that may be None)
    team_number = user.get("team_number") if user.get("team_number") is not None else user.get("assigned_number", 1)
    try:
        team_number = int(team_number)
    except Exception:
        team_number = int(user.get("assigned_number", 1))

    team_key = str(team_number)

    # lookup team_info inside group_data; fallback to default structure
    team_info = group_data.get(team_key, {"team_label": f"TEAM{team_number}", "referrals": 0})

    # build sorted mini leaderboard for the user's group
    try:
        normalized_group_teams = {
            str(k): {"team_label": v.get("team_label"), "referrals": int(v.get("referrals", 0))}
            for k, v in group_data.items()
        }
        group_teams = dict(
            sorted(
                normalized_group_teams.items(),
                key=lambda kv: int(kv[1].get("referrals", 0)),
                reverse=True
            )
        )
    except Exception:
        group_teams = group_data

    referral_goal = 10000

    return render_template(
        "progress.html",
        user=user,
        team_info=team_info,
        group_teams=group_teams,
        all_refs=referrals,
        referral_goal=referral_goal,
        TEAM_LINKS=TEAM_LINKS,
        SOLO_LINKS=SOLO_LINKS
    )

@app.route("/public", methods=["POST", "GET"])
def public():
    # attempt an immediate sync so leaderboard is up-to-date
    try:
        result = fetch_contacts_and_update()
    except Exception as e:
        result = {"status": "error", "message": str(e)}

    # optionally return JSON result if requested
    if request.args.get("format") == "json" or request.is_json:
        return jsonify(result)

    referrals = load_json(REF_FILE, {})

    # Pre-sort each group's teams by referrals descending
    sorted_refs = {}
    for group, teams in referrals.items():
        try:
            # teams: dict of team_key -> info
            sorted_list = sorted(teams.items(), key=lambda kv: int(kv[1].get("referrals", 0)), reverse=True)
            sorted_refs[group] = {k: v for k, v in sorted_list}
        except Exception:
            sorted_refs[group] = teams

    return render_template(
        "leaderboard.html",
        all_refs=sorted_refs,
        TEAM_LINKS=TEAM_LINKS,
        SOLO_LINKS=SOLO_LINKS
    )

@app.route("/auth")
def auth():
    # start web-based OAuth flow
    flow = Flow.from_client_secrets_file(CRED_FILE, scopes=SCOPES)
    flow.redirect_uri = url_for("oauth2callback", _external=True)
    auth_url, _ = flow.authorization_url(prompt="consent")
    return redirect(auth_url)

@app.route("/oauth2callback")
def oauth2callback():
    flow = Flow.from_client_secrets_file(CRED_FILE, scopes=SCOPES)
    flow.redirect_uri = url_for("oauth2callback", _external=True)
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    with open(TOKEN_FILE, "w") as token:
        token.write(creds.to_json())
    # run an initial sync immediately after successful auth
    fetch_contacts_and_update()
    return redirect(url_for("public"))

# ---------------------- Admin: sync-now ----------------------
@app.route("/sync-now", methods=["POST", "GET"])
def sync_now():
    if ADMIN_KEY:
        provided = request.args.get("key") or request.form.get("key")
        if not provided or provided != ADMIN_KEY:
            return abort(403, description="Forbidden: invalid admin key")
    else:
        app.logger.warning("ADMIN_KEY not set — /sync-now is unprotected in this environment.")

    result = fetch_contacts_and_update()
    if request.args.get("format") == "json" or request.is_json:
        return jsonify(result)
    return redirect(url_for("public"))

# ---------------------- Admin: migrate existing users to have team_link ----------------------
@app.route("/migrate-team-links", methods=["POST", "GET"])
def migrate_team_links():
    if ADMIN_KEY:
        provided = request.args.get("key") or request.form.get("key")
        if not provided or provided != ADMIN_KEY:
            return abort(403, description="Forbidden: invalid admin key")
    users = load_json(DATA_FILE, [])
    changed = 0
    for u in users:
        if "team_link" not in u or not u.get("team_link"):
            tn = u.get("team_number")
            if tn:
                u["team_link"] = TEAM_LINKS.get(int(tn))
                changed += 1
    if changed > 0:
        save_json(DATA_FILE, users, push_to_github=True)
    return jsonify({"status": "ok", "updated": changed})

# ---------------------- Start ----------------------
if __name__ == "__main__":
    # launch background updater thread only when running directly
    threading.Thread(target=background_updater, daemon=True).start()
    print("✅ Flask app running with automatic Google Contacts sync and team link assignment.")
    app.run(debug=True)
