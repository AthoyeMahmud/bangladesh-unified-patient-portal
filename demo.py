from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from werkzeug.security import generate_password_hash


DHAKA = ZoneInfo("Asia/Dhaka")
DEMO_PASSWORD = "demo123"
PATIENTS = [
    (1, "BUP-1001", "Amina Rahman", "1987-04-12", "Female", "Mirpur, Dhaka", "BUP-CARD-X6P2Q9", "DEMO-NID-1001", None),
    (2, "BUP-1002", "Karim Uddin", "1964-09-08", "Male", "Mohammadpur, Dhaka", "BUP-CARD-G4H7T3", "DEMO-NID-1002", None),
    (3, "BUP-1003", "Nusrat Akter", "2017-07-21", "Female", "Uttara, Dhaka", "BUP-CARD-K9M5J8", None, "DEMO-BRN-1003"),
    (4, "BUP-1004", "Rafi Hasan", "1997-01-16", "Male", "Dhanmondi, Dhaka", "BUP-CARD-W8S4L2", "DEMO-NID-1004", None),
]


def seed(db):
    today = datetime.now(DHAKA).date()
    previous = (today - timedelta(days=8)).isoformat()
    earlier = (today - timedelta(days=35)).isoformat()
    if not db.execute("SELECT 1 FROM staff LIMIT 1").fetchone():
        db.executemany("INSERT INTO staff VALUES (?, ?, ?, ?)", [
            ("registrar", "Registration staff", generate_password_hash(DEMO_PASSWORD), "registration"),
            ("pharmacy", "Pharmacy staff", generate_password_hash(DEMO_PASSWORD), "pharmacy"),
        ])
        db.executemany("INSERT INTO patients VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", PATIENTS)
        db.executemany("""
            INSERT INTO records(patient_id, kind, title, on_date, source, details, released)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [
            (1, "Visit", "Medicine outpatient visit", previous, "Demo Hospital · Outpatient",
             "Follow-up consultation recorded. Existing prescription available for collection.", 1),
            (1, "Condition", "Iron deficiency anaemia", previous, "Demo Hospital · Medicine",
             "Recorded at the last outpatient visit.", 1),
            (1, "Allergy", "Penicillin allergy reported", earlier, "Demo Hospital · Registration",
             "Patient-reported at a previous visit.", 1),
            (1, "Report", "Complete blood count (CBC)", previous, "Demo Hospital · Laboratory",
             "Haemoglobin: 10.9 g/dL\nWhite blood cell count: 6.7 × 10⁹/L\nPlatelets: 280 × 10⁹/L", 1),
            (1, "Report", "Serum ferritin", previous, "Demo Hospital · Laboratory", "Ferritin: 11 µg/L", 1),
            (1, "Report", "Draft report", previous, "Demo Hospital · Laboratory", "Unreleased report fixture.", 0),
            (2, "Visit", "Medicine assessment", previous, "Demo Hospital · Medicine",
             "Admission order entered by the treating clinician.", 1),
            (2, "Admission", "Previous medicine admission", earlier, "Demo Hospital · Inpatient",
             "Discharge recorded. See prior visit information if available.", 1),
            (2, "Report", "Serum creatinine", previous, "Demo Hospital · Laboratory", "Creatinine: 1.2 mg/dL", 1),
            (3, "Visit", "Paediatrics outpatient visit", previous, "Demo Hospital · Paediatrics",
             "Routine follow-up recorded.", 1),
        ])
        db.execute("INSERT INTO admission_orders VALUES (1, 2, 'Dr Farhana Islam', 'Medicine ward', ?)", (today.isoformat(),))
        db.executemany("INSERT INTO beds VALUES (?, ?, ?)", [
            (1, "Medicine ward", "M-04"), (2, "Medicine ward", "M-07"), (3, "Surgery ward", "S-02"),
        ])
        db.execute("INSERT INTO prescriptions VALUES (1, 1, 'Dr Farhana Islam', ?)", (previous,))
        db.executemany("INSERT INTO prescription_items VALUES (?, 1, ?, ?, 0)", [
            (1, "Ferrous sulfate tablets", 14), (2, "Folic acid tablets", 7),
        ])

    for days in range(1, 5):
        date = (today + timedelta(days=days)).isoformat()
        db.executemany("INSERT OR IGNORE INTO slots(department, doctor, starts) VALUES (?, ?, ?)", [
            ("Medicine", "Dr Farhana Islam", f"{date} 09:00"),
            ("Medicine", "Dr Farhana Islam", f"{date} 09:30"),
            ("Paediatrics", "Dr Saif Ahmed", f"{date} 10:00"),
            ("Surgery", "Dr Tania Sultana", f"{date} 11:00"),
        ])
