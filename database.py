from pathlib import Path

from demo import DEMO_ADMISSION, DEMO_ITEMS, DEMO_PRESCRIPTION, PATIENTS, seed


# Rename dependent tables first and drop their legacy copies before the parents.
REBUILT_TABLES = ("records", "appointments", "admissions", "collection_events", "collections")


def initialize(db):
    with db:
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise RuntimeError("This database was created by a newer version of the portal.")
        if version == 0:
            legacy = db.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='patients'").fetchone()
            if legacy:
                for table in REBUILT_TABLES:
                    db.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
                for table in ("admission_orders", "prescriptions"):
                    db.execute(f"ALTER TABLE {table} ADD COLUMN active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1))")
            # executescript would commit the open migration transaction.
            for statement in Path(__file__).with_name("schema.sql").read_text().split(";"):
                if statement.strip():
                    db.execute(statement)
            if legacy:
                migrate_legacy_data(db)
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeError("The database contains broken references. The migration was rolled back.")
            db.execute("PRAGMA user_version = 1")
        seed(db)


def migrate_legacy_data(db):
    db.execute("INSERT INTO records SELECT * FROM records_old")
    db.execute("""
        INSERT INTO appointments(id,patient_id,slot_id,created_at,reference)
        SELECT id,patient_id,slot_id,created_at,printf('APT-%04d',id) FROM appointments_old
    """)
    db.execute("""
        INSERT INTO admissions(id,patient_id,order_id,bed_id,created_at,reference)
        SELECT id,patient_id,order_id,bed_id,created_at,printf('ADM-%04d',id) FROM admissions_old
    """)
    db.execute("""
        INSERT INTO collections(id,prescription_id,status,revision,created_at,reference)
        SELECT id,prescription_id,status,revision,created_at,printf('MED-%04d',id) FROM collections_old
    """)
    db.execute("INSERT INTO collection_events SELECT * FROM collection_events_old")
    for table in REBUILT_TABLES:
        db.execute(f"DROP TABLE {table}_old")

    db.execute("""
        UPDATE admission_orders SET active=1 WHERE id=? AND patient_id=? AND clinician=? AND ward=?
            AND patient_id IN (SELECT id FROM patients WHERE pin=? AND card_code=?)
    """, (*DEMO_ADMISSION, PATIENTS[1][1], PATIENTS[1][6]))
    items = db.execute("""
        SELECT id,prescription_id,medicine,prescribed FROM prescription_items WHERE prescription_id=? ORDER BY id
    """, (DEMO_PRESCRIPTION[0],))
    if list(map(tuple, items)) == DEMO_ITEMS:
        db.execute("""
            UPDATE prescriptions SET active=1 WHERE id=? AND patient_id=? AND clinician=?
                AND patient_id IN (SELECT id FROM patients WHERE pin=? AND card_code=?)
        """, (*DEMO_PRESCRIPTION, PATIENTS[0][1], PATIENTS[0][6]))
