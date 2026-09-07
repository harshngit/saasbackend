from app.core.database import engine
from sqlalchemy import text

with engine.begin() as conn:
    conn.execute(text("UPDATE users SET role='DELIVERY_PARTNER' WHERE role IN ('delivery_partner', 'STAFF')"))
print("Cleaned up invalid user roles in DB")
