from database import init_db
from leads import receive_lead, list_leads

init_db()
lead = receive_lead(name="Test User", email="test@example.com", company="Test Co", source="manual")
print(lead)
print(list_leads())