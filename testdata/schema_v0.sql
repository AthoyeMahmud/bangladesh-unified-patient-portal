CREATE TABLE IF NOT EXISTS staff (
    username TEXT PRIMARY KEY, name TEXT NOT NULL, password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('registration', 'pharmacy'))
);
CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY, pin TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    birth_date TEXT NOT NULL, sex TEXT NOT NULL, area TEXT NOT NULL,
    card_code TEXT NOT NULL UNIQUE, nid TEXT UNIQUE, brn TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES patients(id),
    kind TEXT NOT NULL, title TEXT NOT NULL, on_date TEXT NOT NULL,
    source TEXT NOT NULL, details TEXT NOT NULL,
    released INTEGER NOT NULL DEFAULT 1 CHECK (released IN (0, 1))
);
CREATE TABLE IF NOT EXISTS slots (
    id INTEGER PRIMARY KEY, department TEXT NOT NULL, doctor TEXT NOT NULL, starts TEXT NOT NULL,
    UNIQUE (doctor, starts)
);
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES patients(id),
    slot_id INTEGER NOT NULL UNIQUE REFERENCES slots(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS admission_orders (
    id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES patients(id),
    clinician TEXT NOT NULL, ward TEXT NOT NULL, on_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS beds (
    id INTEGER PRIMARY KEY, ward TEXT NOT NULL, label TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS admissions (
    id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL UNIQUE REFERENCES patients(id),
    order_id INTEGER NOT NULL UNIQUE REFERENCES admission_orders(id),
    bed_id INTEGER NOT NULL UNIQUE REFERENCES beds(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS prescriptions (
    id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES patients(id),
    clinician TEXT NOT NULL, on_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prescription_items (
    id INTEGER PRIMARY KEY, prescription_id INTEGER NOT NULL REFERENCES prescriptions(id),
    medicine TEXT NOT NULL, prescribed INTEGER NOT NULL CHECK (prescribed > 0),
    supplied INTEGER NOT NULL DEFAULT 0 CHECK (supplied BETWEEN 0 AND prescribed)
);
CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY, prescription_id INTEGER NOT NULL UNIQUE REFERENCES prescriptions(id),
    status TEXT NOT NULL DEFAULT 'Requested'
        CHECK (status IN ('Requested', 'Partially supplied', 'Awaiting stock', 'Completed')),
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS collection_events (
    id INTEGER PRIMARY KEY, collection_id INTEGER NOT NULL REFERENCES collections(id),
    revision INTEGER NOT NULL, staff TEXT NOT NULL REFERENCES staff(username),
    outcome TEXT NOT NULL, quantities TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (collection_id, revision)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, staff TEXT NOT NULL REFERENCES staff(username),
    patient_id INTEGER REFERENCES patients(id), action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
