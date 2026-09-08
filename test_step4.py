from database import init_db
from leads import receive_lead, get_lead
from qualify import qualify_lead
from scoring import score_lead

init_db()

lead = receive_lead(name="Test Lead 4", email="testlead4@example.com", company="Example Co", source="manual")
print("Lead created:", lead["id"])

qualify_lead(lead["id"], "We need a scheduling tool for our 8-person clinic, budget is about $200/month, I'm the office manager, need it within a month.")

result = score_lead(lead["id"])
print("Score:", result["score"])
for r in result["reasons"]:
    print(" -", r)

updated = get_lead(lead["id"])
print("Final lead status:", updated["status"])