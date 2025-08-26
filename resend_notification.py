from flask import request, jsonify

def handle_resend_notification():
    data = request.get_json()
    tracking_number = data.get("tracking_number")

    # Mock logic for notification resend
    return jsonify({
        "action": "resend_notification",
        "status": "New notification sent",
        "notification_type": "SMS + Email",
        "tracking_number": tracking_number
    })