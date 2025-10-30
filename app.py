import os, json, threading, time, re, requests
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, abort
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "super_secret_key")



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

# ---------------------- Team links (WhatsApp) ----------------------
TEAM_LINKS = {
    1: "https://wa.link/lrg1il",
    2: "https://wa.link/1trxu8",
    3: "https://wa.link/x1z2ey",
    4: "https://wa.link/rdg24q",
    5: "https://wa.link/6sgqe8",
    6: "https://wa.link/hf0q2j",
    7: "https://wa.link/4mormj",
    8: "https://wa.link/6b6k23",
    9: "https://wa.link/h0xegq",
    10: "https://wa.link/943a6n"
}

# ---------------------- Helpers ----------------------
def load_json(path, default):
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

def team_label(group_name, team_number):
    # unified team label format: TEAM1, TEAM2, ...
    return f"TEAM{int(team_number)}"


# ---------------------- Google matching logic ----------------------
def contact_mentions_team(contact, group_name, team_number):
    """
    Look for TEAM matches in many forms:
      - TEAM1
      - team1
      - team 1
      - team-1
      - team_01
      - Team 01
    """
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
            pageSize=2000   # ✅ max allowed
        ).execute()

        connections = results.get("connections", [])
        users = load_json(DATA_FILE, [])

        # prepare groups -> teams structure from registered users
        groups = {}
        for u in users:
            group = u.get("group", "ALL").strip()
            team_num = u.get("team_number", 1)
            groups.setdefault(group, {})
            groups[group].setdefault(team_num, {"team_label": f"TEAM{team_num}", "count": 0})

        # helper to check if a contact mentions a team
        def contact_mentions_team(contact, team_number):
            # flexible regex: TEAM1, TEAM 1, TEAM1., TEAM1!
            token_pattern = re.compile(r"TEAM\s*{}\b".format(team_number), flags=re.I)

            texts = []
            for field in ["names", "biographies", "organizations", "userDefined"]:
                if field in contact:
                    for item in contact[field]:
                        # collect all possible string values
                        for key in ["displayName", "value", "name", "title"]:
                            if key in item and item[key]:
                                texts.append(item[key])

            combined = " ".join([t for t in texts if t])
            combined_clean = re.sub(r"[^\w\s]", "", combined)  # remove punctuation
            if token_pattern.search(combined_clean):
                return True
            return False

        # scan contacts and increment team counts when matched
        for contact in connections:
            for group, teams in groups.items():
                for team_num in list(teams.keys()):
                    if contact_mentions_team(contact, team_num):
                        teams[team_num]["count"] += 1
                        # debug log
                        name = contact.get("names", [{"displayName": "Unknown"}])[0]["displayName"]
                        print(f"[MATCH] {name} counted for {group} TEAM{team_num}")

        # build referrals dict saved to REF_FILE
        referrals = {}
        for group, teams in groups.items():
            referrals[group] = {}
            for team_num, info in teams.items():
                referrals[group][str(team_num)] = {
                    "team_label": info["team_label"],
                    "referrals": info["count"]
                }

        save_json(REF_FILE, referrals)
        print("[AUTO-UPDATE] Referral counts per group/team synced from Google Contacts.")
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
    name = request.form["name"].strip()
    if not name:
        return redirect(url_for("index"))

    ref_id = normalize_ref_id(name)

    users = load_json(DATA_FILE, [])
    if any(user["ref_id"] == ref_id for user in users):
        return redirect(url_for("progress", ref_id=ref_id))

    # assign team globally
    team_number = assign_team_global()
    label = f"TEAM {team_number}"
    team_link = TEAM_LINKS.get(team_number)

    new_user = {
        "name": name,
        "ref_id": ref_id,
        "team_number": team_number,
        "team_label": label,
        "team_link": team_link,
        "registered_at": int(time.time())
    }
    users.append(new_user)
    save_json(DATA_FILE, users)

    # ensure referrals structure has the team initialized
    referrals = load_json(REF_FILE, {})
    referrals.setdefault("ALL", {})
    referrals["ALL"].setdefault(str(team_number), {"team_label": label, "referrals": 0})
    save_json(REF_FILE, referrals)

    return redirect(url_for("progress", ref_id=ref_id))

@app.route("/progress/<ref_id>", methods=["GET", "POST"])
def progress(ref_id):
    # Optional: auto-sync from Google Contacts before reading REF_FILE
    try:
        fetch_contacts_and_update()
    except Exception as e:
        print("[WARN] Auto-sync failed:", e)

    # Load users
    users = load_json(DATA_FILE, [])
    user = next((u for u in users if u["ref_id"] == ref_id), None)
    if not user:
        return "Invalid referral ID", 404

    # Load referral counts
    referrals = load_json(REF_FILE, {})

    group = user.get("group", "")
    team_num = user.get("team_number", 1)
    team_num_str = str(team_num)
    team_num_int = int(team_num)

    # Lookup team info safely (check both str and int keys)
    group_data = referrals.get(group, {})
    team_info = group_data.get(team_num_str) or group_data.get(team_num_int) or {
        "team_label": f"TEAM{team_num}",
        "referrals": 0
    }

    # Build group's teams for mini leaderboard
    group_teams = dict(
        sorted(
            group_data.items(),
            key=lambda kv: int(kv[1].get("referrals", 0)),
            reverse=True
        )
    )

    # Referral goal for progress bar
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
    result = fetch_contacts_and_update()
    if request.args.get("format") == "json" or request.is_json:
        return jsonify(result)
    # load saved referrals (group -> { team_num: {team_label, referrals} })
    referrals = load_json(REF_FILE, {})

    # Pre-sort each group's teams by referrals descending
    sorted_refs = {}
    for group, teams in referrals.items():
        try:
            # teams is dict: team_num -> info
            # convert to list of tuples sorted by info['referrals'] desc
            sorted_list = sorted(teams.items(), key=lambda kv: int(kv[1].get("referrals", 0)), reverse=True)
            # convert back to dict preserving this order
            sorted_refs[group] = {k: v for k, v in sorted_list}
        except Exception:
            # fallback — keep original if something unexpected
            sorted_refs[group] = teams

    

    return render_template("leaderboard.html", all_refs=sorted_refs, TEAM_LINKS=TEAM_LINKS)


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
        save_json(DATA_FILE, users)
    return jsonify({"status": "ok", "updated": changed})

# ---------------------- Start ----------------------
if __name__ == "__main__":
    threading.Thread(target=background_updater, daemon=True).start()
    print("✅ Flask app running with automatic Google Contacts sync and team link assignment.")
    app.run(debug=True)
