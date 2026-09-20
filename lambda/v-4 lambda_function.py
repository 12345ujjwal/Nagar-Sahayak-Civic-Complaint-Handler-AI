import json
import boto3
import re
import uuid
from decimal import Decimal
from datetime import datetime, timedelta, timezone
import urllib.request
import urllib.parse

bedrock = boto3.client("bedrock-runtime", region_name="ap-south-1")
dynamodb = boto3.resource("dynamodb", region_name="ap-south-1")
s3 = boto3.client("s3", region_name="ap-south-1")

complaints_table = dynamodb.Table("complaints")
sessions_table = dynamodb.Table("sessions")

S3_BUCKET_NAME = "civic-complaint-photos-CHANGE-ME"  # <-- set your actual bucket name

DEPARTMENTS = {
    "streetlight": "BESCOM (Electricity)",
    "garbage": "BBMP (Municipal Corporation)",
    "pothole": "BBMP - Roads",
    "water": "BWSSB (Water Supply)"
}

QUESTION_FOR_FIELD = {
    "issue_type": "What type of issue is this — a streetlight, garbage, pothole, or water problem?",
    "location": "Could you tell me the name of the village, town, street, or a nearby landmark? A general reference like \"my area\" isn't specific enough — or you can tap 📍 to share your exact location instead.",
    "condition": "Could you briefly describe how serious this is or how it's affecting people nearby?"
}

SLACK_WEBHOOK_URL = "https://hook............"

SENDER_EMAIL = "12345ujjwalpratap@gmail.com"
DEPARTMENT_EMAIL = "support@nexaskilllab.com"

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*"
}


IST = timezone(timedelta(hours=5, minutes=30))

VAGUE_LOCATION_PHRASES = [
    "my village", "my town", "my area", "my locality", "my street", "my neighbourhood", "my neighborhood",
    "near my house", "near my home", "this area", "this village", "this place", "here", "nearby",
    "yahan", "yaha", "mera gaon", "mera gaanv", "mere gaon", "mere ilake", "is jagah", "yahan par"
]


def is_vague_location(text):
    if not text:
        return True
    normalized = text.strip().lower()
    if len(normalized) < 3:
        return True
    return any(phrase in normalized for phrase in VAGUE_LOCATION_PHRASES)


def today_ist():
    return datetime.now(IST)


def validate_incident_date(date_str, ref_datetime):
    """date_str expected as ISO YYYY-MM-DD from the extraction model, or None."""
    if not date_str:
        return None, None  # nothing provided, no error — caller decides if that's OK
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None, "That doesn't look like a valid date. Could you provide the date the incident happened (e.g. '15 September 2026')?"

    today = ref_datetime.date()
    if parsed > today:
        return None, "The incident date can't be in the future. Please provide a date up to today."
    if (today - parsed).days > 30:
        return None, "I can only accept complaints for incidents within the last 30 days. Please provide a more recent date."
    return parsed.strftime("%d %b %Y"), None


# ---------- Bedrock: extraction only (per turn) ----------

def extract_fields(latest_message, known_fields, ref_datetime):
    today_iso = ref_datetime.strftime("%Y-%m-%d")
    prompt = f"""Extract structured fields from ONE message sent by a citizen. It may be a full complaint, or just a short follow-up answer (e.g. only a location name, or only a severity description) in an ongoing conversation.

Today's date is {today_iso}.

Message: "{latest_message}"

Fields already known (for context only — extract only what THIS message adds or confirms): {json.dumps(known_fields)}

Extract:
- issue_type: one of {list(DEPARTMENTS.keys())} if mentioned or clearly implied in this message, else null
- location: a SPECIFIC, identifiable place — a named village/town/city, a street name, a neighborhood, or a landmark. This must be null if the message only contains a vague, non-specific reference to location such as "my village", "here", "near my house", "this area", "yahan", "mera gaon" — those do NOT count as a location even though they mention one exists. Only extract this field if an actual name is given.
- incident_date: if a date or relative time reference is mentioned (e.g. "yesterday", "3 days ago", "12 Sept"), convert it to an absolute ISO date (YYYY-MM-DD) using today's date above as the reference point. If no date is mentioned, use null.
- condition: a short severity/impact description if mentioned in this message, else null
- is_greeting_or_irrelevant: true only if this message contains NONE of the above and is just a greeting, small talk, or unrelated chat

Respond with ONLY valid JSON, no other text:
{{"issue_type": "... or null", "location": "... or null", "incident_date": "YYYY-MM-DD or null", "condition": "... or null", "is_greeting_or_irrelevant": true|false}}"""

    try:
        response = bedrock.invoke_model(
            modelId="global.anthropic.claude-haiku-4-5-20251001-v1:0",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}]
            })
        )
        body = json.loads(response["body"].read())
        text = body["content"][0]["text"].replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"extract_fields failed: {e}")
        return {"issue_type": None, "location": None, "incident_date": None, "condition": None, "is_greeting_or_irrelevant": False}


# ---------- Bedrock: final drafting (once, when complete) ----------

def draft_complaint(fields):
    prompt = f"""Write a formal English civic complaint using these details:

Issue type: {fields['issue_type']}
Location: {fields['location']}
Incident date: {fields['incident_date']}
Condition/severity: {fields['condition']}

Write ONE formal paragraph suitable for submission to a municipal department. Respond with ONLY the paragraph text, no JSON, no preamble."""

    response = bedrock.invoke_model(
        modelId="global.anthropic.claude-haiku-4-5-20251001-v1:0",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 400,
            "messages": [{"role": "user", "content": prompt}]
        })
    )
    body = json.loads(response["body"].read())
    return body["content"][0]["text"].strip()


# ---------- Session storage (in-progress complaints) ----------

def get_session(session_id):
    resp = sessions_table.get_item(Key={"session_id": session_id})
    return resp.get("Item")


def save_session(session_id, history, known_fields, image_keys, geo):
    item = {
        "session_id": session_id,
        "history": history,
        "known_fields": known_fields,
        "image_keys": image_keys,
        "updated_at": datetime.utcnow().isoformat()
    }
    if geo:
        item["geo"] = {"lat": Decimal(str(geo["lat"])), "lng": Decimal(str(geo["lng"]))}
    sessions_table.put_item(Item=item)


def delete_session(session_id):
    sessions_table.delete_item(Key={"session_id": session_id})


# ---------- Final complaint storage ----------

def save_complaint(fields, department, drafted_complaint, image_keys, geo):
    ticket_id = "CIV-" + str(uuid.uuid4())[:8].upper()
    item = {
        "ticket_id": ticket_id,
        "issue_type": fields["issue_type"],
        "department": department,
        "location": fields["location"],
        "incident_date": fields["incident_date"],
        "condition": fields["condition"],
        "drafted_complaint": drafted_complaint,
        "image_keys": image_keys or [],
        "status": "Submitted",
        "created_at": datetime.utcnow().isoformat()
    }
    if geo:
        item["geo"] = {"lat": Decimal(str(geo["lat"])), "lng": Decimal(str(geo["lng"]))}
    complaints_table.put_item(Item=item)
    return ticket_id


def get_status(ticket_id):
    response = complaints_table.get_item(Key={"ticket_id": ticket_id})
    item = response.get("Item")
    if not item:
        return None
    return {
        "ticket_id": item["ticket_id"],
        "status": item["status"],
        "issue_type": item["issue_type"],
        "department": item["department"],
        "created_at": item["created_at"]
    }


def list_all_complaints():
    response = complaints_table.scan()
    items = response.get("Items", [])
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    result = []
    for i in items:
        geo = i.get("geo")
        geo_out = {"lat": float(geo["lat"]), "lng": float(geo["lng"])} if geo else None
        result.append({
            "ticket_id": i["ticket_id"],
            "status": i.get("status", "Submitted"),
            "issue_type": i.get("issue_type"),
            "department": i.get("department"),
            "location": i.get("location"),
            "incident_date": i.get("incident_date"),
            "created_at": i.get("created_at"),
            "drafted_complaint": i.get("drafted_complaint"),
            "image_keys": i.get("image_keys", []),
            "geo": geo_out
        })
    return result


def update_ticket_status(ticket_id, new_status):
    complaints_table.update_item(
        Key={"ticket_id": ticket_id},
        UpdateExpression="SET #s = :val",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":val": new_status}
    )


# ---------- Notifications ----------

def public_image_url(key):
    return f"https://{S3_BUCKET_NAME}.s3.ap-south-1.amazonaws.com/{urllib.parse.quote(key, safe='/')}"


def notify_slack(ticket_id, fields, department, drafted_complaint, image_keys, geo):
    location_line = f"*Location:* {fields['location']}\n"
    if geo:
        location_line += f"*Live GPS:* {geo['lat']}, {geo['lng']}\n"

    summary_text = (
        f"🚨 New civic complaint — *{ticket_id}*\n"
        f"*Department:* {department}\n"
        f"*Issue type:* {fields['issue_type']}\n"
        f"{location_line}"
        f"*Incident date:* {fields['incident_date']}\n"
        f"*Complaint:* {drafted_complaint}"
    )

    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": summary_text}}]
    for key in (image_keys or []):
        blocks.append({
            "type": "image",
            "image_url": public_image_url(key),
            "alt_text": f"Photo for {ticket_id}"
        })

    message = {"text": summary_text, "blocks": blocks}  # "text" is the fallback for notifications
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=json.dumps(message).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"Slack notify failed: {e}")


def notify_email(ticket_id, fields, department, drafted_complaint, image_keys, geo):
    subject = f"New Civic Complaint — {ticket_id} ({department})"

    photo_links = ""
    if image_keys:
        photo_links = "\nPhotos:\n" + "\n".join(public_image_url(k) for k in image_keys) + "\n"

    body = f"""A new civic complaint has been filed and requires attention.

Ticket ID: {ticket_id}
Department: {department}
Issue type: {fields['issue_type']}
Location: {fields['location']}
Incident date: {fields['incident_date']}
Condition: {fields['condition']}
Live GPS: {geo['lat'] if geo else 'N/A'}, {geo['lng'] if geo else ''}
{photo_links}
Complaint:
{drafted_complaint}

— Filed automatically via Nagar Sahayak
"""
    try:
        ses = boto3.client("ses", region_name="ap-south-1")
        ses.send_email(
            Source=SENDER_EMAIL,
            Destination={"ToAddresses": [DEPARTMENT_EMAIL]},
            Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}}
        )
    except Exception as e:
        print(f"Email notify failed: {e}")


# ---------- Main handler ----------

def lambda_handler(event, context):
    try:
        path = event.get("rawPath", "")

        if "body" in event and event["body"]:
            body = json.loads(event["body"])
        else:
            body = event

        # Route: presigned S3 upload URL
        if path == "/upload-url":
            raw_filename = body.get("filename", "photo.jpg")
            safe_filename = re.sub(r"[^A-Za-z0-9._-]", "_", raw_filename)
            key = f"complaints/{uuid.uuid4()}-{safe_filename}"
            upload_url = s3.generate_presigned_url(
                "put_object",
                Params={"Bucket": S3_BUCKET_NAME, "Key": key},
                ExpiresIn=300
            )
            return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps({"upload_url": upload_url, "key": key})}

        # Route: status check
        if path == "/status":
            ticket_id = body.get("ticket_id") or (event.get("queryStringParameters") or {}).get("ticket_id")
            if not ticket_id:
                return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": "ticket_id required"})}
            result = get_status(ticket_id)
            if not result:
                return {"statusCode": 404, "headers": CORS_HEADERS, "body": json.dumps({"error": "Ticket not found"})}
            return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps(result)}

        # Route: admin — list all complaints
        if path == "/admin/complaints":
            complaints = list_all_complaints()
            return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps(complaints)}

        # Route: admin — update a ticket's status
        if path == "/update-status":
            ticket_id = body.get("ticket_id")
            new_status = body.get("status")
            if not ticket_id or new_status not in ("Submitted", "Acknowledged", "Resolved"):
                return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": "ticket_id and a valid status are required"})}
            update_ticket_status(ticket_id, new_status)
            return {"statusCode": 200, "headers": CORS_HEADERS, "body": json.dumps({"ticket_id": ticket_id, "status": new_status})}

        # Route: report (multi-turn, deterministic slot-filling)
        citizen_text = body.get("text", "")
        session_id = body.get("session_id") or str(uuid.uuid4())
        new_image_keys = body.get("image_keys", [])
        geo = body.get("geo")

        if not citizen_text:
            return {"statusCode": 400, "headers": CORS_HEADERS, "body": json.dumps({"error": "No text provided"})}

        session = get_session(session_id)
        if session:
            history = session.get("history", [])
            known_fields = session.get("known_fields", {})
            image_keys = session.get("image_keys", [])
            saved_geo = session.get("geo")
            saved_geo = {"lat": float(saved_geo["lat"]), "lng": float(saved_geo["lng"])} if saved_geo else None
        else:
            history = []
            known_fields = {"issue_type": None, "location": None, "incident_date": None, "condition": None}
            image_keys = []
            saved_geo = None

        history.append(citizen_text)
        image_keys = image_keys + new_image_keys
        if geo:
            saved_geo = geo

        extracted = extract_fields(citizen_text, known_fields, today_ist())

        merged = {
            "issue_type": extracted.get("issue_type") or known_fields.get("issue_type"),
            "location": extracted.get("location") or known_fields.get("location"),
            "incident_date": extracted.get("incident_date") or known_fields.get("incident_date"),
            "condition": extracted.get("condition") or known_fields.get("condition"),
        }

        # If a date was newly mentioned this turn, validate it (real calendar date, not in the future, within 30 days)
        if extracted.get("incident_date"):
            normalized_date, date_error = validate_incident_date(extracted["incident_date"], today_ist())
            if date_error:
                save_session(session_id, history, merged, image_keys, saved_geo)
                return {
                    "statusCode": 200,
                    "headers": CORS_HEADERS,
                    "body": json.dumps({
                        "needs_input": True,
                        "session_id": session_id,
                        "message": date_error
                    })
                }
            merged["incident_date"] = normalized_date

        # Reject vague, non-specific location phrases even if the model let one through
        if merged["location"] and is_vague_location(merged["location"]):
            merged["location"] = None

        # Sharing live location counts as satisfying "location"
        if saved_geo and not merged["location"]:
            merged["location"] = "Shared live location (see GPS coordinates)"

        # First-ever message with nothing extractable and no real content = invalid, don't start a session
        is_first_message = len(history) == 1
        if is_first_message and extracted.get("is_greeting_or_irrelevant") and not any(merged.values()):
            return {
                "statusCode": 200,
                "headers": CORS_HEADERS,
                "body": json.dumps({
                    "needs_input": True,
                    "session_id": None,
                    "message": "Hi! Please describe a civic issue you'd like to report — e.g. a broken streetlight, garbage, a pothole, or a water leak."
                })
            }

        missing = [f for f in ["issue_type", "location", "condition"] if not merged.get(f)]
        collected_status = {
            "issue_type": bool(merged.get("issue_type")),
            "location": bool(merged.get("location")),
            "condition": bool(merged.get("condition"))
        }

        if missing:
            next_field = missing[0]
            save_session(session_id, history, merged, image_keys, saved_geo)
            return {
                "statusCode": 200,
                "headers": CORS_HEADERS,
                "body": json.dumps({
                    "needs_input": True,
                    "session_id": session_id,
                    "message": QUESTION_FOR_FIELD[next_field],
                    "collected": collected_status
                })
            }

        # All required fields present — finalize
        if not merged["incident_date"]:
            merged["incident_date"] = today_ist().strftime("%d %b %Y, %I:%M %p IST")

        department = DEPARTMENTS.get(merged["issue_type"], "General Municipal Department")
        drafted_complaint = draft_complaint(merged)
        ticket_id = save_complaint(merged, department, drafted_complaint, image_keys, saved_geo)
        notify_slack(ticket_id, merged, department, drafted_complaint, image_keys, saved_geo)
        notify_email(ticket_id, merged, department, drafted_complaint, image_keys, saved_geo)
        delete_session(session_id)

        return {
            "statusCode": 200,
            "headers": CORS_HEADERS,
            "body": json.dumps({
                "ticket_id": ticket_id,
                "issue_type": merged["issue_type"],
                "department": department,
                "location": merged["location"],
                "incident_date": merged["incident_date"],
                "drafted_complaint": drafted_complaint
            })
        }

    except Exception as e:
        print(f"Unhandled error: {e}")
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "Something went wrong. Please try again."})
        }
