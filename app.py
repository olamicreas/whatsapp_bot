from flask import Flask, request, jsonify
import requests
import gspread
import os
import urllib.parse
import re
import json
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from dotenv import load_dotenv

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
people_creds = Credentials.from_service_account_info(creds_dict, scopes=PEOPLE_API_SCOPES)
people_service = build("people", "v1", credentials=people_creds)

# ✅ Mr. Heep's phone number
MR_HEEP_PHONE = "2347010528330"
VERIFY_TOKEN = "my_verify_token"

app = Flask(__name__)

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

# Function to generate a unique referral code
def generate_referral_code():
    users = sheet.get_all_records()
    last_referral = max((int(user["Referral code"][3:]) for user in users if user["Referral code"].startswith("REF")), default=99)
    return f"REF{last_referral + 1}"

# Function to generate a WhatsApp referral link
def generate_whatsapp_link(referral_code, name):
    base_url = "https://api.whatsapp.com/send"
    message = f"Hello, Mr Heep. I’m from {referral_code}. My name is {name}."
    encoded_message = urllib.parse.quote_plus(message)
    return f"{base_url}?phone={MR_HEEP_PHONE}&text={encoded_message}"

# Function to save user to Google Sheets
def save_to_google_sheets(phone, name, referral_code=None):
    try:
        users = sheet.get_all_records(expected_headers=["Phone", "Name", "Referral code", "Referrals", "Heep saved?", "User saved?"])
    except gspread.exceptions.GSpreadException:
        users = []  # If no valid headers exist, treat as empty

    # If sheet is empty, create headers
    if not users:
        headers = ["Phone", "Name", "Referral code", "Referrals", "Heep saved?", "User saved?"]
        sheet.clear()  # Clear sheet to remove any hidden characters
        sheet.append_row(headers)

    # Check if the user already exists
    for user in users:
        if str(user["Phone"]).strip() == phone:
            return user["Referral code"]

    # Generate a referral code if not provided
    if not referral_code:
        referral_code = generate_referral_code()

    # Append new user row
    sheet.append_row([phone, name, referral_code, 0, "Pending", "Pending"])
    
    return referral_code


# Function to save contact in Google Contacts
def save_to_google_contacts(name, phone):
    try:
        existing_contacts = people_service.people().connections().list(
            resourceName="people/me", personFields="phoneNumbers"
        ).execute().get("connections", [])
        if any(phone == num["value"] for contact in existing_contacts for num in contact.get("phoneNumbers", [])):
            return False  # Contact already exists
        people_service.people().createContact(body={"names": [{"givenName": name}], "phoneNumbers": [{"value": phone}]}).execute()
        return True
    except Exception as e:
        print(f"⚠️ Google Contacts Error: {e}")
        return False

# Function to handle referral usage
def handle_referral_usage(referral_code, referred_phone):
    users = sheet.get_all_records()
    if any(user["Phone"] == referred_phone and user["User saved?"] == "Yes" for user in users):
        return False  # User already used a referral
    referrer = next((user for user in users if user["Referral code"] == referral_code), None)
    if referrer:
        referrer_row = users.index(referrer) + 2
        sheet.update_cell(referrer_row, 4, int(sheet.cell(referrer_row, 4).value) + 1)
        sheet.append_row([referred_phone, "Unknown", referral_code, 0, "Pending", "Yes"])
        return True
    return False

@app.route("/webhook", methods=["GET", "POST"])
def whatsapp_webhook():
    if request.method == "GET":
        # WhatsApp webhook verification
        verify_token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if verify_token == VERIFY_TOKEN:
            return challenge  # Return challenge to verify webhook
        else:
            return "Verification failed", 403

    elif request.method == "POST":
        # Handle incoming messages
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
                    message_text = message["text"]["body"].strip().lower()

                    sender_name = contacts[0]["profile"]["name"] if contacts else "Unknown"
                    print(f"📞 Sender Phone: {sender_phone}, 👤 Sender Name: {sender_name}")

                    if message_text == "start":
                        referral_code = save_to_google_sheets(sender_phone, sender_name)
                        send_whatsapp_message(sender_phone, f"✅ Your referral code is: {referral_code}")
                        send_whatsapp_message(sender_phone, f"🔗 Share this link: {generate_whatsapp_link(referral_code, sender_name)}")

        return jsonify({"status": "success"}), 200


@app.route("/autoresponder", methods=["POST", "GET"])
def autoresponder():
    try:
        data = request.get_json()
        print("📩 Incoming Autoresponder Data:", json.dumps(data, indent=2))  # Debugging

        if not data or "query" not in data:
            print("⚠️ Invalid data received in autoresponder.")
            return jsonify({"status": "error", "message": "Invalid data received"}), 400

        sender_phone = data["query"].get("sender", "").strip().replace(" ", "")
        message_content = data["query"].get("message", "").strip()

        print(f"📞 Extracted Sender Phone: {sender_phone}")
        print(f"📝 Extracted Message Content: {message_content}")

        if not sender_phone or not message_content:
            print("⚠️ Missing sender phone or message content.")
            return jsonify({"status": "error", "message": "Missing sender phone or message content"}), 400

        sender_name = extract_name(message_content)
        print(f"👤 Extracted Name: {sender_name}")

        # Try saving contact first
        contact_saved = save_to_google_contacts(sender_name, sender_phone)
        print(f"📇 Contact Saved to Google: {contact_saved}")

        if contact_saved:
            referral_code = save_to_google_sheets(sender_phone, sender_name)
            print(f"📊 Referral Code Assigned: {referral_code}")

            # ✅ Increment referral count for the referrer
            if handle_referral_usage(referral_code, sender_phone):
                send_whatsapp_message(sender_phone, "✅ Your contact has been saved by Mr. Heep. Your referrer has been rewarded!")
                print(f"📊 Referral count updated for {referral_code}")
            else:
                send_whatsapp_message(sender_phone, "✅ Your contact has been saved by Mr. Heep, but no referral was counted.")
                print(f"⚠️ No referral update needed for {sender_phone}")

        else:
            send_whatsapp_message(sender_phone, "From Mr Heep! 📩 Contact was already saved. No referral counted.")
            print(f"📩 Contact was already saved. No referral counted.")

        return jsonify({"status": "success", "message": f"Processed contact {sender_name} ({sender_phone})"}), 200

    except Exception as e:
        print(f"⚠️ Autoresponder Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
if __name__ == "__main__":
    app.run(debug=True)
