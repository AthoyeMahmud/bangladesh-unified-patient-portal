import json
import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from demo import DHAKA, PATIENTS
from server import create_app


class PortalTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "test.sqlite"
        self.app = create_app(self.db_path)
        self.app.testing = True
        self.staff = self.sign_in()

    def sql(self, query, params=(), *, database=None):
        with closing(sqlite3.connect(database or self.db_path)) as db, db:
            return db.execute(query, params).fetchall()

    def post(self, client, path, follow=True, **fields):
        with client.session_transaction() as session:
            csrf = session["csrf"]
        if path.startswith("/patient/") or path == "/lookup/reset":
            fields.setdefault("selection", self.selection(client))
        if path.endswith("/service/appointment"):
            fields.setdefault("request_key", uuid.uuid4().hex)
        return client.post(path, data={"csrf": csrf, **fields}, follow_redirects=follow)

    def selection(self, client):
        with client.session_transaction() as session:
            session_id = session.get("staff_session")
        if not session_id:
            return ""
        return self.sql("SELECT selection FROM staff_sessions WHERE id=?", (session_id,),
                        database=client.application.config["DATABASE"])[0][0]

    def sign_in(self, username="registrar", app=None):
        client = (app or self.app).test_client()
        client.get("/")
        self.assertEqual(self.post(client, "/login", username=username, password="demo123").status_code, 200)
        return client

    def select_patient(self, patient=1, *, client=None, confirm=True):
        client = client or self.staff
        result = self.post(client, "/lookup", method="pin", identifier=f"BUP-100{patient}")
        self.assertIn(b"Confirm patient", result.data)
        if confirm:
            result = self.post(client, f"/patient/{patient}/confirm")
            self.assertIn(b"Identity confirmed", result.data)
        return result

    def service(self, action, patient=1, client=None, **fields):
        return self.post(client or self.staff, f"/patient/{patient}/service/{action}", **fields)

    def request_medicine(self):
        self.select_patient()
        self.assertIn(b"sent to pharmacy", self.service("medicine", prescription=1).data)
        return self.sign_in("pharmacy")

    def supply(self, pharmacy, revision=0, first="4", second="2", **extra):
        return self.post(pharmacy, "/pharmacy/1", **{
            "outcome": "supply", "identity": "confirmed", "revision": revision,
            "quantity_1": first, "quantity_2": second, **extra,
        })

    def test_login_session_and_csrf(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/lookup").status_code, 403)
        anonymous.get("/")
        self.assertEqual(anonymous.post("/login", data={"csrf": "invalid"}).status_code, 400)
        bad = self.post(anonymous, "/login", username="registrar", password="wrong")
        self.assertIn(b"Staff ID or password is incorrect", bad.data)
        self.assertIn(b"Sign out", self.staff.get("/lookup").data)
        self.post(self.staff, "/logout")
        self.assertEqual(self.staff.get("/lookup").status_code, 403)
        self.assertNotIn("demo123", self.sql("SELECT password_hash FROM staff")[0][0])

    def test_hosted_hostname_accepts_only_expected_hosts_and_secures_cookie(self):
        with patch.dict("os.environ", {"RENDER_EXTERNAL_HOSTNAME": "portal-check.onrender.com"}):
            hosted = create_app(self.db_path).test_client()
        response = hosted.get("/", base_url="https://portal-check.onrender.com")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Secure", response.headers["Set-Cookie"])
        self.assertEqual(hosted.get("/", base_url="https://another.onrender.com").status_code, 400)

    def test_private_https_hostname_is_allowlisted_without_trusting_subdomains(self):
        with patch.dict("os.environ", {"PORTAL_HTTPS_HOST": "portal.ward.test"}):
            client = create_app(self.db_path).test_client()
        response = client.get("/", base_url="https://portal.ward.test")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Secure", response.headers["Set-Cookie"])
        self.assertEqual(client.get("/", base_url="https://other.portal.ward.test").status_code, 400)

    def test_identifiers_resolve_to_one_patient_and_bangla_digits_work(self):
        for method, identifier in [
            ("qr", "BUP-CARD-X6P2Q9"), ("barcode", "BUP-CARD-X6P2Q9"),
            ("identity", "DEMO-NID-১০০১"), ("pin", " bup-1001 "), ("fingerprint", "BUP-1001"),
        ]:
            with self.subTest(method=method):
                page = self.post(self.staff, "/lookup", method=method, identifier=identifier, identity_type="nid")
                self.assertIn(b"Amina Rahman", page.data)
                self.assertNotIn(b"Penicillin allergy", page.data)
        self.assertIn(b"Nusrat Akter", self.post(
            self.staff, "/lookup", method="identity", identity_type="brn", identifier="DEMO-BRN-1003",
        ).data)
        self.assertIn(b"Karim Uddin", self.post(
            self.staff, "/lookup?method=qr", method="pin", identifier="BUP-1002",
        ).data)

    def test_no_match_failure_and_rejected_patient_cannot_be_confirmed(self):
        self.select_patient(confirm=False)
        self.post(self.staff, "/lookup/reset")
        self.assertEqual(self.post(self.staff, "/patient/1/confirm").status_code, 403)
        for identifier, expected in [("FAILED", b"simulated scan failed"), ("NO-MATCH", b"No matching patient")]:
            page = self.post(self.staff, "/lookup", method="fingerprint", identifier=identifier)
            self.assertIn(expected, page.data)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM patients"), [(4,)])

    def test_national_id_type_disambiguates_and_blank_never_matches(self):
        identifier = "00123456789012345"
        self.sql("UPDATE patients SET nid=? WHERE id=1", (identifier,))
        self.sql("UPDATE patients SET brn=? WHERE id=3", (identifier,))
        self.sql("UPDATE patients SET nid='' WHERE id=4")
        untyped = self.post(self.staff, "/lookup", follow=False, method="identity", identifier=identifier)
        self.assertEqual(untyped.location, "/lookup?method=identity")
        for id_type, patient_id in [("nid", 1), ("brn", 3)]:
            with self.subTest(id_type=id_type):
                typed = self.post(
                    self.staff, "/lookup", follow=False, method="identity",
                    identity_type=id_type, identifier=" ০০১২৩৪৫৬৭৮৯ ০১২৩৪৫ ",
                )
                self.assertEqual(typed.location, f"/patient/{patient_id}")
        blank = self.post(self.staff, "/lookup", follow=False, method="identity", identity_type="nid", identifier=" \u00a0 ")
        self.assertEqual(blank.location, "/lookup?method=identity")
        self.assertEqual(self.staff.get("/patient/3").status_code, 403)

    def test_incomplete_birth_date_requires_manual_verification(self):
        self.sql("UPDATE patients SET birth_date='1990' WHERE id=1")
        with patch.dict(self.app.config, {"PROPAGATE_EXCEPTIONS": False}), patch.object(self.app.logger, "disabled", True):
            page = self.post(self.staff, "/lookup", method="pin", identifier="BUP-1001")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Manual verification required", page.data)
        self.assertNotIn(b"Penicillin allergy", page.data)
        self.assertEqual(self.post(self.staff, "/patient/1/confirm").status_code, 403)
        self.assertEqual(self.staff.get("/patient/1/service/medicine").status_code, 403)

    def test_stale_patient_form_and_unconfirmed_action_are_rejected(self):
        self.select_patient(confirm=False)
        self.assertEqual(self.service("appointment", slot=1).status_code, 403)
        self.post(self.staff, "/patient/1/confirm")
        self.select_patient(2)
        self.assertEqual(self.service("appointment", slot=1).status_code, 403)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM appointments"), [(0,)])

    def test_returning_to_same_patient_does_not_revive_an_old_form(self):
        self.select_patient(1)
        stale = self.selection(self.staff)
        self.select_patient(2)
        self.select_patient(1)
        response = self.service("appointment", slot=1, selection=stale)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM appointments"), [(0,)])
        self.post(self.staff, "/lookup", method="pin", identifier="unknown")
        self.assertEqual(self.staff.get("/patient/1").status_code, 403)

    def test_logout_revokes_a_copied_cookie(self):
        self.select_patient()
        old = self.staff.get_cookie("session")
        self.post(self.staff, "/logout")
        replay = self.app.test_client()
        replay.set_cookie("session", old.value)
        self.assertEqual(replay.get("/patient/1/reports/print?ids=4").status_code, 403)

    def test_booking_overlap_and_department_limit(self):
        self.select_patient()
        starts = self.sql("SELECT starts FROM slots WHERE id=1")[0][0]
        self.sql("INSERT INTO slots VALUES (80, 'Surgery', 'Dr Test', ?)", (starts,))
        self.assertIn(b"booked.", self.service("appointment", slot=1).data)
        self.assertIn(b"already has an appointment at that time", self.service("appointment", slot=80).data)
        self.assertIn(b"already has a future appointment in this department", self.service("appointment", slot=2).data)
        self.assertEqual(self.sql("SELECT patient_id, slot_id FROM appointments"), [(1, 1)])

    def test_cancel_frees_slot_and_a_lost_response_retry_cannot_reopen_it(self):
        self.select_patient()
        request_key = uuid.uuid4().hex
        other_key = uuid.uuid4().hex
        with self.staff.session_transaction() as session:
            csrf = session["csrf"]
        cookie = self.staff.get_cookie("session").value
        lost = self.app.test_client(use_cookies=False).post(
            "/patient/1/service/appointment",
            data={"csrf": csrf, "selection": self.selection(self.staff), "slot": 1, "request_key": request_key},
            headers={"Cookie": f"session={cookie}"},
        )
        self.assertEqual(lost.status_code, 302)
        self.assertIn(b"already booked", self.service("appointment", slot=1, request_key=request_key).data)
        self.assertIn(b"already booked", self.service("appointment", slot=1, request_key=other_key).data)
        cancelled = self.post(self.staff, "/patient/1/appointments/1/cancel")
        self.assertIn(b"cancelled.", cancelled.data)
        self.assertIn(b"Cancelled", self.staff.get("/patient/1/confirmation/appointment/1").data)
        for key in [request_key, other_key]:
            self.assertIn(b"was cancelled", self.service("appointment", slot=1, request_key=key).data)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM appointments"), [(1,)])
        self.select_patient(2)
        self.assertIn(b"booked.", self.service("appointment", patient=2, slot=1).data)
        self.assertEqual(self.sql("SELECT patient_id FROM appointments ORDER BY id"), [(1,), (2,)])

    def test_demo_schedule_rolls_forward_without_restart(self):
        self.select_patient()
        later = (datetime.now(DHAKA) + timedelta(days=7)).strftime("%Y-%m-%d 08:00")
        with patch("server.local_minute", return_value=later):
            page = self.staff.get("/patient/1/service/appointment")
        self.assertIn(b"Book appointment", page.data)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM slots WHERE starts > ?", (later,)), [(16,)])

    def test_appointment_confirmation_idempotency_and_restart(self):
        self.select_patient()
        booked = self.service("appointment", slot=1)
        reference = self.sql("SELECT reference FROM appointments")[0][0].encode()
        self.assertIn(b"Appointment " + reference + b" booked", booked.data)
        self.assertIn(b"already booked", self.service("appointment", slot=1).data)
        self.assertEqual(self.sql("SELECT patient_id, slot_id FROM appointments"), [(1, 1)])
        new_client = self.sign_in(app=create_app(self.db_path))
        self.assertIn(reference, self.select_patient(client=new_client).data)
        print_view = new_client.get("/patient/1/confirmation/appointment/1")
        self.assertIn(b"Dr Farhana Islam", print_view.data)
        self.assertIn(b"(Dhaka)", print_view.data)

    def test_two_staff_cannot_book_the_same_slot(self):
        other = self.sign_in()
        self.select_patient(1)
        self.select_patient(2, client=other)
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(self.service, "appointment", patient=1, client=self.staff, slot=1)
            two = pool.submit(self.service, "appointment", patient=2, client=other, slot=1)
            results = [one.result().data, two.result().data]
        self.assertEqual(sum(b" booked." in data for data in results), 1)
        self.assertEqual(sum(b"appointment time is unavailable" in data for data in results), 1)

    def test_busy_chart_and_booking_return_retryable_503_without_saving(self):
        other = self.sign_in()
        self.select_patient()
        self.select_patient(2, client=other)
        with closing(sqlite3.connect(self.db_path)) as blocker, blocker:
            blocker.execute("BEGIN IMMEDIATE")
            with patch.dict(self.app.config, {"PROPAGATE_EXCEPTIONS": False}), patch.object(self.app.logger, "disabled", True):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    chart = pool.submit(self.staff.get, "/patient/1")
                    booking = pool.submit(self.service, "appointment", patient=2, client=other, slot=1)
                    responses = [chart.result(), booking.result()]
        for response in responses:
            self.assertEqual(response.status_code, 503)
            self.assertIn(b"check the saved services or collection history before retrying", response.data)
            self.assertIn("Retry-After", response.headers)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM appointments"), [(0,)])
        self.assertIn(b"booked.", self.service("appointment", slot=1).data)

    def test_restore_keeps_saved_receipts_and_does_not_reuse_lost_reference(self):
        self.select_patient()
        self.assertIn(b"booked.", self.service("appointment", slot=1).data)
        backup = self.db_path.with_name("restored.sqlite")
        with closing(sqlite3.connect(self.db_path)) as original, closing(sqlite3.connect(backup)) as restored:
            original.backup(restored)
        self.assertIn(b"booked.", self.service("appointment", slot=3).data)
        kept, lost = self.sql("SELECT id,reference FROM appointments ORDER BY id")
        restored_client = self.sign_in(app=create_app(backup))
        self.assertIn(kept[1].encode(), self.select_patient(client=restored_client).data)
        self.select_patient(2, client=restored_client)
        self.assertIn(b"booked.", self.service("appointment", patient=2, client=restored_client, slot=3).data)
        new_id, new_reference = self.sql("SELECT id,reference FROM appointments WHERE patient_id=2", database=backup)[0]
        self.assertEqual(new_id, lost[0])
        self.assertNotEqual(new_reference, lost[1])

    def test_admission_checks_order_ward_and_duplicate_or_occupied_bed(self):
        self.select_patient(1)
        self.assertIn(b"Choose an admission order", self.service("admission", order=1, bed=1).data)
        self.select_patient(2)
        self.assertIn(b"unavailable for this order", self.service("admission", patient=2, order=1, bed=3).data)
        self.assertRegex(self.service("admission", patient=2, order=1, bed=1).data, rb"Admission ADM-[0-9A-F]+ saved")
        self.assertIn(b"already saved", self.service("admission", patient=2, order=1, bed=1).data)
        self.assertIn(b"already has an admission", self.service("admission", patient=2, order=1, bed=2).data)
        self.assertIn(b"M-04", self.staff.get("/patient/2/confirmation/admission/1").data)
        self.sql("""
            INSERT INTO admission_orders(id,patient_id,clinician,ward,on_date,active)
            VALUES (2,3,'Dr Test','Medicine ward','2026-11-01',1)
        """)
        self.select_patient(3)
        self.assertIn(b"unavailable for this order", self.service("admission", patient=3, order=2, bed=1).data)
        self.assertEqual(self.sql("SELECT patient_id, bed_id FROM admissions"), [(2, 1)])

    def test_inactive_legacy_order_and_prescription_cannot_start_services(self):
        self.sql("""
            INSERT INTO admission_orders (id,patient_id,clinician,ward,on_date)
            VALUES (2,1,'Dr Test','Medicine ward','2019-01-01')
        """)
        self.sql("INSERT INTO prescriptions (id,patient_id,clinician,on_date) VALUES (2,1,'Dr Test','2019-01-01')")
        self.sql("INSERT INTO prescription_items VALUES (3,2,'Test item',10,0)")
        self.select_patient()
        self.assertIn(b"active admission order", self.service("admission", order=2, bed=1).data)
        self.assertIn(b"active prescription", self.service("medicine", prescription=2).data)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM admissions"), [(0,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM collections"), [(0,)])

    def test_released_bed_is_reassigned_and_readmission_requires_new_order(self):
        self.select_patient(2)
        self.assertIn(b"saved.", self.service("admission", patient=2, order=1, bed=1).data)
        url = "/patient/2/admissions/1/release"
        self.assertIn(b"ward", self.post(self.staff, url, reason="discharge").data)
        page = self.post(self.staff, url, reason="discharge", ward_confirmed="confirmed")
        self.assertIn(b"Bed released", page.data)
        self.assertIn(b"Discharged", self.staff.get("/patient/2/confirmation/admission/1").data)
        self.assertIn(b"already released", self.service("admission", patient=2, order=1, bed=1).data)
        for order_id, patient_id, bed_id in [(2, 3, 1), (3, 2, 2)]:
            self.sql("""
                INSERT INTO admission_orders(id,patient_id,clinician,ward,on_date,active)
                VALUES (?,?,'Dr Test','Medicine ward','2026-01-01',1)
            """, (order_id, patient_id))
            self.select_patient(patient_id)
            self.assertIn(b"saved.", self.service("admission", patient=patient_id, order=order_id, bed=bed_id).data)
        self.assertEqual(self.sql("SELECT patient_id, bed_id, released_at IS NULL FROM admissions ORDER BY id"),
                         [(2, 1, 0), (3, 1, 1), (2, 2, 1)])

    def test_two_desks_competing_for_last_bed_save_one_admission(self):
        self.sql("""
            INSERT INTO admission_orders(id,patient_id,clinician,ward,on_date,active)
            VALUES (2,3,'Dr Test','Medicine ward','2026-01-01',1)
        """)
        other = self.sign_in()
        self.select_patient(2)
        self.select_patient(3, client=other)
        barrier = Barrier(2, timeout=5)

        def admit(client, patient, order):
            barrier.wait()
            return self.service("admission", patient=patient, client=client, order=order, bed=1).data

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(admit, self.staff, 2, 1)
            second = pool.submit(admit, other, 3, 2)
            results = [first.result(), second.result()]
        self.assertEqual(sum(b" saved." in response for response in results), 1)
        self.assertEqual(sum(b"bed is unavailable" in response for response in results), 1)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM admissions WHERE bed_id=1"), [(1,)])

    def test_print_only_released_reports_for_confirmed_patient(self):
        self.select_patient()
        page = self.service("reports", report=["4", "5"])
        self.assertIn(b"Complete blood count", page.data)
        self.assertIn(b"Serum ferritin", page.data)
        self.assertIn(b"synthetic patient information", page.data)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        for suffix, status in [("ids=6", 404), ("ids=9", 404), ("ids=4&ids=garbage", 400), ("", 400)]:
            self.assertEqual(self.staff.get(f"/patient/1/reports/print?{suffix}").status_code, status)

    def test_reports_default_to_draft_and_withdrawn_reports_stop_printing(self):
        self.sql("""
            INSERT INTO records(id, patient_id, kind, title, on_date, source, details)
            VALUES (80, 1, 'Report', 'Unreviewed result', '2026-01-01', 'Test lab', 'Draft content')
        """)
        self.assertNotIn(b"Unreviewed result", self.select_patient().data)
        self.assertEqual(self.staff.get("/patient/1/reports/print?ids=80").status_code, 404)
        self.assertEqual(self.staff.get("/patient/1/reports/print?ids=4").status_code, 200)
        self.sql("UPDATE records SET released=0 WHERE id=4")
        self.assertEqual(self.staff.get("/patient/1/reports/print?ids=4").status_code, 404)

    def test_oversized_report_selection_returns_to_form(self):
        self.select_patient()
        page = self.service("reports", report=[str(i) for i in range(21, 42)])
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Select up to 20 reports", page.data)

    def test_missing_history_has_explicit_empty_states(self):
        page = self.select_patient(4)
        for message in [
            b"No conditions or allergies recorded", b"No previous visits or admissions available",
            b"No released reports available", b"No prescriptions available",
        ]:
            self.assertIn(message, page.data)

    def test_invalid_database_identifiers_return_normal_errors(self):
        oversized = str(2**80)
        self.select_patient()
        self.assertIn(b"appointment time is unavailable", self.service("appointment", slot=oversized).data)
        self.assertIn(b"existing prescription", self.service("medicine", prescription=oversized).data)
        self.assertEqual(self.staff.get(f"/patient/1/confirmation/appointment/{oversized}").status_code, 404)
        self.assertEqual(self.sign_in("pharmacy").get(f"/pharmacy/{oversized}").status_code, 404)

    def test_pharmacy_roles_partial_unavailable_and_completed(self):
        pharmacy = self.request_medicine()
        self.assertEqual(self.service("medicine", prescription=1).status_code, 200)
        self.assertEqual(self.staff.get("/pharmacy/1").status_code, 403)
        self.assertEqual(pharmacy.get("/patient/1").status_code, 403)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM collections"), [(1,)])
        self.assertIn(b"Partially supplied", self.supply(pharmacy).data)
        self.assertEqual(self.sql("SELECT supplied FROM prescription_items ORDER BY id"), [(4,), (2,)])
        page = self.post(pharmacy, "/pharmacy/1", revision=1, outcome="unavailable", identity="confirmed")
        self.assertIn(b"Awaiting stock", page.data)
        self.assertIn(b"4 supplied", page.data)
        self.assertIn(b"Completed", self.supply(pharmacy, revision=2, first="10", second="5").data)
        self.assertEqual(self.sql("SELECT revision, status FROM collections"), [(3, "Completed")])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM collection_events"), [(3,)])

    def test_pharmacy_rolls_back_bad_lines_and_rejects_stale_submissions(self):
        pharmacy = self.request_medicine()
        self.assertIn(b"Confirm the patient", self.supply(pharmacy, identity="").data)
        self.assertIn(b"Enter a quantity between", self.supply(pharmacy, first="4", second="8").data)
        self.assertIn(b"Enter a quantity between", self.supply(pharmacy, first="4", second="x").data)
        self.assertEqual(self.sql("SELECT supplied FROM prescription_items ORDER BY id"), [(0,), (0,)])
        self.assertIn(b"Partially supplied", self.supply(pharmacy).data)
        self.assertIn(b"This request has changed", self.supply(pharmacy).data)
        self.assertEqual(self.sql("SELECT revision FROM collections"), [(1,)])
        self.assertEqual(self.sql("SELECT supplied FROM prescription_items ORDER BY id"), [(4,), (2,)])
        self.assertEqual(self.sql("""
            SELECT staff, patient_id FROM audit WHERE action = 'collection_updated'
        """), [("pharmacy", 1)])

    def test_stockout_keeps_pending_bangla_quantities_without_saving(self):
        pharmacy = self.request_medicine()
        page = self.supply(pharmacy, first="৪", second="০", outcome="unavailable")
        self.assertEqual(page.status_code, 400)
        self.assertIn(b"Save the entered quantities first", page.data)
        self.assertIn('value="৪"'.encode(), page.data)
        self.assertEqual(self.sql("SELECT supplied FROM prescription_items ORDER BY id"), [(0,), (0,)])
        self.assertEqual(self.sql("SELECT revision, status FROM collections"), [(0, "Requested")])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM collection_events"), [(0,)])

    def test_bangla_supply_keeps_separate_history_for_duplicate_labels(self):
        self.sql("UPDATE prescription_items SET medicine='Same label'")
        pharmacy = self.request_medicine()
        page = self.supply(pharmacy, first="৪", second="২")
        self.assertIn(b"Partially supplied", page.data)
        self.assertEqual(self.sql("SELECT supplied FROM prescription_items ORDER BY id"), [(4,), (2,)])
        entries = json.loads(self.sql("SELECT quantities FROM collection_events")[0][0])
        self.assertEqual([(entry["item_id"], entry["quantity"]) for entry in entries], [(1, 4), (2, 2)])
        self.assertIn(b"Line 1", page.data)
        self.assertIn(b"Line 2", page.data)

    def test_withdrawn_prescription_cannot_be_dispensed_from_an_open_form(self):
        pharmacy = self.request_medicine()
        self.assertIn(b"Save supplied quantities", pharmacy.get("/pharmacy/1").data)
        self.sql("UPDATE prescriptions SET active=0 WHERE id=1")
        self.assertIn(b"Prescription inactive", self.supply(pharmacy).data)
        self.assertEqual(self.sql("SELECT supplied FROM prescription_items ORDER BY id"), [(0,), (0,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM collection_events"), [(0,)])

    def test_two_pharmacy_desks_cannot_dispense_same_revision_twice(self):
        first = self.request_medicine()
        second = self.sign_in("pharmacy")
        barrier = Barrier(2, timeout=5)

        def dispense(client):
            barrier.wait()
            return self.supply(client).data

        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(dispense, first)
            b = pool.submit(dispense, second)
            responses = [a.result(), b.result()]
        self.assertEqual(sum(b"updated. Partially supplied" in response for response in responses), 1)
        self.assertEqual(sum(b"This request has changed" in response for response in responses), 1)
        self.assertEqual(self.sql("SELECT supplied FROM prescription_items ORDER BY id"), [(4,), (2,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM collection_events"), [(1,)])

    def test_queue_pages_are_bounded_sorted_and_audited_once(self):
        pharmacy = self.sign_in("pharmacy")
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.executemany("""
                INSERT INTO prescriptions(id,patient_id,clinician,on_date,active)
                VALUES (?,1,'Dr Test','2026-01-01',?)
            """, [(i, int(i != 1001)) for i in range(2, 1002)])
            db.executemany("""
                INSERT INTO collections(id,prescription_id,reference,status) VALUES (?,?,?,?)
            """, [(i, i, f"TEST-{i}", "Requested" if i < 302 or i == 1001 else "Completed") for i in range(2, 1002)])
        start = self.sql("SELECT COUNT(*) FROM audit")[0][0]
        active = pharmacy.get("/pharmacy")
        self.assertEqual(active.data.count(b'class="queue-row"'), 25)
        self.assertLess(active.data.index(b'href="/pharmacy/2"'), active.data.index(b'href="/pharmacy/26"'))
        self.assertNotIn(b'href="/pharmacy/27"', active.data)
        second = pharmacy.get("/pharmacy?page=2")
        self.assertIn(b'href="/pharmacy/27"', second.data)
        history = pharmacy.get("/pharmacy?view=history")
        self.assertIn(b"Prescription inactive", history.data)
        self.assertNotIn(b'href="/pharmacy/2"', history.data)
        pharmacy.get("/pharmacy")
        logs = self.sql("SELECT action,detail FROM audit WHERE id > ? ORDER BY id", (start,))
        self.assertEqual([action for action, _ in logs], ["collection_list_viewed"] * 4)
        self.assertEqual(json.loads(logs[0][1])["ids"], list(range(2, 27)))
        self.assertEqual(pharmacy.get("/pharmacy?page=999999999999999999999").status_code, 400)

    def test_scanner_assets_and_all_demo_cards_are_served_locally(self):
        for path in ["/assets/zxing.js", "/static/scanner.js", "/demo/cards",
                     "/static/cards/BUP-CARD-X6P2Q9-qr.svg", "/static/cards/BUP-CARD-X6P2Q9-barcode.svg"]:
            with self.subTest(path=path):
                response = self.staff.get(path)
                self.assertEqual(response.status_code, 200)
                response.close()


class MigrationTest(unittest.TestCase):
    def test_v0_upgrade_preserves_receipts_audits_and_clinical_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite"
            with closing(sqlite3.connect(path)) as old, old:
                old.executescript(Path(__file__).with_name("testdata").joinpath("schema_v0.sql").read_text())
                old.executemany("INSERT INTO patients VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", PATIENTS)
                old.execute("INSERT INTO staff VALUES ('pharmacy','Pharmacy','unused','pharmacy')")
                old.executescript("""
                    INSERT INTO records VALUES (4,1,'Report','Released','2026-01-01','Lab','Result',1),
                        (6,1,'Report','Draft','2026-01-01','Lab','Draft',0);
                    INSERT INTO slots VALUES (1,'Medicine','Dr Test','2099-01-01 09:00');
                    INSERT INTO appointments(id,patient_id,slot_id) VALUES (35,1,1);
                    INSERT INTO beds VALUES (1,'Medicine ward','M-04');
                    INSERT INTO admission_orders VALUES (1,2,'Dr Farhana Islam','Medicine ward','2026-01-01'),
                        (2,1,'External clinician','Medicine ward','2019-01-01');
                    INSERT INTO admissions(id,patient_id,order_id,bed_id) VALUES (23,2,1,1);
                    INSERT INTO prescriptions VALUES (1,1,'Dr Farhana Islam','2026-01-01'), (2,2,'External clinician','2019-01-01');
                    INSERT INTO prescription_items VALUES (1,1,'Ferrous sulfate tablets',14,4), (2,1,'Folic acid tablets',7,2);
                    INSERT INTO collections(id,prescription_id,status,revision) VALUES (9,1,'Partially supplied',1);
                    INSERT INTO collection_events(id,collection_id,revision,staff,outcome,quantities)
                        VALUES (8,9,1,'pharmacy','Partially supplied','{"Ferrous sulfate tablets":4,"Folic acid tablets":2}');
                    INSERT INTO audit(id,staff,patient_id,action,detail)
                        VALUES (6,'pharmacy',1,'collection_updated','MED-0009');
                """)
            create_app(path)
            create_app(path)
            with closing(sqlite3.connect(path)) as upgraded, upgraded:
                self.assertEqual(upgraded.execute("PRAGMA user_version").fetchone(), (1,))
                self.assertEqual(upgraded.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(upgraded.execute("SELECT id,released FROM records ORDER BY id").fetchall(), [(4, 1), (6, 0)])
                self.assertEqual(upgraded.execute("SELECT reference,cancelled_at FROM appointments").fetchone(), ("APT-0035", None))
                self.assertEqual(upgraded.execute("SELECT reference,bed_id,released_at FROM admissions").fetchone(), ("ADM-0023", 1, None))
                self.assertEqual(upgraded.execute("SELECT reference,revision,status FROM collections").fetchone(),
                                 ("MED-0009", 1, "Partially supplied"))
                self.assertEqual(upgraded.execute("SELECT supplied FROM prescription_items ORDER BY id").fetchall(), [(4,), (2,)])
                self.assertEqual(upgraded.execute("SELECT id,collection_id,quantities FROM collection_events").fetchone(),
                                 (8, 9, '{"Ferrous sulfate tablets":4,"Folic acid tablets":2}'))
                self.assertEqual(upgraded.execute("SELECT id,detail FROM audit").fetchone(), (6, "MED-0009"))
                for table in ["admission_orders", "prescriptions"]:
                    self.assertEqual(upgraded.execute(f"SELECT id,active FROM {table} ORDER BY id").fetchall(), [(1, 1), (2, 0)])
                upgraded.execute("""
                    INSERT INTO records (id,patient_id,kind,title,on_date,source,details)
                    VALUES (80,1,'Report','New draft','2026-01-01','Lab','Draft content')
                """)
                self.assertEqual(upgraded.execute("SELECT released FROM records WHERE id=80").fetchone(), (0,))

if __name__ == "__main__":
    unittest.main()
