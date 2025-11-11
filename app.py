# app.py
import os
import json
import threading
import time
import re
import base64
import requests
from datetime import datetime, timedelta
from urllib.parse import quote
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
DAILY_FILE = os.getenv("DAILY_FILE", "daily_refs.json")  # daily snapshots storage

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

# Contest end date: set CONTEST_END_ISO (ISO format) or CONTEST_LENGTH_DAYS (int)
CONTEST_END_ISO = os.getenv("CONTEST_END_ISO")
CONTEST_LENGTH_DAYS = int(os.getenv("CONTEST_LENGTH_DAYS", "30"))

# ---------------------- Team links (WhatsApp) ----------------------
TEAM_LINKS = {
    1: "https://wa.link/0a7pj3",
    2: "https://wa.link/uiv1az",
    3: "https://wa.link/i7rwku",
    4: "https://wa.link/47ly4h",
    5: "https://wa.link/xfq5gn"
}

# Base SOLO_LINKS (existing ones)
SOLO_LINKS = {
    1: "https://wa.link/b6kecz",  # Ref 001
    2: "https://wa.link/stv0mr",  # Ref 002
    3: "https://wa.link/yup4kc",  # Ref 003 / Mr Heep
    4: "https://wa.link/ze4vj4",  # Ref 004
    5: "https://wa.link/109mvf"   # Ref 005
}

# We'll extend SOLO_LINKS programmatically with REF006..REF025 using the same phone/message pattern.
SOLO_PHONE = "2347010528330"  # target WhatsApp number for the generated links
SOLO_START = 6
SOLO_END = 25  # inclusive

# ---------------------- Constants ----------------------
TEAMS_PER_GROUP = 5
SOLO_COUNT = 5

# ---------------------- Utility helpers ----------------------
def safe_int(x, default=0):
    try:
        # treat bool as invalid for counting
        if isinstance(x, bool):
            return default
        if x is None:
            return default
        return int(x)
    except Exception:
        try:
            return int(str(x).strip() or 0)
        except Exception:
            return default

# generate & merge extra SOLO links (doesn't create a separate list)
def _extend_solo_links(start=SOLO_START, end=SOLO_END, phone=SOLO_PHONE):
    for i in range(start, end + 1):
        if i in SOLO_LINKS:
            continue
        ref = f"{i:03d}"
        text = f"hello mr heep, i am from ref {ref}. my name is"
        url = f"https://wa.me/{phone}?text={quote(text)}"
        SOLO_LINKS[i] = url

_extend_solo_links()

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
    Fetch file content bytes from GitHub. Tries branch (if provided),
    then configured GITHUB_BRANCH, then 'main', then 'master'.
    Returns decoded bytes or None on failure.
    """
    headers = _github_api_headers()
    if not headers:
        return None

    branches_to_try = []
    if branch:
        branches_to_try.append(branch)
    if GITHUB_BRANCH:
        branches_to_try.append(GITHUB_BRANCH)
    branches_to_try.extend(["main", "master"])

    for b in branches_to_try:
        try:
            url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={b}"
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 200:
                data = r.json()
                content_b64 = data.get("content", "")
                if content_b64:
                    payload = "".join(content_b64.splitlines())
                    return base64.b64decode(payload)
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
        app.logger.debug(f"[GITHUB] GET file sha failed: {e}")
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
        app.logger.info("[GITHUB] Skipping push: GITHUB_TOKEN or GITHUB_REPO not set.")
        return {"skipped": True}

    if not os.path.exists(path):
        app.logger.info(f"[GITHUB] Local file {path} not found, skipping push.")
        return {"skipped": True}

    try:
        with open(path, "rb") as f:
            content = f.read()
    except Exception as e:
        app.logger.warning(f"[GITHUB] Failed to read {path}: {e}")
        return {"error": str(e)}

    repo = GITHUB_REPO
    branch = GITHUB_BRANCH or "master"
    commit_message = commit_message or f"Auto-update {os.path.basename(path)}"

    try:
        sha = _github_get_file_sha(repo, path, branch=branch)
        result = _github_put_file(repo, path, content, commit_message, branch=branch, sha=sha)
        app.logger.info(f"[GITHUB] Pushed {path} to {repo}@{branch}")
        return {"ok": True, "response": result}
    except Exception as e:
        app.logger.warning(f"[GITHUB] Push failed for {path}: {e}")
        return {"error": str(e)}

# ---------------------- Local / GitHub JSON helpers ----------------------
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
        try:
            with open(path, "w") as f:
                json.dump(default, f)
        except Exception:
            pass
        return default

    with open(path, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return default

def save_json(path, data, push_to_github=True):
    """
    Save JSON locally and optionally push to GitHub.
    Supports DATA_FILE, REF_FILE and DAILY_FILE when push_to_github=True.
    """
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        app.logger.error(f"[ERROR] Failed writing {path}: {e}")
        raise

    if push_to_github and GITHUB_TOKEN and GITHUB_REPO and path in (DATA_FILE, REF_FILE, DAILY_FILE):
        try:
            res = push_file_to_github(path, commit_message=f"Auto-update {path}")
            return res
        except Exception as e:
            app.logger.warning(f"[WARN] GitHub push failed for {path}: {e}")
            return {"error": str(e)}
    return {"saved_local": True}

# ---------------------- Google credentials ----------------------
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
    return re.sub(r"\s+", "_", (s or "").strip().lower())

# ---------------------- Team & Registration logic ----------------------
def assign_team_global():
    users = load_json(DATA_FILE, [])
    team_number = (len([u for u in users if (u.get("registration_type") or "").strip().lower() == "team"]) % TEAMS_PER_GROUP) + 1
    return team_number

def assign_link(reg_type):
    if reg_type == "team":
        users = [u for u in load_json(DATA_FILE, []) if (u.get("registration_type") or "").strip().lower() == "team"]
        team_number = (len(users) % TEAMS_PER_GROUP) + 1
        return team_number, TEAM_LINKS.get(team_number)
    elif reg_type == "solo":
        users = [u for u in load_json(DATA_FILE, []) if (u.get("registration_type") or "").strip().lower() == "solo"]
        solo_number = (len(users) % SOLO_COUNT) + 1
        # if generated range goes beyond SOLO_COUNT, we still support generated links in SOLO_LINKS
        return solo_number, SOLO_LINKS.get(solo_number)
    else:
        return 1, TEAM_LINKS.get(1)

def team_label(group_name, team_number):
    return f"TEAM{int(team_number)}"

# ---------------------- Contact matching helpers ----------------------
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
    combined_clean = re.sub(r"[^\w\s]", " ", combined)
    return bool(pat.search(combined_clean))

# ---------------------- Sync & aggregation ----------------------
def fetch_contacts_and_update():
    creds = get_credentials()
    if not creds:
        app.logger.info("[INFO] No credentials yet. Visit /auth to connect Google Contacts.")
        return {"status": "no-credentials"}

    try:
        service = build("people", "v1", credentials=creds)
        results = service.people().connections().list(
            resourceName="people/me",
            personFields="names,emailAddresses,organizations,biographies,userDefined",
            pageSize=2000
        ).execute()

        connections = results.get("connections", []) or []
        users = load_json(DATA_FILE, []) or []

        # Prepare groups -> teams structure from registered users (only TEAM registrations)
        groups = {}
        for u in users:
            regt = (u.get("registration_type") or "").strip().lower()
            if regt != "team":
                continue  # skip solo users for team counts
            group = (u.get("group") or "ALL").strip()
            team_num = safe_int(u.get("team_number") or u.get("assigned_number") or 1)
            groups.setdefault(group, {})
            groups[group].setdefault(team_num, {"team_label": f"TEAM{team_num}", "count": 0})

        # SOLO refs
        SOLO_MAX = max(SOLO_END, SOLO_COUNT)
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
                        teams[team_num]["count"] = safe_int(teams[team_num].get("count")) + 1
                        try:
                            name = contact.get("names", [{"displayName": "Unknown"}])[0].get("displayName", "Unknown")
                        except Exception:
                            name = "Unknown"
                        app.logger.debug(f"[MATCH] {name} counted for {group} TEAM{team_num}")

            # SOLO refs: REF001..REFNN
            for i in range(1, SOLO_MAX + 1):
                if contact_mentions_ref(contact, i):
                    solo_refs[i]["count"] = safe_int(solo_refs[i].get("count")) + 1
                    try:
                        name = contact.get("names", [{"displayName": "Unknown"}])[0].get("displayName", "Unknown")
                    except Exception:
                        name = "Unknown"
                    app.logger.debug(f"[MATCH] {name} counted for SOLO {solo_refs[i]['ref_label']}")

        # Build referrals dict
        referrals = {}
        for group, teams in groups.items():
            referrals[group] = {}
            for team_num, info in teams.items():
                count = safe_int(info.get("count"))
                referrals[group][str(team_num)] = {
                    "team_label": info.get("team_label", f"TEAM{team_num}"),
                    "referrals": count
                }

        # Add SOLO group
        referrals.setdefault("SOLO", {})
        for i, info in solo_refs.items():
            count = safe_int(info.get("count"))
            key = f"REF{str(i).zfill(3)}"
            referrals["SOLO"][key] = {
                "team_label": info.get("ref_label", key),
                "referrals": count
            }

        # Save locally and push to GitHub if configured
        save_json(REF_FILE, referrals, push_to_github=True)
        app.logger.info("[AUTO-UPDATE] Referral counts per group/team and SOLO synced from Google Contacts.")
        return {"status": "ok", "groups": len(referrals)}

    except Exception as e:
        app.logger.error(f"[ERROR] Failed to update referrals: {e}")
        return {"status": "error", "message": str(e)}

def background_updater():
    while True:
        fetch_contacts_and_update()
        time.sleep(UPDATE_INTERVAL)

# ---------------------- Daily snapshot helpers & routes ----------------------
def build_today_snapshot():
    """
    Read REF_FILE and produce {'date': 'YYYY-MM-DD', 'counts': {label: count}}
    """
    refs = load_json(REF_FILE, {})
    counts = {}

    # Teams: prefer 'ALL' group if present, else aggregate across groups
    if isinstance(refs, dict):
        if "ALL" in refs and isinstance(refs["ALL"], dict):
            source = refs["ALL"]
            for k, v in source.items():
                c = safe_int((v or {}).get("referrals"))
                try:
                    label = f"TEAM{int(k)}"
                except Exception:
                    label = str((v or {}).get("team_label") or f"TEAM{str(k)}")
                counts[label] = c
        else:
            # aggregate teams from any group keys
            for g, teams in refs.items():
                if g == "SOLO" or not isinstance(teams, dict):
                    continue
                for k, v in teams.items():
                    c = safe_int((v or {}).get("referrals"))
                    try:
                        label = f"TEAM{int(k)}"
                    except Exception:
                        label = str((v or {}).get("team_label") or f"TEAM{str(k)}")
                    counts[label] = counts.get(label, 0) + c

        # Solos
        solo = refs.get("SOLO", {}) or {}
        for k, v in solo.items():
            c = safe_int((v or {}).get("referrals"))
            label = str((v or {}).get("team_label") or k)
            counts[label] = counts.get(label, 0) + c

    # Ensure teams known in DATA_FILE are present with zero if missing
    users = load_json(DATA_FILE, []) or []
    if isinstance(users, list):
        for u in users:
            tl = (u.get("team_label") or "").strip()
            if tl:
                counts.setdefault(tl, 0)

    date_str = datetime.utcnow().date().isoformat()
    return {"date": date_str, "counts": counts}

def read_daily_file():
    return load_json(DAILY_FILE, {"days": []})

def append_daily_snapshot(snapshot):
    """
    Append today's snapshot only if not already present for today's date.
    Keep at most 90 entries.
    Returns (ok: bool, reason: str)
    """
    if not isinstance(snapshot, dict) or "date" not in snapshot or "counts" not in snapshot:
        return False, "invalid-snapshot"

    data = read_daily_file()
    days = data.get("days", [])

    # avoid duplicates
    if any(d.get("date") == snapshot["date"] for d in days):
        return False, "duplicate-date"

    days.append(snapshot)
    # cap at 90
    if len(days) > 90:
        days = days[-90:]
    data["days"] = days

    # save and push to GitHub (we now allow DAILY_FILE to be pushed)
    try:
        save_json(DAILY_FILE, data, push_to_github=True)
        return True, "saved"
    except Exception as e:
        app.logger.error(f"[ERROR] append_daily_snapshot save failed: {e}")
        return False, str(e)

# ---------------------- Routes ----------------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/register", methods=["POST"])
def register():
    # server-side admin password (set in environment)
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "ContactBatch321!")
    supplied_pw = request.form.get("admin_password", "")

    if supplied_pw != ADMIN_PASSWORD:
        return render_template("index.html", error="Invalid admin password. Registration blocked.")

    name = request.form.get("name", "").strip()
    reg_type = request.form.get("registration_type", "team").strip().lower()
    if not name:
        return redirect(url_for("index"))

    ref_id = normalize_ref_id(name)

    users = load_json(DATA_FILE, [])
    existing = next((u for u in users if normalize_ref_id(u.get("ref_id", "")) == ref_id), None)
    if existing:
        return redirect(url_for("progress", ref_id=ref_id))

    try:
        assigned_number, assigned_link = assign_link(reg_type)
    except Exception:
        assigned_number, assigned_link = (1, TEAM_LINKS.get(1))

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

    users.append(new_user)
    save_json(DATA_FILE, users, push_to_github=True)

    referrals = load_json(REF_FILE, {})
    referrals.setdefault("ALL", {})

    if reg_type == "team":
        referrals["ALL"].setdefault(str(assigned_number), {"team_label": label, "referrals": 0})
    else:
        referrals.setdefault("SOLO", {})
        # store by canonical REF label so counting matches
        referrals["SOLO"].setdefault(f"REF{int(assigned_number):03d}", {"team_label": label, "referrals": 0})

    save_json(REF_FILE, referrals, push_to_github=True)
    return redirect(url_for("progress", ref_id=ref_id))

@app.route("/progress/<ref_id>", methods=["GET", "POST"])
def progress(ref_id):
    # try quick sync but ignore failure
    try:
        fetch_contacts_and_update()
    except Exception as e:
        app.logger.warning("[WARN] Auto-sync failed: %s", e)

    users = load_json(DATA_FILE, [])
    norm = normalize_ref_id(ref_id)
    user = next((u for u in users if normalize_ref_id(u.get("ref_id", "")) == norm), None)
    if not user:
        return "Invalid referral ID", 404

    referrals = load_json(REF_FILE, {})

    reg_type = (user.get("registration_type") or "").strip().lower()
    if not reg_type:
        tl = (user.get("team_label") or "").upper()
        reg_type = "solo" if tl.startswith("REF") else "team"

    group_key = (user.get("group") or "").strip() or "ALL"
    raw_group_data = referrals.get(group_key, referrals.get("ALL", {}))
    group_data = {str(k): v for k, v in (raw_group_data or {}).items()}

    # determine team / solo info
    if reg_type == "solo":
        solo_map = referrals.get("SOLO", {}) or {}
        candidates = []
        tl = (user.get("team_label") or "").strip()
        if tl:
            candidates.append(tl)
        if user.get("ref_id"):
            candidates.append(user.get("ref_id"))
        if user.get("assigned_number") is not None:
            candidates.append(f"REF{int(user.get('assigned_number')):03d}")

        team_info = None
        for c in candidates:
            if not c:
                continue
            if c in solo_map:
                team_info = solo_map[c]; break
            cu = str(c).upper()
            if cu in solo_map:
                team_info = solo_map[cu]; break
            cl = str(c).lower()
            if cl in solo_map:
                team_info = solo_map[cl]; break

        if not team_info:
            team_info = {
                "team_label": user.get("team_label", f"REF{int(user.get('assigned_number',1)):03d}"),
                "referrals": 0
            }

        team_info["referrals"] = safe_int(team_info.get("referrals", 0))
        # solo goal remains 1,000
        referral_goal = 1000

    else:
        # TEAM path
        team_number = user.get("team_number") if user.get("team_number") is not None else user.get("assigned_number")
        try:
            team_number = int(team_number)
        except Exception:
            team_number = 1
        team_key = str(team_number)
        team_info = group_data.get(team_key, {"team_label": user.get("team_label", f"TEAM{team_number}"), "referrals": 0})
        team_info["referrals"] = safe_int(team_info.get("referrals", 0))

        # --- special per-team goal logic ---
        # default team goal:
        default_team_goal = 10000
        # override for specific teams:
        TEAM_GOALS = {
            2: 100000,   # Team 2 has 100k goal
            # add more overrides here if needed, e.g. 3: 50000
        }
        referral_goal = TEAM_GOALS.get(team_number, default_team_goal)
        # -------------------------------------

    try:
        normalized_group_teams = {
            str(k): {"team_label": v.get("team_label"), "referrals": safe_int(v.get("referrals", 0))}
            for k, v in (group_data or {}).items()
        }
        group_teams = dict(sorted(normalized_group_teams.items(), key=lambda kv: kv[1]["referrals"], reverse=True))
    except Exception:
        group_teams = group_data

    # compute contest_end_iso automatically from yesterday + 30 days
    # contest started yesterday and runs for 30 days
    try:
        yesterday = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        contest_end = yesterday + timedelta(days=30)
        contest_end_iso = contest_end.isoformat()
    except Exception:
        # graceful fallback: 30 days from now
        contest_end_iso = (datetime.utcnow() + timedelta(days=30)).isoformat()

    return render_template(
        "progress.html",
        user=user,
        team_info=team_info,
        group_teams=group_teams,
        all_refs=referrals,
        referral_goal=referral_goal,
        TEAM_LINKS=TEAM_LINKS,
        SOLO_LINKS=SOLO_LINKS,
        contest_end_iso=contest_end_iso
    )
    
@app.route("/public", methods=["POST", "GET"])
def public():
    # Always fetch fresh data first (best-effort)
    try:
        result = fetch_contacts_and_update()
    except Exception as e:
        result = {"status": "error", "message": str(e)}

    if request.args.get("format") == "json" or request.is_json:
        return jsonify(result)

    referrals = load_json(REF_FILE, {})

    sorted_refs = {}
    for group, teams in (referrals or {}).items():
        try:
            if isinstance(teams, dict):
                safe_teams = {}
                for k, v in teams.items():
                    ref_count = safe_int((v or {}).get("referrals", 0))
                    safe_teams[str(k)] = {
                        "team_label": (v or {}).get("team_label") or f"TEAM{k}",
                        "referrals": ref_count
                    }
                sorted_list = sorted(safe_teams.items(), key=lambda kv: kv[1]["referrals"], reverse=True)
                sorted_refs[group] = {k: v for k, v in sorted_list}
            else:
                sorted_refs[group] = teams
        except Exception as e:
            app.logger.warning(f"[WARN] Failed to sort group {group}: {e}")
            sorted_refs[group] = teams

    # render leaderboard template
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

# ---------------------- Daily snapshot display & snapshot endpoint ----------------------
@app.route("/daily-progress", methods=["GET"])
def daily_progress():
    daily = read_daily_file()
    days = daily.get("days", [])
    if not days:
        today_snapshot = build_today_snapshot()
        append_daily_snapshot(today_snapshot)
        daily = read_daily_file()
        days = daily.get("days", [])

    padded_days = list(days)[:]
    while len(padded_days) < 30:
        padded_days.append({"date": None, "counts": {}})

    # helper: robust int conversion (safe to re-declare here)
    def safe_int_local(v):
        try:
            return int(v)
        except Exception:
            try:
                return int(float(v))
            except Exception:
                return 0

    refs = load_json(REF_FILE, {})
    labels_set = set()
    for k in (refs.get("ALL") or {}).keys():
        try:
            labels_set.add(f"TEAM{int(k)}")
        except Exception:
            labels_set.add(f"TEAM{str(k)}")
    for k in (refs.get("SOLO") or {}).keys():
        labels_set.add(str(k))
    for d in days:
        for label in d.get("counts", {}).keys():
            labels_set.add(str(label))

    users = load_json(DATA_FILE, []) or []
    label_to_name = {}
    for u in users:
        lbl = (u.get("team_label") or "").strip()
        if lbl:
            label_to_name[lbl] = u.get("name") or lbl
        tn = u.get("team_number")
        if tn is not None:
            label_to_name[f"TEAM{int(tn)}"] = label_to_name.get(f"TEAM{int(tn)}", f"Team {tn}")

    # Build per-label rows (Day 1..30 counts + total)
    rows = []
    for label in sorted(labels_set):
        day_counts = []
        total = 0
        for d in padded_days:
            c = 0
            if d.get("counts") and label in d["counts"]:
                c = safe_int_local(d["counts"][label])
            day_counts.append(c)
            total += c
        rows.append({"label": label, "name": label_to_name.get(label, label), "day_counts": day_counts, "total": total})

    # latest recorded day index (0-based)
    latest_index = max(0, len(days) - 1)

    # sort rows for display by that latest day
    rows_sorted_by_latest = sorted(rows, key=lambda r: r["day_counts"][latest_index], reverse=True)
    totals_sorted = sorted(rows, key=lambda r: r["total"], reverse=True)
    day_dates = [d.get("date") for d in padded_days]

    # compute daily_totals (Day 1..30 totals) and overall_total
    daily_totals = []
    for i in range(30):
        day_sum = sum((row["day_counts"][i] if i < len(row["day_counts"]) else 0) for row in rows)
        daily_totals.append(day_sum)
    overall_total = sum(daily_totals)

    return render_template(
        "daily_progress.html",
        day_dates=day_dates,
        rows=rows_sorted_by_latest,
        totals_sorted=totals_sorted,
        latest_index=latest_index,
        daily_totals=daily_totals,
        overall_total=overall_total
    )

@app.route("/daily-progress/snapshot", methods=["POST"])
def daily_progress_snapshot():
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "ContactBatch321!")
    pw = request.form.get("admin_password") or request.headers.get("X-Admin-Password", "")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False, "reason": "forbidden"}), 403
    snapshot = build_today_snapshot()
    ok, reason = append_daily_snapshot(snapshot)
    return jsonify({"ok": ok, "reason": reason, "date": snapshot["date"]})

# ---------------------- Start ----------------------
if __name__ == "__main__":
    # start background updater (daemon)
    threading.Thread(target=background_updater, daemon=True).start()
    app.logger.info("✅ Flask app running with GitHub-backed JSON and Google Contacts sync.")
    app.run(debug=True)
