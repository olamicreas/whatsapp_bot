from flask import Flask, request, jsonify, render_template
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import urllib.parse
import re
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
import json
import pickle
from google_auth_oauthlib.flow import InstalledAppFlow
from datetime import datetime

# Load environment variables
load_dotenv()

# ✅ Meta WhatsApp API Setup
META_WHATSAPP_ACCESS_TOKEN = os.getenv("META_WHATSAPP_ACCESS_TOKEN")
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID")
WHATSAPP_API_URL = f"https://graph.facebook.com/v21.0/{META_PHONE_NUMBER_ID}/messages"

# ✅ Load Google Credentials from Environment Variable
creds_json = os.getenv("GOOGLE_CREDENTIALS")
if creds_json is None:
    raise ValueError("❌ Missing GOOGLE_CREDENTIALS environment variable")

creds_dict = json.loads(creds_json)

# ✅ Fix OAuth Scopes for Google Sheets & Drive
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)

# ✅ Authenticate with Google Sheets
client_gs = gspread.authorize(creds)
sheet = client_gs.open("WhatsApp Referral Bot").sheet1

# ✅ Fix Google People API (for Google Contacts)
PEOPLE_API_SCOPES = ["https://www.googleapis.com/auth/contacts"]
REDIRECT_URI = "https://referral-contest.onrender.com/"

# ✅ Mr. Heep's phone number
MR_HEEP_PHONE = "2347010528330"
VERIFY_TOKEN = "my_verify_token"
BOT_NUMBER = "+2348066850927"

app = Flask(__name__)

def authenticate():
    creds = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as token:
            creds = pickle.load(token)
    
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            "client_secret_258863544208-vu84m7tuf9j99s10his372sobabqebjs.apps.googleusercontent.com.json", PEOPLE_API_SCOPES, redirect_uri=REDIRECT_URI
        )
        #creds = flow.run_local_server(port=8080)  # Ensure the port matches the redirect URI

        with open("token.pickle", "wb") as token:
            pickle.dump(creds, token)

    return creds


# Function to send WhatsApp message via Meta API
def send_whatsapp_message(to, message):
    headers = {
        "Authorization": f"Bearer {META_WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": message}
    }
    response = requests.post(WHATSAPP_API_URL, json=payload, headers=headers)
    print("📩 Sending Message:", payload)
    print("📩 WhatsApp API Response:", response.json())
    return response.json()

# Function to extract name from message
def extract_name(message):
    match = re.search(r"My name is ([A-Za-z\s]+)", message)
    return match.group(1).strip() if match else "Unknown"

def extract_referral_code(message):
    match = re.search(r"REF\d+", message)
    return match.group(0) if match else None  # Return None if no match



# Function to generate a unique referral code
def generate_referral_code():
    users = sheet.get_all_records()
    last_referral = max((int(user["Referral code"][3:]) for user in users if user["Referral code"].startswith("REF")), default=99)
    return f"REF{last_referral + 1}"

# Function to generate a WhatsApp referral link
def generate_whatsapp_link(referral_code, name):
    base_url = "https://api.whatsapp.com/send"
    message = f"Hello, Mr. Heep. I’m from {referral_code}. My name is _____."
    encoded_message = urllib.parse.quote_plus(message)
    return f"{base_url}?phone={MR_HEEP_PHONE}&text={encoded_message}"


def save_to_google_sheets(phone, name, referral_code=None, referrer_phone=None):
    users = sheet.get_all_records()
    today_date = datetime.today().strftime("%Y-%m-%d")  # Get today's date

    # If sheet is empty, create headers in the correct order
    if not users:
        headers = ["Phone", "Name", "Referral Code", "Referrals", "Heep Saved?", "User Saved?", "Date Joined", "Referred By"]
        sheet.clear()
        sheet.append_row(headers)

    # Check if the user already exists
    for user in users:
        if str(user.get("Phone", "")).strip() == phone:
            return user["Referral code"]  # Return existing referral code (Don't overwrite)

    # If referral_code is None, the user wasn't referred, so generate a new referral code
    if not referral_code:
        referral_code = generate_referral_code()

    # Append new user row with the referrer’s phone number (correct column order)
    sheet.append_row([phone, name, referral_code, 0, "Pending", "Pending", today_date, referrer_phone or ""])

    return referral_code  # Return the correct referral code





def save_to_google_contacts(name, phone, referral_code=None):
    try:
        creds = authenticate()
        service = build("people", "v1", credentials=creds)

        # Modify the name to include the referral code and "HIPTV"
        if referral_code:
            full_name = f"{name} {referral_code} HIPTV"
        else:
            full_name = f"{name} HIPTV"

        contact_data = {
            "names": [{"givenName": full_name}],
            "phoneNumbers": [{"value": phone}],
        }

        contact = service.people().createContact(body=contact_data).execute()
        print("✅ Contact created successfully:", contact)

        # Return the contact's resource name (ID) instead of None
        return contact.get("resourceName", None)
    
    except Exception as e:
        print(f"⚠️ Error saving contact: {e}")
        return None



def update_heep_saved_status(phone):
    users = sheet.get_all_records()

    # Find the user by phone number
    for user in users:
        if str(user["Phone"]).strip() == phone:
            user_row = users.index(user) + 2  # The row number in the sheet
            # Update the "Heep saved?" status to "Saved"
            sheet.update_cell(user_row, 5, "Saved")  # Column 5 is "Heep saved?"


# Function to handle referral usage
def handle_referral_usage_by_referred(referred_phone):
    # Normalize the referred phone before comparing
    referred_phone_norm = normalize_phone_number(referred_phone)
    users = sheet.get_all_records()
    
    # Find the referred user's record using normalized phone numbers
    referred_user = next((user for user in users if normalize_phone_number(user["Phone"]) == referred_phone_norm), None)
    if not referred_user:
        print("⚠️ Referred user not found.")
        return False

    # Retrieve the referrer's phone number and normalize it
    referrer_phone = normalize_phone_number(referred_user.get("Referred By", "").strip())
    if not referrer_phone:
        print("⚠️ No referrer found for this user.")
        return False

    # Find the referrer's record using the normalized referrer's phone number
    referrer = next((user for user in users if normalize_phone_number(user["Phone"]) == referrer_phone), None)
    if not referrer:
        print("⚠️ Referrer record not found.")
        return False

    referred_row = users.index(referred_user) + 2
    referrer_row = users.index(referrer) + 2

    print("DEBUG: Referred row:", referred_row, "| Referrer row:", referrer_row)
    current_referral_value = sheet.cell(referrer_row, 4).value
    print("DEBUG: Current referral count (raw):", current_referral_value)

    try:
        current_count = int(current_referral_value)
    except Exception as e:
        print("⚠️ Error converting current referral count to int:", e)
        return False

    heep_saved_status = sheet.cell(referred_row, 5).value
    user_saved_status = sheet.cell(referred_row, 6).value

    print("DEBUG: Heep Saved status:", heep_saved_status)
    print("DEBUG: User Saved status:", user_saved_status)

    if heep_saved_status == "Verified" and user_saved_status == "Verified":
        new_referral_count = current_count + 1
        sheet.update_cell(referrer_row, 4, new_referral_count)
        print(f"✅ Referral counted for referrer {referrer['Name']} ({referrer_phone}). New count: {new_referral_count}")
        return True
    else:
        print("⚠️ Referral not counted because the referred user's verifications are not complete.")
        return False




def get_referral_code_by_phone(phone):
    """Retrieve the referral code for a user from the sheet."""
    users = sheet.get_all_records()
    for user in users:
        if str(user["Phone"]).strip() == phone:
            return user.get("Referral code")
    return None

    
def normalize_phone_number(phone):
    if not phone:
        return ""
    return re.sub(r"\D", "", str(phone))


def verify_heep_contact(vcard_contact):
    heep_official_phone = normalize_phone_number(MR_HEEP_PHONE)

    contact_numbers = vcard_contact.get("phones", [])
    for phone in contact_numbers:
        if normalize_phone_number(phone["phone"]) == heep_official_phone:
            return True
    return False



def update_heep_saved_status(phone, verified=False):
    users = sheet.get_all_records()
    for i, user in enumerate(users, start=2):  # Start from row 2 (excluding headers)
        if str(user["Phone"]).strip() == phone:
            status = "Verified" if verified else "Pending"
            sheet.update_cell(i, 5, status)  # Column 5 is "Heep saved?"
            return True
    return False

def update_user_saved_status(phone, verified=False):
    users = sheet.get_all_records()
    for i, user in enumerate(users, start=2):  # Start from row 2
        if str(user["Phone"]).strip() == phone:
            status = "Verified" if verified else "Pending"
            sheet.update_cell(i, 6, status)  # Column 6 is "User saved?"
            return True
    return False


@app.route("/webhook", methods=["POST", "GET"])
def whatsapp_webhook():
    if request.method == 'GET':
        # WhatsApp Meta sends a GET request for verification
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if token and challenge:
            if token == VERIFY_TOKEN:
                return challenge, 200
            else:
                return "Invalid verification token", 403
        else:
            return "Missing query parameters", 400
            
    data = request.get_json()
    print("📩 Incoming Webhook Data:", json.dumps(data, indent=2))  # Debugging

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            message_data = value.get("messages", [])
            contacts = value.get("contacts", [])

            if message_data:
                message = message_data[0]
                sender_phone = message["from"]
                message_type = message["type"]  # Ensure we check the message type
                
                # Extract sender name from contacts
                sender_name = contacts[0]["profile"]["name"] if contacts else "Unknown"
                print(f"📞 Sender Phone: {sender_phone}, 👤 Sender Name: {sender_name}, 📝 Message Type: {message_type}")

                if message_type == "text":
                    message_text = message["text"]["body"].strip().lower()

                    if message_text == "start":
                        referral_code = save_to_google_sheets(sender_phone, sender_name)
                        send_whatsapp_message(sender_phone, f"✅ Your referral code is: {referral_code}")
                        send_whatsapp_message(sender_phone, f"🔗 Share this link: {generate_whatsapp_link(referral_code, sender_name)}")

                    elif message_text == "verify":
                        send_whatsapp_message(sender_phone, "📩 Please send Mr. Heep’s contact as a vCard to verify.\n\nFollow these steps to send a contact card:\n1️⃣ Tap the + (iPhone) or 📎 (Android) icon.\n2️⃣ Select 'Contact'.\n3️⃣ Choose 'Mr. Heep' and send.\n\n✅ Done! We will verify it shortly.")

                elif message_type == "contacts":
                   
                    vcard_contact = message["contacts"][0]  # Extract vCard contact
                    heep_verified = verify_heep_contact(vcard_contact)
                
                    if heep_verified:
                        # Update verification statuses for the sender (the referred user)
                        update_heep_saved_status(sender_phone, verified=True)
                        update_user_saved_status(sender_phone, verified=True)
                        send_whatsapp_message(sender_phone, "✅ Verification successful! Mr. Heep’s contact has been saved.")
                
                        # Now update the referral count for the referrer using the "Referred By" information
                        normalized_sender = normalize_phone_number(sender_phone)
                        if handle_referral_usage_by_referred(normalized_sender):
                            print("Referral count updated successfully.")
                        else:
                            print("Referral count not updated.")
                        
                    else:
                        send_whatsapp_message(sender_phone, "❌ Verification failed. Please make sure you’ve saved Mr. Heep’s contact correctly.")


    return jsonify({"status": "success"}), 200


@app.route("/autoresponder", methods=["POST", "GET"])
def autoresponder():
    try:
        data = request.get_json()
        print("📩 Incoming Autoresponder Data:", json.dumps(data, indent=2))  # Debugging

        if not data or "query" not in data:
            return jsonify({
                "status": "error",
                "message": "Invalid data received",
                "replies": [{"message": "⚠️ Invalid data received. Please try again."}]
            }), 400

        sender_phone = str(data["query"].get("sender", "")).strip().replace(" ", "")
        message_content = str(data["query"].get("message", "")).strip()
        vcard_contact = data["query"].get("vcard", {})

        if not sender_phone or not message_content:
            return jsonify({
                "status": "error",
                "message": "Missing sender phone or message content",
                "replies": [{"message": "⚠️ Missing required information. Please try again."}]
            }), 400

        sender_name = extract_name(message_content)
        referral_code_from_msg = extract_referral_code(message_content)

        # 🛠 **Find the referrer using the extracted referral code**
        users = sheet.get_all_records()
        referrer = next((u for u in users if str(u.get("Referral code", "")).strip() == referral_code_from_msg), None)
        referrer_phone = str(referrer.get("Phone", "")) if referrer else None

        print(f"🔎 Extracted referrer phone: {referrer_phone}")  # Debugging

        # 🚀 **Generate a new unique referral code for this user**
        referral_code = generate_referral_code()

        # ✅ Save referred contact to Google Sheets
        contact_saved = save_to_google_contacts(sender_name, sender_phone, referral_code)
        if contact_saved:
            save_to_google_sheets(sender_phone, sender_name, referral_code, referrer_phone)

            # ✅ Mark referred user as "Verified"
            update_user_saved_status(sender_phone, verified=True)

            # ✅ Mark referrer as "Verified" if they exist
            if referrer_phone:
                print(f"✅ Updating referrer {referrer_phone} to Verified")  # Debugging
                updated = update_user_saved_status(referrer_phone, verified=True)
                if updated:
                    print(f"✅ Successfully verified referrer {referrer_phone}")
                else:
                    print(f"⚠️ Failed to verify referrer {referrer_phone}")

            update_heep_saved_status(sender_phone)

            # ✅ Check if Mr. Heep is saved by the referred user
            heep_saved_by_user = verify_heep_contact(vcard_contact)

            if heep_saved_by_user:
                if handle_referral_usage(referral_code_from_msg, sender_phone, sender_name):
                    response_message = "Welcome😊!\n\nThank you for joining our community.\n\n• Your contact has been successfully saved.\n\n• To stay updated with our daily news and engaging content, please save our contact as “MR HEEP”.\n\n• Click the link below to verify that you have saved our contact:\n\n👉 [Click here to verify](https://wa.me/2348066850927?text=verify)"



                else:
                    response_message = "Welcome😊!\n\nThank you for joining our community.\n\n• Your contact has been successfully saved.\n\n• To stay updated with our daily news and engaging content, please save our contact as “MR HEEP”.\n\n• Click the link below to verify that you have saved our contact:\n\n👉 [Click here to verify](https://wa.me/2348066850927?text=verify)"

                    

            else:
                response_message = "Welcome😊!\n\nThank you for joining our community.\n\n• Your contact has been successfully saved.\n\n• To stay updated with our daily news and engaging content, please save our contact as “MR HEEP”.\n\n• Click the link below to verify that you have saved our contact:\n\n👉 [Click here to verify](https://wa.me/2348066850927?text=verify)"

                

        else:
            response_message = "❌ Contact could not be saved. Please try again."

        return jsonify({
            "status": "success",
            "message": f"Processed contact {sender_name} ({sender_phone})",
            "replies": [{"message": response_message}]
        }), 200

    except Exception as e:
        print(f"⚠️ Autoresponder Error: {e}")
        return jsonify({
            "status": "error",
            "message": str(e),
            "replies": [{"message": "⚠️ An error occurred while processing your request."}]
        }), 500





@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")  # Ensure dashboard.html exists in templates folder

@app.route("/get_users")
def get_users():
    try:
        users = sheet.get_all_records()
        processed_users = []

        print("🔍 Raw Users Data:", users)  # Debugging line to print all users

        for user in users:
            user_phone = str(user.get("Phone", "")).strip()

            # Convert referrals safely
            num_referrals = user.get("Referrals", 0)
            total_referrals = int(num_referrals) if str(num_referrals).isdigit() else 0

            print(f"👤 Checking {user.get('Name', 'Unknown')} (Phone: {user_phone})")  # Debugging

            # Count pending referrals
            pending_referrals = 0
            for u in users:
                referred_by = str(u.get("Referred By", "")).strip()

                if referred_by == user_phone:
                    print(f"🔗 Found Referral: {u.get('Name', 'Unknown')} - Heep Saved: {u.get('Heep saved?')}, User Saved: {u.get('User saved?')}")  # Debugging

                    heep_saved = str(u.get("Heep saved?", "")).strip().lower()
                    user_saved = str(u.get("User saved?", "")).strip().lower()

                    if heep_saved != "verified" or user_saved != "verified":
                        pending_referrals += 1

            print(f"📌 {user.get('Name', 'Unknown')} - Pending Referrals: {pending_referrals}")  # Debugging

            processed_users.append({
                "phone": user_phone,
                "name": str(user.get("Name", "Unknown")),
                "referral_code": str(user.get("Referral code", "")).strip(),
                "referrals": total_referrals,
                "pending_referrals": pending_referrals
            })

        return jsonify({"data": processed_users})

    except Exception as e:
        print(f"❌ Error in /get_users: {str(e)}")  # Log the error
        return jsonify({"error": str(e)}), 500




@app.route("/get_analytics")
def get_analytics():
    try:
        users = sheet.get_all_records()
        referral_counts = {
            "0 Referrals": 0,
            "1-2 Referrals": 0,
            "3-5 Referrals": 0,
            "6+ Referrals": 0
        }
        total_users = len(users)
        total_pending_referrals = 0

        for user in users:
            num_referrals = int(user.get("Referrals", 0))
            user_phone = str(user.get("Phone", "")).strip()  # Ensure it's a string

            # Categorize referral counts
            if num_referrals == 0:
                referral_counts["0 Referrals"] += 1
            elif num_referrals <= 2:
                referral_counts["1-2 Referrals"] += 1
            elif num_referrals <= 5:
                referral_counts["3-5 Referrals"] += 1
            else:
                referral_counts["6+ Referrals"] += 1

            # Count pending referrals
            for ref_user in users:
                referred_by = str(ref_user.get("Referred By", "")).strip()  # Convert to string safely
                
                if referred_by == user_phone:
                    heep_saved = str(ref_user.get("Heep saved?", "")).strip().lower()
                    user_saved = str(ref_user.get("User saved?", "")).strip().lower()

                    if heep_saved != "verified" or user_saved != "verified":
                        total_pending_referrals += 1

        return jsonify({
            "labels": list(referral_counts.keys()),
            "values": list(referral_counts.values()),
            "total_users": total_users,
            "total_pending_referrals": total_pending_referrals
        })
    except Exception as e:
        print(f"❌ Error in /get_analytics: {str(e)}")  # Debugging
        return jsonify({"error": str(e)}), 500



@app.route("/get_new_users")
def get_new_users():
    try:
        users = sheet.get_all_records()
        monthly_counts = {}

        print("📊 Raw Users Data:", users)  # Debugging line

        for user in users:
            phone = user.get("Phone", "")
            name = user.get("Name", "")
            referral_code = user.get("Referral code", "")
            date_str = user.get("Date Joined", "").strip()  # Get and clean the date

            print(f"📅 Processing User: {name}, Date Joined: {date_str}")  # Debugging

            if not date_str:
                print(f"⚠️ Skipping {name}: No Date Joined")
                continue  # Skip users with no date

            try:
                # Convert date to YYYY-MM format
                date_obj = datetime.strptime(date_str, "%Y-%m-%d")
                month_year = date_obj.strftime("%Y-%m")

                # Count users per month
                monthly_counts[month_year] = monthly_counts.get(month_year, 0) + 1

            except ValueError:
                print(f"❌ Invalid date format for {name}: {date_str}")
                continue  # Skip invalid dates

        labels = list(monthly_counts.keys())
        values = list(monthly_counts.values())

        print(f"📈 Final Labels: {labels}")
        print(f"📊 Final Values: {values}")

        return jsonify({"labels": labels, "values": values})
    except Exception as e:
        print(f"❌ Error fetching new users: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/get_user_referral")
def get_user_referral():
    phone = request.args.get("phone")
    if not phone:
        return jsonify({"error": "Phone number is required"}), 400
    try:
        users = sheet.get_all_records()
        user_referrals = None
        total_referrals = 0
        pending_referrals = 0

        # Find user and their referral count
        for user in users:
            if str(user.get("Phone", "")).strip() == phone:
                total_referrals = int(user.get("Referrals", 0))
                referral_code = user.get("Referral code", "")

                # Count pending referrals
                for ref_user in users:
                    if ref_user.get("Referral code", "") == referral_code:
                        heep_saved = ref_user.get("Heep saved?", "").strip().lower()
                        user_saved = ref_user.get("User saved?", "").strip().lower()
                        
                        if heep_saved != "verified" or user_saved != "verified":
                            pending_referrals += 1

                user_referrals = {
                    "name": user.get("Name", "Unknown"),
                    "phone": user.get("Phone", ""),
                    "referral_code": referral_code,
                    "referrals": total_referrals,
                    "pending_referrals": pending_referrals
                }
                break

        if not user_referrals:
            return jsonify({"error": "User not found"}), 404

        return jsonify(user_referrals)
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/leaderboard")
def leaderboard():
    try:
        users = sheet.get_all_records()
        if not users:
            return render_template("leaderboard.html", users=[])

        # Sort users by referral count in descending order
        sorted_users = sorted(users, key=lambda x: int(x.get("Referrals", 0)), reverse=True)

        # Assign rank to each user
        for index, user in enumerate(sorted_users, start=1):
            user["rank"] = index

        return render_template("leaderboard.html", users=sorted_users)
    except Exception as e:
        return render_template("leaderboard.html", users=[], error=str(e))

if __name__ == "__main__":
    app.run(debug=True)

