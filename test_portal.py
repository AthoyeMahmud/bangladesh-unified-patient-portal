import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from server import create_app


class PortalTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "test.sqlite"
        self.app = create_app(self.db_path)
        self.app.testing = True
        self.staff = self.sign_in()

    def sql(self, query, params=()):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            return db.execute(query, params).fetchall()

    def post(self, client, path, follow=True, **fields):
        with client.session_transaction() as session:
            csrf = session["csrf"]
        return client.post(path, data={"csrf": csrf, **fields}, follow_redirects=follow)

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

    def test_identifiers_resolve_to_one_patient_and_bangla_digits_work(self):
        for method, identifier in [
            ("qr", "BUP-CARD-X6P2Q9"), ("barcode", "BUP-CARD-X6P2Q9"),
            ("identity", "DEMO-NID-১০০১"), ("pin", " bup-1001 "), ("fingerprint", "BUP-1001"),
        ]:
            with self.subTest(method=method):
                page = self.post(self.staff, "/lookup", method=method, identifier=identifier)
                self.assertIn(b"Amina Rahman", page.data)
                self.assertNotIn(b"Penicillin allergy", page.data)
        self.assertIn(b"Nusrat Akter", self.post(
            self.staff, "/lookup", method="identity", identifier="DEMO-BRN-1003",
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

    def test_stale_patient_form_and_unconfirmed_action_are_rejected(self):
        self.select_patient(confirm=False)
        self.assertEqual(self.service("appointment", slot=1).status_code, 403)
        self.post(self.staff, "/patient/1/confirm")
        self.select_patient(2)
        self.assertEqual(self.service("appointment", slot=1).status_code, 403)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM appointments"), [(0,)])

    def test_appointment_confirmation_idempotency_and_restart(self):
        self.select_patient()
        self.assertIn(b"Appointment APT-0001 booked", self.service("appointment", slot=1).data)
        self.assertIn(b"already booked", self.service("appointment", slot=1).data)
        self.assertEqual(self.sql("SELECT patient_id, slot_id FROM appointments"), [(1, 1)])
        new_client = self.sign_in(app=create_app(self.db_path))
        self.assertIn(b"APT-0001", self.select_patient(client=new_client).data)
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
        self.assertEqual(sum(b"APT-0001 booked" in data for data in results), 1)
        self.assertEqual(sum(b"appointment time is unavailable" in data for data in results), 1)

    def test_admission_checks_order_ward_and_duplicate_or_occupied_bed(self):
        self.select_patient(1)
        self.assertIn(b"Choose an admission order", self.service("admission", order=1, bed=1).data)
        self.select_patient(2)
        self.assertIn(b"unavailable for this order", self.service("admission", patient=2, order=1, bed=3).data)
        self.assertIn(b"Admission ADM-0001 saved", self.service("admission", patient=2, order=1, bed=1).data)
        self.assertIn(b"already saved", self.service("admission", patient=2, order=1, bed=1).data)
        self.assertIn(b"already has an admission", self.service("admission", patient=2, order=1, bed=2).data)
        self.assertIn(b"M-04", self.staff.get("/patient/2/confirmation/admission/1").data)
        self.sql("INSERT INTO admission_orders VALUES (2, 3, 'Dr Test', 'Medicine ward', '2026-11-01')")
        self.select_patient(3)
        self.assertIn(b"unavailable for this order", self.service("admission", patient=3, order=2, bed=1).data)
        self.assertEqual(self.sql("SELECT patient_id, bed_id FROM admissions"), [(2, 1)])

    def test_print_only_released_reports_for_confirmed_patient(self):
        self.select_patient()
        page = self.service("reports", report=["4", "5"])
        self.assertIn(b"Complete blood count", page.data)
        self.assertIn(b"Serum ferritin", page.data)
        self.assertIn(b"synthetic patient information", page.data)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        for suffix, status in [("ids=6", 404), ("ids=9", 404), ("ids=4&ids=garbage", 400), ("", 400)]:
            self.assertEqual(self.staff.get(f"/patient/1/reports/print?{suffix}").status_code, status)

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

    def test_scanner_assets_and_all_demo_cards_are_served_locally(self):
        for path in ["/assets/zxing.js", "/static/scanner.js", "/demo/cards",
                     "/static/cards/BUP-CARD-X6P2Q9-qr.svg", "/static/cards/BUP-CARD-X6P2Q9-barcode.svg"]:
            with self.subTest(path=path):
                response = self.staff.get(path)
                self.assertEqual(response.status_code, 200)
                response.close()


if __name__ == "__main__":
    unittest.main()
