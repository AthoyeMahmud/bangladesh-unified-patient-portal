import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import (
    Flask, abort, current_app, flash, g, redirect, render_template, request, send_from_directory, session, url_for,
)
from werkzeug.exceptions import SecurityError, ServiceUnavailable
from werkzeug.security import check_password_hash

from database import initialize
from demo import DHAKA, DEMO_PASSWORD, DEMO_SLOTS, seed_slots


ROOT = Path(__file__).resolve().parent
MAX_ROW_ID = 2**63 - 1
REPORT_LIMIT = 20
QUEUE_PAGE_SIZE = 25
DB_TIMEOUT = 2
BANGLA_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
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
    https_hostname = os.environ.get("PORTAL_HTTPS_HOST", "").strip().lower()
    if https_hostname and not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*", https_hostname):
        raise ValueError("PORTAL_HTTPS_HOST must be an exact hostname without a scheme, port, or wildcard.")
    trusted_hosts = ["localhost", "127.0.0.1", "[::1]"]
    trusted_hosts.extend(host for host in (render_hostname, https_hostname) if host)
    app.config.update(
        SECRET_KEY=secrets.token_bytes(32),  # shortcut: restart signs staff out, use managed keys for deployment.
        DATABASE=Path(database or os.environ.get("PORTAL_DATABASE") or ROOT / ".data" / "portal.sqlite"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=bool(render_hostname or https_hostname),
        SESSION_COOKIE_SAMESITE="Strict",
        MAX_CONTENT_LENGTH=64 * 1024,
        TRUSTED_HOSTS=trusted_hosts,
    )
    app.config["DATABASE"].parent.mkdir(parents=True, exist_ok=True)

    @app.teardown_appcontext
    def close_db(_error):
        if "db" in g:
            g.db.close()

    with app.app_context():
        initialize(database_connection())

    @app.before_request
    def authorize_request():
        g.staff = None
        if request.endpoint in {"static", "scanner_library"}:
            return
        if request.method == "POST":
            expected = session.get("csrf", "")
            supplied = request.form.get("csrf", "")
            if not expected or not secrets.compare_digest(expected.encode(), supplied.encode()):
                abort(400, "This form expired. Reload the page and try again.")
        g.staff = authenticated_staff()

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
        return {
            "csrf_token": session["csrf"], "actions": ACTIONS,
            "selection_token": g.staff["selection"] if g.get("staff") else "",
        }

    @app.template_filter("date_label")
    def date_label(value):
        try:
            parsed = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return "Date unavailable"
        label = parsed.strftime("%d %b %Y")
        return f"{label} · {parsed.strftime('%H:%M')}" if len(value) > 10 else label

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def page_error(error):
        if isinstance(error, SecurityError):
            return "Invalid request host.", 400
        return render_template("error.html", error=error), error.code

    @app.errorhandler(sqlite3.OperationalError)
    def database_error(error):
        code = getattr(error, "sqlite_errorcode", 0) & 0xFF
        if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise error
        message = ServiceUnavailable(
            "The portal is busy. If you just submitted a change, check the saved services or collection history before retrying."
        )
        return render_template("error.html", error=message), 503, {"Retry-After": str(DB_TIMEOUT)}

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
        session_id = secrets.token_urlsafe(32)
        with write_transaction() as db:
            db.execute("DELETE FROM staff_sessions WHERE expires_at <= CURRENT_TIMESTAMP")
            db.execute("INSERT INTO staff_sessions(id,username) VALUES (?,?)", (session_id, user["username"]))
        session.clear()
        session["staff_session"] = session_id
        return redirect(url_for("home"))

    @app.post("/logout")
    def logout():
        if session_id := session.get("staff_session"):
            with write_transaction() as db:
                db.execute("DELETE FROM staff_sessions WHERE id = ?", (session_id,))
        session.clear()
        return redirect(url_for("home"))

    @app.route("/lookup", methods=["GET", "POST"])
    def lookup():
        require_role("registration")
        values = request.form if request.method == "POST" else request.args
        method = values.get("method", "pin")
        if request.method == "POST":
            with write_transaction():
                require_role("registration")
                try:
                    patient_id = identify_patient(method)
                except ValueError as error:
                    select_patient(None)
                    flash(str(error), "error")
                    return redirect(url_for("lookup", method=method if method in METHODS else "pin"))
                select_patient(patient_id)
            return redirect(url_for("patient_page", patient_id=patient_id))
        if method not in METHODS:
            abort(400, "Choose a valid identification method.")
        return render_template(
            "lookup.html", method=method, methods=METHODS,
            patients=rows("SELECT * FROM patients ORDER BY id"),
        )

    @app.post("/lookup/reset")
    def reset_lookup():
        with write_transaction():
            require_role("registration")
            if g.staff["patient_id"] is not None:
                require_selection()
            select_patient(None)
        return redirect(url_for("lookup"))

    @app.get("/patient/<int:patient_id>")
    def patient_page(patient_id):
        records, bookings, admissions, prescriptions = [], [], [], []
        with write_transaction():
            patient = active_patient(patient_id, confirmed=False)
            can_confirm = identity_is_complete(patient)
            confirmed = can_confirm and bool(g.staff["confirmed"])
            audit(patient_id, "records_viewed" if confirmed else "identity_viewed")
            if confirmed:
                records = rows("""
                    SELECT * FROM records WHERE patient_id = ? AND released = 1 ORDER BY on_date DESC, id
                """, (patient_id,))
                bookings = rows("""
                    SELECT a.id, a.reference, a.cancelled_at, s.department, s.doctor, s.starts
                    FROM appointments a JOIN slots s ON s.id = a.slot_id
                    WHERE a.patient_id = ? ORDER BY a.id DESC
                """, (patient_id,))
                admissions = rows("""
                    SELECT a.id, a.reference, a.released_at, a.release_reason, b.ward, b.label
                    FROM admissions a JOIN beds b ON b.id = a.bed_id WHERE a.patient_id = ? ORDER BY a.id DESC
                """, (patient_id,))
                prescriptions = patient_prescriptions(patient_id)
        return render_template(
            "patient.html", patient=patient, confirmed=confirmed, can_confirm=can_confirm,
            records=records, bookings=bookings, admissions=admissions, prescriptions=prescriptions, now=local_minute(),
        )

    @app.post("/patient/<int:patient_id>/confirm")
    def confirm_patient(patient_id):
        with write_transaction() as db:
            patient = active_patient(patient_id, confirmed=False)
            if not identity_is_complete(patient):
                abort(403, "Manual verification required. Use the hospital's manual registration process.")
            if not g.staff["confirmed"]:
                audit(patient_id, "identity_confirmed")
                db.execute("UPDATE staff_sessions SET confirmed=1 WHERE id=?", (session["staff_session"],))
        return redirect(url_for("patient_page", patient_id=patient_id))

    @app.route("/patient/<int:patient_id>/service/<action>", methods=["GET", "POST"])
    def service(patient_id, action):
        patient = active_patient(patient_id)
        if action not in ACTIONS:
            abort(404, "This service does not exist.")
        if request.method == "POST":
            if action == "reports":
                try:
                    ids = report_ids(request.form.getlist("report"))
                except ValueError as error:
                    flash(str(error), "error")
                else:
                    return redirect(url_for("report_preview", patient_id=patient_id, ids=ids))
            else:
                try:
                    with write_transaction():
                        active_patient(patient_id)
                        message = perform_service(patient_id, action)
                except ValueError as error:
                    flash(str(error), "error")
                else:
                    flash(message, "success")
                    return redirect(url_for("patient_page", patient_id=patient_id))
            return redirect(url_for("service", patient_id=patient_id, action=action))

        if action == "appointment":
            ensure_demo_schedule()
        return render_template(
            "service.html", patient=patient, action=action, report_limit=REPORT_LIMIT,
            request_key=secrets.token_hex(16) if action == "appointment" else None,
            slots=rows("""
                SELECT s.* FROM slots s
                WHERE s.starts > ?
                    AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.slot_id = s.id AND a.cancelled_at IS NULL)
                    AND NOT EXISTS (
                        SELECT 1 FROM appointments a JOIN slots booked ON booked.id = a.slot_id
                        WHERE a.patient_id = ? AND a.cancelled_at IS NULL AND booked.starts > ?
                            AND (booked.department = s.department OR booked.starts = s.starts)
                    )
                ORDER BY s.department, s.doctor, s.starts
            """, (local_minute(), patient_id, local_minute())) if action == "appointment" else [],
            orders=rows("""
                SELECT o.* FROM admission_orders o WHERE o.patient_id = ? AND o.active = 1
                AND NOT EXISTS (SELECT 1 FROM admissions WHERE order_id = o.id)
                AND NOT EXISTS (SELECT 1 FROM admissions WHERE patient_id = o.patient_id AND released_at IS NULL)
                ORDER BY o.id
            """, (patient_id,)) if action == "admission" else [],
            beds=rows("""
                SELECT b.* FROM beds b LEFT JOIN admissions a ON a.bed_id = b.id AND a.released_at IS NULL
                WHERE a.id IS NULL ORDER BY b.ward, b.label
            """) if action == "admission" else [],
            reports=rows("""
                SELECT * FROM records WHERE patient_id = ? AND kind = 'Report' AND released = 1
                ORDER BY on_date DESC, id
            """, (patient_id,)) if action == "reports" else [],
            prescriptions=patient_prescriptions(patient_id) if action == "medicine" else [],
        )

    @app.post("/patient/<int:patient_id>/appointments/<int:entry_id>/cancel")
    def cancel_appointment(patient_id, entry_id):
        with write_transaction() as db:
            active_patient(patient_id)
            entry = get_appointment(patient_id, entry_id)
            code = entry["reference"]
            if entry["cancelled_at"]:
                flash(f"Appointment {code} was already cancelled.", "success")
            elif entry["starts"] <= local_minute():
                flash("Only future appointments can be cancelled.", "error")
            else:
                db.execute("UPDATE appointments SET cancelled_at=CURRENT_TIMESTAMP WHERE id=?", (entry_id,))
                audit(patient_id, "appointment_cancelled", code)
                flash(f"Appointment {code} cancelled. The slot is available again.", "success")
        return redirect(url_for("patient_page", patient_id=patient_id))

    @app.post("/patient/<int:patient_id>/admissions/<int:entry_id>/release")
    def release_admission(patient_id, entry_id):
        with write_transaction() as db:
            active_patient(patient_id)
            entry = get_admission(patient_id, entry_id)
            reason = request.form.get("reason")
            if entry["released_at"]:
                flash("This bed assignment was already released.", "success")
            elif reason not in ("discharge", "transfer") or request.form.get("ward_confirmed") != "confirmed":
                flash("Confirm with the ward that the patient has left the bed and choose the reason.", "error")
            else:
                db.execute("""
                    UPDATE admissions SET released_at=CURRENT_TIMESTAMP, release_reason=? WHERE id=?
                """, (reason, entry_id))
                audit(patient_id, "bed_released", f"{entry['reference']} · {reason}")
                flash(f"Bed released for admission {entry['reference']}.", "success")
        return redirect(url_for("patient_page", patient_id=patient_id))

    @app.get("/patient/<int:patient_id>/reports/print")
    def report_preview(patient_id):
        with write_transaction():
            patient = active_patient(patient_id)
            try:
                ids = report_ids(request.args.getlist("ids"))
            except ValueError as error:
                abort(400, str(error))
            selected = []
            for report_id in ids:
                report = one("""
                    SELECT * FROM records WHERE id = ? AND patient_id = ? AND kind = 'Report' AND released = 1
                """, (report_id, patient_id))
                if not report:
                    abort(404, "A selected report is unavailable for this patient.")
                selected.append(report)
            audit(patient_id, "report_previewed", ",".join(str(report["id"]) for report in selected))
        return render_template("print.html", patient=patient, title="Test reports", reports=selected, fields=[])

    @app.get("/patient/<int:patient_id>/confirmation/<kind>/<int:entry_id>")
    def confirmation(patient_id, kind, entry_id):
        with write_transaction():
            patient = active_patient(patient_id)
            if kind == "appointment":
                entry = get_appointment(patient_id, entry_id)
                title = "Appointment confirmation"
                fields = [
                    ("Appointment", entry["reference"]), ("Status", "Cancelled" if entry["cancelled_at"] else "Booked"),
                    ("Department", entry["department"]), ("Doctor", entry["doctor"]),
                    ("Date and time", date_label(entry["starts"]) + " (Dhaka)"),
                ]
            elif kind == "admission":
                entry = get_admission(patient_id, entry_id)
                title = "Admission confirmation"
                status = {"discharge": "Discharged", "transfer": "Transferred"}.get(entry["release_reason"], "Admitted")
                fields = [
                    ("Admission", entry["reference"]), ("Status", status), ("Ward", entry["ward"]),
                    ("Bed", entry["label"]), ("Ordering clinician", entry["clinician"]),
                ]
            else:
                abort(404)
            audit(patient_id, f"{kind}_previewed", entry["reference"])
        return render_template("print.html", patient=patient, title=title, reports=[], fields=fields)

    @app.get("/pharmacy")
    def pharmacy():
        require_role("pharmacy")
        view = request.args.get("view", "active")
        raw_page = request.args.get("page", "1")
        if view not in ("active", "history"):
            abort(400, "Choose Active requests or History.")
        if not raw_page.isascii() or not raw_page.isdecimal() or len(raw_page) > 19:
            abort(400, "Choose a valid queue page.")
        page = int(raw_page)
        if not 1 <= page <= MAX_ROW_ID // QUEUE_PAGE_SIZE:
            abort(400, "Choose a valid queue page.")
        active = view == "active"
        direction = "ASC" if active else "DESC"
        with write_transaction():
            require_role("pharmacy")
            items = rows(f"""
                SELECT c.id, c.reference, p.name, p.pin,
                    CASE WHEN rx.active = 0 AND c.status <> 'Completed' THEN 'Prescription inactive'
                        ELSE c.status END AS status
                FROM collections c JOIN prescriptions rx ON rx.id = c.prescription_id
                JOIN patients p ON p.id = rx.patient_id
                WHERE (rx.active = 1 AND c.status <> 'Completed') = ?
                ORDER BY c.created_at {direction}, c.id {direction} LIMIT ? OFFSET ?
            """, (active, QUEUE_PAGE_SIZE + 1, (page - 1) * QUEUE_PAGE_SIZE))
            queue = items[:QUEUE_PAGE_SIZE]
            audit(None, "collection_list_viewed", json.dumps({"view": view, "page": page, "ids": [i["id"] for i in queue]}))
        return render_template(
            "pharmacy.html", queue=queue, collection=None, view=view, page=page, has_next=len(items) > QUEUE_PAGE_SIZE,
        )

    @app.route("/pharmacy/<int:collection_id>", methods=["GET", "POST"])
    def pharmacy_detail(collection_id):
        require_role("pharmacy")
        error_message = None
        if request.method == "POST":
            try:
                with write_transaction():
                    require_role("pharmacy")
                    message = dispense(collection_id)
            except ValueError as error:
                error_message = str(error)
            else:
                flash(message, "success")
                return redirect(url_for("pharmacy_detail", collection_id=collection_id))

        with write_transaction():
            require_role("pharmacy")
            collection = get_collection(collection_id)
            items = prescription_items(collection["prescription_id"])
            events = collection_history(collection_id)
            audit(collection["patient_id"], "collection_viewed", str(collection_id))
        if error_message:
            flash(error_message, "error")
        return render_template(
            "pharmacy.html", queue=[], collection=collection,
            items=items, events=events, can_confirm=identity_is_complete(collection),
            pending=request.form if request.form.get("revision", type=int) == collection["revision"] else {},
        ), 400 if error_message else 200

    @app.get("/demo/cards")
    def demo_cards():
        require_role("registration")
        return render_template("cards.html", patients=rows("SELECT * FROM patients ORDER BY id"))

    return app


def database_connection():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"], timeout=DB_TIMEOUT)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def rows(sql, params=()):
    return database_connection().execute(sql, params).fetchall()


def one(sql, params=()):
    return database_connection().execute(sql, params).fetchone()


def authenticated_staff():
    if session_id := session.get("staff_session"):
        return one("""
            SELECT u.username, u.name, u.role, s.patient_id, s.selection, s.confirmed
            FROM staff_sessions s JOIN staff u ON u.username = s.username
            WHERE s.id = ? AND s.expires_at > CURRENT_TIMESTAMP
        """, (session_id,))
    return None


@contextmanager
def write_transaction():
    with database_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        # Authorization must see the selection and session after acquiring the write lock.
        g.staff = authenticated_staff()
        yield db


def require_role(role):
    if not g.staff or g.staff["role"] != role:
        abort(403, "Sign in with the staff role authorized for this page.")


def require_selection():
    if request.form.get("selection") != g.staff["selection"]:
        abort(403, "This patient form expired. Open the current patient record and try again.")


def identify_patient(method):
    if method not in METHODS:
        raise ValueError("Choose a valid identification method.")
    identifier = request.form.get("identifier", "").strip().upper().translate(BANGLA_DIGITS)
    if method == "identity":
        column = request.form.get("identity_type")
        if column not in ("nid", "brn"):
            raise ValueError("Choose NID or birth registration number.")
        identifier = "".join(identifier.split())
    else:
        column = "card_code" if method in ("qr", "barcode") else "pin"
    if not 1 <= len(identifier) <= 80:
        raise ValueError("Enter a valid patient identifier. Use manual registration if it cannot be verified.")
    if method == "fingerprint" and identifier == "FAILED":
        raise ValueError("The simulated scan failed. Try again or use a different method.")
    patient = one(f"SELECT id FROM patients WHERE {column} = ?", (identifier,))
    if not patient:
        raise ValueError("No matching patient. Try another method or use manual registration.")
    return patient["id"]


def select_patient(patient_id):
    database_connection().execute("""
        UPDATE staff_sessions SET patient_id=?, selection=?, confirmed=0 WHERE id=?
    """, (patient_id, secrets.token_urlsafe(24), session["staff_session"]))


def active_patient(patient_id, *, confirmed=True):
    require_role("registration")
    if g.staff["patient_id"] != patient_id:
        abort(403, "This patient is no longer selected. Find the patient again.")
    if request.method == "POST":
        require_selection()
    if confirmed and not g.staff["confirmed"]:
        abort(403, "Confirm this patient's identity before continuing.")
    patient = one("SELECT * FROM patients WHERE id = ?", (patient_id,))
    if not patient:
        abort(404)
    if confirmed and not identity_is_complete(patient):
        abort(403, "Manual verification required. Use the hospital's manual registration process.")
    return patient


def identity_is_complete(patient):
    try:
        born = date.fromisoformat(patient["birth_date"])
    except (ValueError, TypeError):
        return False
    return born.isoformat() == patient["birth_date"] and bool(patient["name"].strip())


def audit(patient_id, action, detail=""):
    database_connection().execute(
        "INSERT INTO audit(staff, patient_id, action, detail) VALUES (?, ?, ?, ?)",
        (g.staff["username"], patient_id, action, detail),
    )


def local_minute():
    return datetime.now(DHAKA).strftime("%Y-%m-%d %H:%M")


def new_reference(prefix):
    return f"{prefix}-{secrets.token_hex(10).upper()}"


def form_id(name):
    value = request.form.get(name, type=int)
    return value if value is not None and 1 <= value <= MAX_ROW_ID else None


def report_ids(raw_ids):
    if not raw_ids:
        raise ValueError("Select at least one released report.")
    if len(raw_ids) > REPORT_LIMIT:
        raise ValueError(f"Select up to {REPORT_LIMIT} reports.")
    if any(not v.isascii() or not v.isdecimal() or len(v) > 19 or not 1 <= int(v) <= MAX_ROW_ID for v in raw_ids):
        raise ValueError("Select valid reports to print.")
    return list(dict.fromkeys(map(int, raw_ids)))


def prescription_items(prescription_id):
    return rows("SELECT * FROM prescription_items WHERE prescription_id = ? ORDER BY id", (prescription_id,))


def patient_prescriptions(patient_id):
    return [
        (rx, prescription_items(rx["id"]))
        for rx in rows("""
            SELECT rx.*, c.id AS collection_id, c.reference AS collection_reference,
                CASE WHEN rx.active = 0 AND c.status <> 'Completed' THEN 'Prescription inactive'
                    ELSE c.status END AS collection_status
            FROM prescriptions rx LEFT JOIN collections c ON c.prescription_id = rx.id
            WHERE rx.patient_id = ? ORDER BY rx.on_date DESC
        """, (patient_id,))
    ]


def ensure_demo_schedule():
    today = date.fromisoformat(local_minute()[:10])
    last_day = (today + timedelta(days=4)).isoformat()
    _, doctor, time = DEMO_SLOTS[-1]
    # shortcut: the last seeded slot marks a complete batch, use an imported schedule for a hospital.
    if not one("SELECT 1 FROM slots WHERE doctor=? AND starts=?", (doctor, f"{last_day} {time}")):
        with write_transaction() as db:
            require_role("registration")
            seed_slots(db, today)


def get_appointment(patient_id, entry_id):
    if 1 <= entry_id <= MAX_ROW_ID:
        entry = one("""
            SELECT a.*, s.department, s.doctor, s.starts FROM appointments a JOIN slots s ON s.id = a.slot_id
            WHERE a.patient_id = ? AND a.id = ?
        """, (patient_id, entry_id))
        if entry:
            return entry
    abort(404, "Appointment not found for this patient.")


def get_admission(patient_id, entry_id):
    if 1 <= entry_id <= MAX_ROW_ID:
        entry = one("""
            SELECT a.*, b.ward, b.label, o.clinician FROM admissions a JOIN beds b ON b.id = a.bed_id
            JOIN admission_orders o ON o.id = a.order_id WHERE a.patient_id = ? AND a.id = ?
        """, (patient_id, entry_id))
        if entry:
            return entry
    abort(404, "Admission not found for this patient.")


def perform_service(patient_id, action):
    db = database_connection()
    if action == "appointment":
        slot_id = form_id("slot")
        request_key = request.form.get("request_key", "")
        if not re.fullmatch(r"[0-9a-f]{32}", request_key):
            raise ValueError("Open a new booking form and choose an appointment time.")
        previous = one("""
            SELECT a.* FROM booking_requests r JOIN appointments a ON a.id = r.appointment_id
            WHERE r.request_key = ?
        """, (request_key,))
        if previous:
            if (previous["patient_id"], previous["slot_id"]) != (patient_id, slot_id):
                raise ValueError("This booking form was already used. Open a new form to make another booking.")
            status = "was cancelled" if previous["cancelled_at"] else "is already booked"
            return f"Appointment {previous['reference']} {status}."

        now = local_minute()
        slot = one("SELECT * FROM slots WHERE id = ? AND starts > ?", (slot_id, now))
        existing = one("SELECT * FROM appointments WHERE slot_id = ? AND cancelled_at IS NULL", (slot_id,))
        if not slot or (existing and existing["patient_id"] != patient_id):
            raise ValueError("That appointment time is unavailable. Choose another time.")
        if existing:
            db.execute("INSERT INTO booking_requests VALUES (?,?)", (request_key, existing["id"]))
            return f"Appointment {existing['reference']} is already booked."
        conflicts = rows("""
            SELECT s.department, s.starts FROM appointments a JOIN slots s ON s.id = a.slot_id
            WHERE a.patient_id = ? AND a.cancelled_at IS NULL AND s.starts > ?
        """, (patient_id, now))
        if any(booking["starts"] == slot["starts"] for booking in conflicts):
            raise ValueError("This patient already has an appointment at that time.")
        if any(booking["department"] == slot["department"] for booking in conflicts):
            raise ValueError("This patient already has a future appointment in this department.")
        code = new_reference("APT")
        entry = db.execute(
            "INSERT INTO appointments(patient_id,slot_id,reference) VALUES (?,?,?)", (patient_id, slot_id, code),
        )
        db.execute("INSERT INTO booking_requests VALUES (?,?)", (request_key, entry.lastrowid))
        audit(patient_id, "appointment_booked", code)
        return f"Appointment {code} booked."

    if action == "admission":
        order_id = form_id("order")
        bed_id = form_id("bed")
        order = one("SELECT * FROM admission_orders WHERE id = ? AND patient_id = ?", (order_id, patient_id))
        if not order:
            raise ValueError("Choose an admission order for this patient.")
        previous = one("SELECT * FROM admissions WHERE order_id = ?", (order_id,))
        if previous and previous["released_at"]:
            raise ValueError("This admission was already released. Choose a new active admission order.")
        if previous and previous["bed_id"] == bed_id:
            return f"Admission {previous['reference']} is already saved."
        if not order["active"]:
            raise ValueError("Choose an active admission order for this patient.")
        if one("SELECT 1 FROM admissions WHERE patient_id = ? AND released_at IS NULL", (patient_id,)):
            raise ValueError("This patient already has an admission.")
        bed = one("""
            SELECT b.id FROM beds b LEFT JOIN admissions a ON a.bed_id = b.id AND a.released_at IS NULL
            WHERE b.id = ? AND b.ward = ? AND a.id IS NULL
        """, (bed_id, order["ward"]))
        if not bed:
            raise ValueError("That bed is unavailable for this order. Choose an available bed in the assigned ward.")
        code = new_reference("ADM")
        db.execute(
            "INSERT INTO admissions(patient_id,order_id,bed_id,reference) VALUES (?,?,?,?)",
            (patient_id, order_id, bed_id, code),
        )
        audit(patient_id, "admission_saved", code)
        return f"Admission {code} saved."

    prescription_id = form_id("prescription")
    rx = one("SELECT active FROM prescriptions WHERE id = ? AND patient_id = ?", (prescription_id, patient_id))
    if not rx:
        raise ValueError("Choose an existing prescription for this patient.")
    if not rx["active"]:
        raise ValueError("Choose an active prescription for this patient.")
    existing = one("SELECT reference FROM collections WHERE prescription_id = ?", (prescription_id,))
    if existing:
        return f"Collection {existing['reference']} already exists. See its status below."
    if not prescription_items(prescription_id):
        raise ValueError("This prescription has no medicines available for collection.")
    code = new_reference("MED")
    db.execute("INSERT INTO collections(prescription_id,reference) VALUES (?,?)", (prescription_id, code))
    audit(patient_id, "collection_requested", code)
    return f"Collection {code} sent to pharmacy."


def get_collection(collection_id):
    if not 1 <= collection_id <= MAX_ROW_ID:
        abort(404, "Medicine collection request not found.")
    collection = one("""
        SELECT c.*, p.id AS patient_id, p.pin, p.name, p.birth_date, rx.clinician, rx.on_date,
            rx.active AS prescription_active
        FROM collections c JOIN prescriptions rx ON rx.id = c.prescription_id
        JOIN patients p ON p.id = rx.patient_id WHERE c.id = ?
    """, (collection_id,))
    if not collection:
        abort(404, "Medicine collection request not found.")
    return collection


def collection_history(collection_id):
    events = []
    for event in rows("SELECT * FROM collection_events WHERE collection_id = ? ORDER BY revision DESC", (collection_id,)):
        quantities = json.loads(event["quantities"])
        if isinstance(quantities, dict):
            quantities = [{"medicine": name, "quantity": amount} for name, amount in quantities.items()]
        events.append({**event, "lines": quantities})
    return events


def dispense(collection_id):
    db = database_connection()
    collection = get_collection(collection_id)
    revision = request.form.get("revision", type=int)
    if revision is None or revision != collection["revision"]:
        raise ValueError("This request has changed. Review the latest quantities before saving again.")
    if collection["status"] == "Completed":
        raise ValueError("All medicines on this request have already been supplied.")
    if not collection["prescription_active"]:
        raise ValueError("Prescription inactive. Contact the prescribing desk before supplying more medicine.")
    if not identity_is_complete(collection):
        raise ValueError("Manual verification required before recording collection.")
    if request.form.get("identity") != "confirmed":
        raise ValueError("Confirm the patient before recording collection.")
    outcome = request.form.get("outcome")
    if outcome not in {"supply", "unavailable"}:
        raise ValueError("Choose how to record this collection.")

    quantities = []
    remaining_total = 0
    for item in prescription_items(collection["prescription_id"]):
        remaining = item["prescribed"] - item["supplied"]
        raw = request.form.get(f"quantity_{item['id']}", "").strip().translate(BANGLA_DIGITS)
        if not raw and (remaining == 0 or outcome == "unavailable"):
            raw = "0"
        valid_quantity = raw.isascii() and raw.isdecimal() and len(raw) <= 6
        quantity = int(raw) if valid_quantity else 0
        if not valid_quantity or quantity > remaining:
            raise ValueError(f"Enter a quantity between 0 and {remaining} for {item['medicine']}.")
        if quantity:
            quantities.append({"item_id": item["id"], "medicine": item["medicine"], "quantity": quantity})
        remaining_total += remaining - quantity

    if outcome == "supply" and not quantities:
        raise ValueError("Enter a positive quantity for at least one medicine, or mark the request unavailable.")
    if outcome == "unavailable" and quantities:
        raise ValueError("Save the entered quantities first, then mark the remaining medicines unavailable.")
    db.executemany(
        "UPDATE prescription_items SET supplied = supplied + ? WHERE id = ?",
        [(line["quantity"], line["item_id"]) for line in quantities],
    )
    status = "Awaiting stock" if outcome == "unavailable" else (
        "Partially supplied" if remaining_total else "Completed"
    )
    db.execute("UPDATE collections SET status = ?, revision = revision + 1 WHERE id = ?", (status, collection_id))
    db.execute("""
        INSERT INTO collection_events(collection_id, revision, staff, outcome, quantities) VALUES (?, ?, ?, ?, ?)
    """, (collection_id, revision + 1, g.staff["username"], status, json.dumps(quantities)))
    code = collection["reference"]
    audit(collection["patient_id"], "collection_updated", f"{code} · {status}")
    return f"Collection {code} updated. {status}."


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", "5050")))
