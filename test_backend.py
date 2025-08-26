from postnord_backend_client import *

tracking_number = "PN123456"

print("🔍 Tracking package:")
print(track_package(tracking_number))

print("\n📩 Rechecking SMS:")
print(recheck_sms(tracking_number))

print("\n📑 Verifying customs docs:")
print(verify_customs_docs_needed(tracking_number))

print("\n🔁 Resending notification:")
print(resend_notification(tracking_number))

print("\n🕒 Getting delivery window:")
print(provide_est_delivery_window(tracking_number))