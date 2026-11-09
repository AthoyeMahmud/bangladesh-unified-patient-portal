import json
import os
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import (
    Flask, abort, current_app, flash, g, redirect, render_template, request, send_from_directory, session, url_for,
)
from werkzeug.exceptions import SecurityError
from werkzeug.security import check_password_hash

from demo import DHAKA, DEMO_PASSWORD, seed


ROOT = Path(__file__).resolve().parent
MAX_ROW_ID = 2**63 - 1
METHODS = {
    "qr": ("QR code", "কিউআর কোড", "qr"),
    "barcode": ("Barcode", "বারকোড", "barcode"),
    "fingerprint": ("Fingerprint", "আঙুলের ছাপ", "fingerprint"),
    "identity": ("NID / birth certificate", "এনআইডি / জন্ম নিবন্ধন", "card"),
    "pin": ("Patient PIN", "রোগীর পিন", "key"),
}
ACTIONS = {
    "appointment": ("Make appointment", "অ্যাপয়েন্টমেন্ট", "calendar", "Choose a doctor and time"),
    "admission": ("Admission", "ভর্তি", "bed", "Use an existing admission order"),
    "reports": ("Print test reports", "পরীক্ষার রিপোর্ট", "print", "Select released lab reports"),
    "medicine": ("Take medicine", "ওষুধ সংগ্রহ", "pill", "Request prescribed medicines"),
}


def create_app(database=None):
    app = Flask(__name__)
    render_hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
    app.config.update(
        SECRET_KEY=secrets.token_bytes(32),  # shortcut: restart signs staff out, use managed keys for deployment.
        DATABASE=Path(database or os.environ.get("PORTAL_DATABASE") or ROOT / ".data" / "portal.sqlite"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=bool(render_hostname),
        SESSION_COOKIE_SAMESITE="Strict",
        MAX_CONTENT_LENGTH=64 * 1024,
        TRUSTED_HOSTS=["localhost", "127.0.0.1", "[::1]"] + ([render_hostname] if render_hostname else []),
    )
    app.config["DATABASE"].parent.mkdir(parents=True, exist_ok=True)

    @app.teardown_appcontext
    def close_db(_error):
        if "db" in g:
            g.db.close()

    with app.app_context():
        db = database_connection()
        db.executescript((ROOT / "schema.sql").read_text())
        with db:
            db.execute("BEGIN IMMEDIATE")
            seed(db)

    @app.before_request
    def authorize_request():
        if request.method == "POST":
            expected = session.get("csrf", "")
            supplied = request.form.get("csrf", "")
            if not expected or not secrets.compare_digest(expected.encode(), supplied.encode()):
                abort(400, "This form expired. Reload the page and try again.")
        g.staff = None
        if username := session.get("staff"):
            g.staff = one("SELECT username, name, role FROM staff WHERE username = ?", (username,))

    @app.after_request
    def protect_response(response):
        if request.endpoint not in {"static", "scanner_library"}:
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.context_processor
    def template_context():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return {"csrf_token": session["csrf"], "actions": ACTIONS}

    @app.template_filter("date_label")
    def date_label(value):
        parsed = datetime.fromisoformat(value)
        date = parsed.strftime("%d %b %Y")
        return f"{date} · {parsed.strftime('%H:%M')}" if len(value) > 10 else date

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def page_error(error):
        if isinstance(error, SecurityError):
            return "Invalid request host.", 400
        return render_template("error.html", error=error), error.code

    @app.get("/assets/zxing.js")
    def scanner_library():
        return send_from_directory(ROOT / "node_modules/@zxing/library/umd", "index.min.js")

    @app.get("/")
    def home():
        if not g.staff:
            return render_template("login.html", demo_password=DEMO_PASSWORD)
        if g.staff["role"] == "pharmacy":
            return redirect(url_for("pharmacy"))
        return redirect(url_for("lookup"))

    @app.post("/login")
    def login():
        if g.staff:
            return redirect(url_for("home"))
        user = one("SELECT * FROM staff WHERE username = ?", (request.form.get("username", "").strip().lower(),))
        if not user or not check_password_hash(user["password_hash"], request.form.get("password", "")):
            flash("Staff ID or password is incorrect.", "error")
            return redirect(url_for("home"))
        session.clear()
        session["staff"] = user["username"]
        return redirect(url_for("home"))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("home"))

    @app.route("/lookup", methods=["GET", "POST"])
    def lookup():
        require_role("registration")
        values = request.form if request.method == "POST" else request.args
        method = values.get("method", "pin")
        if method not in METHODS:
            abort(400, "Choose a valid identification method.")
        if request.method == "POST":
            session.pop("patient", None)
            session.pop("confirmed", None)
            identifier = request.form.get("identifier", "").strip().upper().translate(
                str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
            )
            if method == "fingerprint" and identifier == "FAILED":
                flash("The simulated scan failed. Try again or use a different method.", "error")
                return redirect(url_for("lookup", method=method))
            column = {"qr": "card_code", "barcode": "card_code", "fingerprint": "pin", "pin": "pin"}.get(method)
            if method == "identity":
                patient = one("SELECT id FROM patients WHERE nid = ? OR brn = ?", (identifier, identifier))
            else:
                patient = one(f"SELECT id FROM patients WHERE {column} = ?", (identifier,))
            if not patient:
                flash("No matching patient. Check the identifier or try another method.", "error")
                return redirect(url_for("lookup", method=method))
            session["patient"] = patient["id"]
            return redirect(url_for("patient_page", patient_id=patient["id"]))
        return render_template(
            "lookup.html", method=method, methods=METHODS,
            patients=rows("SELECT * FROM patients ORDER BY id"),
        )

    @app.post("/lookup/reset")
    def reset_lookup():
        require_role("registration")
        session.pop("patient", None)
        session.pop("confirmed", None)
        return redirect(url_for("lookup"))

    @app.get("/patient/<int:patient_id>")
    def patient_page(patient_id):
        patient = active_patient(patient_id, confirmed=False)
        confirmed = session.get("confirmed") == patient_id
        with database_connection():
            audit(patient_id, "records_viewed" if confirmed else "identity_viewed")
        return render_template(
            "patient.html", patient=patient, confirmed=confirmed,
            records=rows("""
                SELECT * FROM records WHERE patient_id = ? AND released = 1 ORDER BY on_date DESC, id
            """, (patient_id,)) if confirmed else [],
            bookings=rows("""
                SELECT a.id, s.department, s.doctor, s.starts FROM appointments a JOIN slots s ON s.id = a.slot_id
                WHERE a.patient_id = ? ORDER BY a.id DESC
            """, (patient_id,)) if confirmed else [],
            admission=one("""
                SELECT a.id, b.ward, b.label FROM admissions a JOIN beds b ON b.id = a.bed_id
                WHERE a.patient_id = ?
            """, (patient_id,)) if confirmed else None,
            prescriptions=patient_prescriptions(patient_id) if confirmed else [],
        )

    @app.post("/patient/<int:patient_id>/confirm")
    def confirm_patient(patient_id):
        active_patient(patient_id, confirmed=False)
        if session.get("confirmed") != patient_id:
            with database_connection():
                audit(patient_id, "identity_confirmed")
            session["confirmed"] = patient_id
        return redirect(url_for("patient_page", patient_id=patient_id))

    @app.route("/patient/<int:patient_id>/service/<action>", methods=["GET", "POST"])
    def service(patient_id, action):
        patient = active_patient(patient_id)
        if action not in ACTIONS:
            abort(404, "This service does not exist.")
        if request.method == "POST":
            if action == "reports":
                report_ids = request.form.getlist("report")
                if not report_ids:
                    flash("Select at least one released report.", "error")
                else:
                    return redirect(url_for("report_preview", patient_id=patient_id, ids=report_ids))
            else:
                try:
                    with database_connection() as db:
                        db.execute("BEGIN IMMEDIATE")
                        message = perform_service(patient_id, action)
                except ValueError as error:
                    flash(str(error), "error")
                else:
                    flash(message, "success")
                    return redirect(url_for("patient_page", patient_id=patient_id))
            return redirect(url_for("service", patient_id=patient_id, action=action))

        return render_template(
            "service.html", patient=patient, action=action,
            slots=rows("""
                SELECT s.* FROM slots s LEFT JOIN appointments a ON a.slot_id = s.id
                WHERE a.id IS NULL AND s.starts > ? ORDER BY s.department, s.doctor, s.starts
            """, (local_minute(),)) if action == "appointment" else [],
            orders=rows("""
                SELECT o.* FROM admission_orders o WHERE o.patient_id = ? AND NOT EXISTS (
                    SELECT 1 FROM admissions WHERE patient_id = o.patient_id
                ) ORDER BY o.id
            """, (patient_id,)) if action == "admission" else [],
            beds=rows("""
                SELECT b.* FROM beds b LEFT JOIN admissions a ON a.bed_id = b.id
                WHERE a.id IS NULL ORDER BY b.ward, b.label
            """) if action == "admission" else [],
            reports=rows("""
                SELECT * FROM records WHERE patient_id = ? AND kind = 'Report' AND released = 1
                ORDER BY on_date DESC, id
            """, (patient_id,)) if action == "reports" else [],
            prescriptions=patient_prescriptions(patient_id) if action == "medicine" else [],
        )

    @app.get("/patient/<int:patient_id>/reports/print")
    def report_preview(patient_id):
        patient = active_patient(patient_id)
        ids = request.args.getlist("ids")
        if not 1 <= len(ids) <= 20 or any(not v.isascii() or not v.isdigit() or len(v) > 9 for v in ids):
            abort(400, "Select valid reports to print.")
        selected = []
        for report_id in dict.fromkeys(map(int, ids)):
            report = one("""
                SELECT * FROM records WHERE id = ? AND patient_id = ? AND kind = 'Report' AND released = 1
            """, (report_id, patient_id))
            if not report:
                abort(404, "A selected report is unavailable for this patient.")
            selected.append(report)
        with database_connection():
            audit(patient_id, "report_previewed", ",".join(str(report["id"]) for report in selected))
        return render_template("print.html", patient=patient, title="Test reports", reports=selected, fields=[])

    @app.get("/patient/<int:patient_id>/confirmation/<kind>/<int:entry_id>")
    def confirmation(patient_id, kind, entry_id):
        patient = active_patient(patient_id)
        if not 1 <= entry_id <= MAX_ROW_ID:
            abort(404)
        if kind == "appointment":
            entry = one("""
                SELECT s.* FROM appointments a JOIN slots s ON s.id = a.slot_id
                WHERE a.patient_id = ? AND a.id = ?
            """, (patient_id, entry_id))
            if not entry:
                abort(404, "Appointment not found for this patient.")
            title = "Appointment confirmation"
            fields = [("Appointment", reference("APT", entry_id)), ("Department", entry["department"]),
                      ("Doctor", entry["doctor"]), ("Date and time", date_label(entry["starts"]) + " (Dhaka)")]
        elif kind == "admission":
            entry = one("""
                SELECT b.ward, b.label, o.clinician FROM admissions a JOIN beds b ON b.id = a.bed_id
                JOIN admission_orders o ON o.id = a.order_id WHERE a.patient_id = ? AND a.id = ?
            """, (patient_id, entry_id))
            if not entry:
                abort(404, "Admission not found for this patient.")
            title = "Admission confirmation"
            fields = [("Admission", reference("ADM", entry_id)), ("Ward", entry["ward"]),
                      ("Bed", entry["label"]), ("Ordering clinician", entry["clinician"])]
        else:
            abort(404)
        with database_connection():
            audit(patient_id, f"{kind}_previewed", str(entry_id))
        return render_template("print.html", patient=patient, title=title, reports=[], fields=fields)

    @app.get("/pharmacy")
    def pharmacy():
        require_role("pharmacy")
        queue = rows("""
            SELECT c.*, p.name, p.pin, p.id AS patient_id FROM collections c
            JOIN prescriptions rx ON rx.id = c.prescription_id JOIN patients p ON p.id = rx.patient_id
            ORDER BY (c.status = 'Completed'), c.id DESC
        """)
        with database_connection():
            for item in queue:
                audit(item["patient_id"], "collection_list_viewed", str(item["id"]))
        return render_template("pharmacy.html", queue=queue, collection=None)

    @app.route("/pharmacy/<int:collection_id>", methods=["GET", "POST"])
    def pharmacy_detail(collection_id):
        require_role("pharmacy")
        if request.method == "POST":
            try:
                with database_connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    message = dispense(collection_id)
            except ValueError as error:
                flash(str(error), "error")
            else:
                flash(message, "success")
            return redirect(url_for("pharmacy_detail", collection_id=collection_id))

        collection = get_collection(collection_id)
        with database_connection():
            audit(collection["patient_id"], "collection_viewed", str(collection_id))
        return render_template(
            "pharmacy.html", queue=[], collection=collection,
            items=prescription_items(collection["prescription_id"]),
            events=rows("SELECT * FROM collection_events WHERE collection_id = ? ORDER BY revision DESC", (collection_id,)),
        )

    @app.get("/demo/cards")
    def demo_cards():
        require_role("registration")
        return render_template("cards.html", patients=rows("SELECT * FROM patients ORDER BY id"))

    return app


def database_connection():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def rows(sql, params=()):
    return database_connection().execute(sql, params).fetchall()


def one(sql, params=()):
    return database_connection().execute(sql, params).fetchone()


def require_role(role):
    if not g.staff or g.staff["role"] != role:
        abort(403, "Sign in with the staff role authorized for this page.")


def active_patient(patient_id, *, confirmed=True):
    require_role("registration")
    if session.get("patient") != patient_id:
        abort(403, "This patient is no longer selected. Find the patient again.")
    if confirmed and session.get("confirmed") != patient_id:
        abort(403, "Confirm this patient's identity before continuing.")
    patient = one("SELECT * FROM patients WHERE id = ?", (patient_id,))
    if not patient:
        abort(404)
    return patient


def audit(patient_id, action, detail=""):
    database_connection().execute(
        "INSERT INTO audit(staff, patient_id, action, detail) VALUES (?, ?, ?, ?)",
        (g.staff["username"], patient_id, action, detail),
    )


def local_minute():
    return datetime.now(DHAKA).strftime("%Y-%m-%d %H:%M")


def reference(prefix, entry_id):
    return f"{prefix}-{entry_id:04d}"


def form_id(name):
    value = request.form.get(name, type=int)
    return value if value is not None and 1 <= value <= MAX_ROW_ID else None


def prescription_items(prescription_id):
    return rows("SELECT * FROM prescription_items WHERE prescription_id = ? ORDER BY id", (prescription_id,))


def patient_prescriptions(patient_id):
    return [
        (rx, prescription_items(rx["id"]))
        for rx in rows("""
            SELECT rx.*, c.id AS collection_id, c.status AS collection_status
            FROM prescriptions rx LEFT JOIN collections c ON c.prescription_id = rx.id
            WHERE rx.patient_id = ? ORDER BY rx.on_date DESC
        """, (patient_id,))
    ]


def perform_service(patient_id, action):
    db = database_connection()
    if action == "appointment":
        slot_id = form_id("slot")
        existing = one("SELECT * FROM appointments WHERE slot_id = ?", (slot_id,))
        if existing and existing["patient_id"] == patient_id:
            return f"Appointment {reference('APT', existing['id'])} is already booked."
        slot = one("SELECT * FROM slots WHERE id = ? AND starts > ?", (slot_id, local_minute()))
        if not slot or existing:
            raise ValueError("That appointment time is unavailable. Choose another time.")
        entry = db.execute("INSERT INTO appointments(patient_id, slot_id) VALUES (?, ?)", (patient_id, slot_id))
        code = reference("APT", entry.lastrowid)
        audit(patient_id, "appointment_booked", code)
        return f"Appointment {code} booked."

    if action == "admission":
        order_id = form_id("order")
        bed_id = form_id("bed")
        order = one("SELECT * FROM admission_orders WHERE id = ? AND patient_id = ?", (order_id, patient_id))
        if not order:
            raise ValueError("Choose an admission order for this patient.")
        existing = one("SELECT * FROM admissions WHERE patient_id = ?", (patient_id,))
        if existing:
            if (existing["order_id"], existing["bed_id"]) == (order_id, bed_id):
                return f"Admission {reference('ADM', existing['id'])} is already saved."
            raise ValueError("This patient already has an admission.")
        bed = one("""
            SELECT b.id FROM beds b LEFT JOIN admissions a ON a.bed_id = b.id
            WHERE b.id = ? AND b.ward = ? AND a.id IS NULL
        """, (bed_id, order["ward"]))
        if not bed:
            raise ValueError("That bed is unavailable for this order. Choose an available bed in the assigned ward.")
        entry = db.execute(
            "INSERT INTO admissions(patient_id, order_id, bed_id) VALUES (?, ?, ?)", (patient_id, order_id, bed_id),
        )
        code = reference("ADM", entry.lastrowid)
        audit(patient_id, "admission_saved", code)
        return f"Admission {code} saved."

    prescription_id = form_id("prescription")
    rx = one("SELECT id FROM prescriptions WHERE id = ? AND patient_id = ?", (prescription_id, patient_id))
    if not rx:
        raise ValueError("Choose an existing prescription for this patient.")
    existing = one("SELECT id FROM collections WHERE prescription_id = ?", (prescription_id,))
    if existing:
        return f"Collection {reference('MED', existing['id'])} already exists. See its status below."
    if not prescription_items(prescription_id):
        raise ValueError("This prescription has no medicines available for collection.")
    entry = db.execute("INSERT INTO collections(prescription_id) VALUES (?)", (prescription_id,))
    code = reference("MED", entry.lastrowid)
    audit(patient_id, "collection_requested", code)
    return f"Collection {code} sent to pharmacy."


def get_collection(collection_id):
    if not 1 <= collection_id <= MAX_ROW_ID:
        abort(404, "Medicine collection request not found.")
    collection = one("""
        SELECT c.*, p.id AS patient_id, p.pin, p.name, p.birth_date, rx.clinician, rx.on_date
        FROM collections c JOIN prescriptions rx ON rx.id = c.prescription_id
        JOIN patients p ON p.id = rx.patient_id WHERE c.id = ?
    """, (collection_id,))
    if not collection:
        abort(404, "Medicine collection request not found.")
    return collection


def dispense(collection_id):
    db = database_connection()
    collection = get_collection(collection_id)
    revision = request.form.get("revision", type=int)
    if revision is None or revision != collection["revision"]:
        raise ValueError("This request has changed. Review the latest quantities before saving again.")
    if collection["status"] == "Completed":
        raise ValueError("All medicines on this request have already been supplied.")
    if request.form.get("identity") != "confirmed":
        raise ValueError("Confirm the patient before recording collection.")
    outcome = request.form.get("outcome")
    if outcome not in {"supply", "unavailable"}:
        raise ValueError("Choose how to record this collection.")

    quantities = {}
    remaining_total = 0
    for item in prescription_items(collection["prescription_id"]):
        remaining = item["prescribed"] - item["supplied"]
        raw = request.form.get(f"quantity_{item['id']}", "0" if remaining == 0 else "")
        valid_quantity = raw.isascii() and raw.isdecimal() and len(raw) <= 6
        quantity = int(raw) if valid_quantity else 0
        if outcome == "supply" and (not valid_quantity or quantity > remaining):
            raise ValueError(f"Enter a quantity between 0 and {remaining} for {item['medicine']}.")
        if outcome == "supply" and quantity:
            quantities[item["medicine"]] = quantity
            db.execute("UPDATE prescription_items SET supplied = supplied + ? WHERE id = ?", (quantity, item["id"]))
            remaining -= quantity
        remaining_total += remaining

    if outcome == "supply" and not quantities:
        raise ValueError("Enter a positive quantity for at least one medicine, or mark the request unavailable.")
    status = "Awaiting stock" if outcome == "unavailable" else (
        "Partially supplied" if remaining_total else "Completed"
    )
    db.execute("UPDATE collections SET status = ?, revision = revision + 1 WHERE id = ?", (status, collection_id))
    db.execute("""
        INSERT INTO collection_events(collection_id, revision, staff, outcome, quantities) VALUES (?, ?, ?, ?, ?)
    """, (collection_id, revision + 1, g.staff["username"], status, json.dumps(quantities)))
    audit(collection["patient_id"], "collection_updated", f"{reference('MED', collection_id)} · {status}")
    return f"Collection {reference('MED', collection_id)} updated. {status}."


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", "5050")))
