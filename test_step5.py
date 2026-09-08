from database import init_db
from leads import receive_lead, get_lead
from qualify import qualify_lead
from scoring import score_lead
from draft import generate_draft, get_pending_messages_for_lead

init_db()

lead = receive_lead(name="Test Lead 5", email="testlead5@example.com", company="Example Clinic", source="manual")
qualify_lead(lead["id"], "We need patient scheduling software for our 6-person clinic, budget $250/month, I'm the practice manager, need it within 3 weeks.")
score_lead(lead["id"])

result = generate_draft(lead["id"])
print("Subject:", result["subject"])
print("Body:", result["body"])

updated = get_lead(lead["id"])
print("Lead status:", updated["status"])