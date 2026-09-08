from database import init_db
from leads import receive_lead
from qualify import qualify_lead
from scoring import score_lead
from draft import generate_draft
from approval import review_pending_lead

init_db()

lead = receive_lead(name="Test Lead 6", email="testlead6@example.com", company="Example Salon", source="manual")
qualify_lead(lead["id"], "Looking for a booking system for our 4-chair salon, budget $150/month, I'm the owner, need it in 2 weeks.")
score_lead(lead["id"])
generate_draft(lead["id"])

review_pending_lead(lead["id"])