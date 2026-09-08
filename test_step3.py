from database import init_db
from leads import receive_lead
from qualify import qualify_lead, get_requirements

init_db()

lead = receive_lead(name="Amit Kumar", email="amit@brightsoft.com", company="BrightSoft", source="website_form")
print("Lead created:", lead["id"])

result = qualify_lead(
    lead["id"],
    "We need a CRM for our 15-person sales team, budget around $400/month, want it within 2 months, currently using spreadsheets."
)
print("Extracted:", result)