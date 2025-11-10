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
GITHUB_TOKEN = os.getenv("GITHUB_PAT")                # required for auto-push / read
GITHUB_REPO = os.getenv("GITHUB_REPO", "olamicreas/whatsapp_bot")  # owner/repo
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "master")    # branch to commit to

# ---------------------- Team links (WhatsApp) ----------------------
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

# ---------------------- GitHub helpers ----------------------
def _github_api_headers():
    if not GITHUB_TOKEN:
        return None
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "referral-app-bot"
    }

def _github_get_file_content(repo, path, branch=None):
    """
    Try to fetch file content from GitHub. Tries branch, then 'main', then 'master' (defensive).
    Returns decoded bytes or None on failure.
    """
    headers = _github_api_headers()
    if not headers:
        return None

    branches_to_try = []
    if branch:
        branches_to_try.append(branch)
    # try user branch, then sensible defaults
    for b in [BRANCH for BRANCH in branches_to_try] + ["main", "master"]:
        try:
            url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={b}"
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 200:
                data = r.json()
                content_b64 = data.get("content", "")
                if content_b64:
                    return base64.b64decode(content_b64)
            # 404 or other -> try next branch
        except Exception as e:
            app.logger.debug(f"[GITHUB] fetch {path}@{b} failed: {e}")
            continue
    return None

def _github_get_file_sha(repo, path, branch="master"):
    headers = _github_api_headers()
    if not headers:
        return None
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            return data.get("sha")
        return None
    except Exception as e:
        print(f"[GITHUB] GET file failed: {e}")
        return None

def _github_put_file(repo, path, content_bytes, message, branch="master", sha=None):
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

    try:
        sha = _github_get_file_sha(repo, path, branch=branch)
        result = _github_put_file(repo, path, content, commit_message, branch=branch, sha=sha)
        print(f"[GITHUB] Pushed {path} to {repo}@{branch} (sha: {result.get('content',{}).get('sha')})")
        return {"ok": True, "response": result}
    except Exception as e:
        print(f"[GITHUB] Push failed for {path}: {e}")
        return {"error": str(e)}

# ---------------------- Helpers ----------------------
def load_json(path, default):
    """
    Try to fetch file from GitHub (if configured). If that fails, use local file.
    Returns parsed JSON (default if can't parse).
    """
    # Try GitHub first (always read latest from remote if PAT/repo configured)
    if GITHUB_TOKEN and GITHUB_REPO:
        try:
            content_bytes = _github_get_file_content(GITHUB_REPO, path, branch=GITHUB_BRANCH)
            if content_bytes:
                try:
                    text = content_bytes.decode("utf-8")
                    return json.loads(text)
                except Exception as e:
                    app.logger.warning(f"[WARN] Failed to parse JSON from GitHub for {path}: {e}")
        except Exception as e:
            app.logger.debug(f"[DEBUG] GitHub fetch failed for {path}: {e}")

    # Fallback to local file (create if not exists)
    if not os.path.exists(path):
        with open(path, "w") as f:
            json.dump(default, f)
        return default

    with open(path, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return default

def save_json(path, data, push_to_github=True):
    """
    Save JSON locally and optionally push to GitHub.
    """
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"[ERROR] Failed writing {path}: {e}")
        raise

    if push_to_github and path in (DATA_FILE, REF_FILE) and GITHUB_TOKEN and GITHUB_REPO:
        try:
            res = push_file_to_github(path, commit_message=f"Auto-update {path}")
            return res
        except Exception as e:
            print(f"[WARN] GitHub push failed for {path}: {e}")
            return {"error": str(e)}
    return {"saved_local": True}

def get_credentials():
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

def normalize_ref_id(s):
    return re.sub(r"\s+", "_", s.strip().lower())

# ---------------------- Team & Registration logic ----------------------
def assign_team_global():
    users = load_json(DATA_FILE, [])
    team_number = (len(users) % TEAMS_PER_GROUP) + 1
    return team_number

def assign_link(reg_type):
    if reg_type == "team":
        users = [u for u in load_json(DATA_FILE, []) if u.get("registration_type") == "team"]
        team_number = (len(users) % TEAMS_PER_GROUP) + 1
        return team_number, TEAM_LINKS.get(team_number)
    elif reg_type == "solo":
        users = [u for u in load_json(DATA_FILE, []) if u.get("registration_type") == "solo"]
        solo_number = (len(users) % SOLO_COUNT) + 1
        return solo_number, SOLO_LINKS.get(solo_number)
    else:
        return 1, TEAM_LINKS.get(1)

def team_label(group_name, team_number):
    return f"TEAM{int(team_number)}"

# ---------------------- Google matching logic ----------------------
def contact_mentions_team(contact, group_name, team_number):
    token_pattern = re.compile(r"\bteam[\s_\-]*0*{}\b".format(int(team_number)), flags=re.I)
    texts = []
    if "names" in contact:
        for n in contact["names"]:
            if n.get("displayName"):
                texts.append(n.get("displayName"))
    if "biographies" in contact:
        for b in contact["biographies"]:
            if b.get("value"):
                texts.append(b.get("value"))
    if "organizations" in contact:
        for o in contact["organizations"]:
            if o.get("name"):
                texts.append(o.get("name"))
            if o.get("title"):
                texts.append(o.get("title"))
    if "userDefined" in contact:
        for ud in contact["userDefined"]:
            if isinstance(ud, dict):
                if ud.get("value"):
                    texts.append(ud.get("value"))
                elif ud.get("key") and ud.get("value"):
                    texts.append(f"{ud.get('key')} {ud.get('value')}")
            else:
                texts.append(str(ud))

    combined = " ".join([t for t in texts if t]).lower()
    if token_pattern.search(combined):
        return True
    normalized_label = f"team{int(team_number)}"
    if normalized_label in combined.replace(" ", ""):
        return True
    if group_name and group_name.strip():
        if group_name.strip().lower() in combined:
            if re.search(r"team[\s_\-]*0*{}".format(int(team_number)), combined, flags=re.I):
                return True
    return False

def contact_mentions_ref(contact, ref_index):
    """
    Look for REF001..REFNNN markers in text values.
    Accepts patterns like REF001, REF 001, ref001, ref-001
    """
    pat = re.compile(r"\bref[\s\-_]*0*{}\b".format(int(ref_index)), flags=re.I)
    texts = []
    for field in ["names", "biographies", "organizations", "userDefined"]:
        if field in contact:
            for item in contact[field]:
                if isinstance(item, dict):
                    for k in ["displayName", "value", "name", "title"]:
                        if item.get(k):
                            texts.append(str(item.get(k)))
                else:
                    texts.append(str(item))
    combined = " ".join(t for t in texts if t)
    combined_clean = re.sub(r"[^\w\s]", " ", combined)  # remove punctuation to be lenient
    return bool(pat.search(combined_clean))

# ---------------------- Sync & aggregation ----------------------
def fetch_contacts_and_update():
    creds = get_credentials()
    if not creds:
        print("[INFO] No credentials yet. Visit /auth to connect Google Contacts.")
        return {"status": "no-credentials"}

    try:
        service = build("people", "v1", credentials=creds)
        results = service.people().connections().list(
            resourceName="people/me",
            personFields="names,emailAddresses,organizations,biographies,userDefined",
            pageSize=2000
        ).execute()

        connections = results.get("connections", [])
        users = load_json(DATA_FILE, [])

        # Prepare groups -> teams structure from registered users
        groups = {}
        for u in users:
            group = (u.get("group") or "ALL").strip()
            team_num = int(u.get("team_number") or u.get("assigned_number") or 1)
            groups.setdefault(group, {})
            groups[group].setdefault(team_num, {"team_label": f"TEAM{team_num}", "count": 0})

        # SOLO refs
        SOLO_MAX = SOLO_COUNT
        solo_refs = {i: {"ref_label": f"REF{str(i).zfill(3)}", "count": 0} for i in range(1, SOLO_MAX + 1)}

        # Local helper for punctuation-tolerant team detection
        def contact_mentions_team_local(contact, team_number):
            token_pattern = re.compile(r"TEAM\s*{}\b".format(team_number), flags=re.I)
            texts = []
            for field in ["names", "biographies", "organizations", "userDefined"]:
                if field in contact:
                    for item in contact[field]:
                        for key in ["displayName", "value", "name", "title"]:
                            if isinstance(item, dict) and item.get(key):
                                texts.append(item.get(key))
                            elif not isinstance(item, dict) and item:
                                texts.append(str(item))
            combined = " ".join([t for t in texts if t])
            combined_clean = re.sub(r"[^\w\s]", "", combined)
            return bool(token_pattern.search(combined_clean))

        # Scan contacts
        for contact in connections:
            for group, teams in groups.items():
                for team_num in list(teams.keys()):
                    if contact_mentions_team(contact, group, team_num) or contact_mentions_team_local(contact, team_num):
                        teams[team_num]["count"] = int(teams[team_num].get("count") or 0) + 1
                        try:
                            name = contact.get("names", [{"displayName": "Unknown"}])[0].get("displayName", "Unknown")
                        except Exception:
                            name = "Unknown"
                        print(f"[MATCH] {name} counted for {group} TEAM{team_num}")

            # SOLO refs
            for i in range(1, SOLO_MAX + 1):
                if contact_mentions_ref(contact, i):
                    solo_refs[i]["count"] = int(solo_refs[i].get("count") or 0) + 1
                    try:
                        name = contact.get("names", [{"displayName": "Unknown"}])[0].get("displayName", "Unknown")
                    except Exception:
                        name = "Unknown"
                    print(f"[MATCH] {name} counted for SOLO {solo_refs[i]['ref_label']}")

        # Build referrals dict
        referrals = {}
        for group, teams in groups.items():
            referrals[group] = {}
            for team_num, info in teams.items():
                count = int(info.get("count") or 0)
                referrals[group][str(team_num)] = {
                    "team_label": info.get("team_label", f"TEAM{team_num}"),
                    "referrals": count
                }

        # Add SOLO group
        referrals.setdefault("SOLO", {})
        for i, info in solo_refs.items():
            count = int(info.get("count") or 0)
            key = f"REF{str(i).zfill(3)}"
            referrals["SOLO"][key] = {
                "team_label": info.get("ref_label", key),
                "referrals": count
            }

        # Save locally and push to GitHub if configured
        save_json(REF_FILE, referrals, push_to_github=True)
        print("[AUTO-UPDATE] Referral counts per group/team and SOLO synced from Google Contacts.")
        return {"status": "ok", "groups": len(referrals)}

    except Exception as e:
        print(f"[ERROR] Failed to update referrals: {e}")
        return {"status": "error", "message": str(e)}
        
def background_updater():
    while True:
        fetch_contacts_and_update()
        time.sleep(UPDATE_INTERVAL)

# ---------------------- Routes ----------------------
@app.route("/")
def index():
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
    label = f"TEAM{assigned_number}" if reg_type == "team" else f"REF{int(assigned_number):03d}"

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

    # Merge into existing users list and save
    users.append(new_user)
    save_json(DATA_FILE, users, push_to_github=True)

    # Initialize or update referrals for team users
    referrals = load_json(REF_FILE, {})
    referrals.setdefault("ALL", {})

    if reg_type == "team":
        referrals["ALL"].setdefault(str(assigned_number), {"team_label": label, "referrals": 0})
    else:
        referrals.setdefault("SOLO", {})
        referrals["SOLO"].setdefault(ref_id, {"team_label": label, "referrals": 0})

    save_json(REF_FILE, referrals, push_to_github=True)

    return redirect(url_for("progress", ref_id=ref_id))

@app.route("/progress/<ref_id>", methods=["GET", "POST"])
def progress(ref_id):
    # best-effort quick sync
    try:
        fetch_contacts_and_update()
    except Exception as e:
        print("[WARN] Auto-sync failed:", e)

    users = load_json(DATA_FILE, [])
    # match using normalized ref_id for reliability
    norm = normalize_ref_id(ref_id)
    user = next((u for u in users if normalize_ref_id(u.get("ref_id", "")) == norm), None)
    if not user:
        return "Invalid referral ID", 404

    referrals = load_json(REF_FILE, {})

    group_key = (user.get("group") or "").strip() or "ALL"
    raw_group_data = referrals.get(group_key, {})

    # ensure group data keys are strings
    group_data = {str(k): v for k, v in raw_group_data.items()}

    # ✅ SAFE conversion for team_number
    team_number = user.get("team_number")
    if team_number is None:
        team_number = user.get("assigned_number", 1)
    try:
        team_number = int(team_number)
    except (TypeError, ValueError):
        team_number = 1

    team_key = str(team_number)

    team_info = group_data.get(team_key, {"team_label": f"TEAM{team_number}", "referrals": 0})

    try:
        team_info["referrals"] = int(team_info.get("referrals", 0))
    except Exception:
        team_info["referrals"] = 0

    # prepare mini leaderboard
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
        TEAM_LINKS=TEAM_LINKS
    )

@app.route("/public", methods=["POST", "GET"])
def public():
    # Always fetch fresh data from GitHub and update referral stats
    result = fetch_contacts_and_update()

    # Return JSON if requested
    if request.args.get("format") == "json" or request.is_json:
        return jsonify(result)

    referrals = load_json(REF_FILE, {})

    # ✅ Pre-sort groups safely (works for both TEAM and SOLO)
    sorted_refs = {}
    for group, teams in referrals.items():
        try:
            # Ensure we’re iterating a dict
            if isinstance(teams, dict):
                safe_teams = {}
                for k, v in teams.items():
                    # normalize and ensure integer referral count
                    try:
                        ref_count = int(v.get("referrals", 0) or 0)
                    except (TypeError, ValueError):
                        ref_count = 0
                    safe_teams[str(k)] = {
                        "team_label": v.get("team_label") or f"TEAM{k}",
                        "referrals": ref_count
                    }

                # Sort safely by referral count
                sorted_list = sorted(safe_teams.items(), key=lambda kv: kv[1]["referrals"], reverse=True)
                sorted_refs[group] = {k: v for k, v in sorted_list}
            else:
                sorted_refs[group] = teams
        except Exception as e:
            print(f"[WARN] Failed to sort group {group}: {e}")
            sorted_refs[group] = teams

    # ✅ Render the leaderboard safely
    return render_template(
        "leaderboard.html",
        all_refs=sorted_refs,
        TEAM_LINKS=TEAM_LINKS,
        SOLO_LINKS=SOLO_LINKS
    )

@app.route("/auth")
def auth():
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
    fetch_contacts_and_update()
    return redirect(url_for("public"))

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

if __name__ == "__main__":
    # start background updater (daemon)
    threading.Thread(target=background_updater, daemon=True).start()
    print("✅ Flask app running with GitHub-backed JSON and Google Contacts sync.")
    app.run(debug=True)
