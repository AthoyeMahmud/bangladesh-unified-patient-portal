# Bangladesh Unified Patient Portal

A hackathon demonstration for one hospital. All patient records, identity numbers, clinician orders, bookings, and stock outcomes are fictional.

## Start

From this directory, install the Python and browser dependencies once:

```sh
uv venv
uv pip install -r requirements.txt
npm ci
```

Run the portal and open <http://127.0.0.1:5050/>:

```sh
PATH="$PWD/.venv/bin:$PATH" npm start
```

Choose **Demo registration** or **Demo pharmacy** to sign in. Manual demo accounts are `registrar` and `pharmacy`. Both use `demo123`. The server binds to localhost. After installation, the demonstration uses local assets and does not need internet access.

## Walkthrough

1. Open **Demo registration**. Choose **QR code** or **Barcode**, then **Scan sample image** to decode a real sample. A printed card can also be read through the camera. In barcode mode a USB scanner can type the code into the focused field. The printable demo cards are available on the lookup screen.
2. Confirm **Amina Rahman**. Choose **Make appointment**, select a doctor and time, and open the saved appointment slip. Choose **Print test reports** to select one or more released reports. **Take medicine** sends the existing prescription to pharmacy.
3. Choose **Find another patient** and open **Karim Uddin**. Confirm the identity, select **Admission**, and save the active clinician order with an available bed. From the patient record, choose a discharge or transfer reason and confirm with the ward before releasing the bed.
4. Sign out and select **Demo pharmacy**. Open Amina's request under **Active requests**, confirm her identity, and save a partial supply in Bangla or ASCII digits. Choose **Mark remaining unavailable** with no pending quantities. Complete the supply later and find the receipt under **History**.
5. Select **Birth registration number (BRN)** and try **DEMO-BRN-1003**. PIN **BUP-1004** has sparse records. Try the three labelled fingerprint simulation outcomes. If no identity can be verified, use the hospital's manual registration process.
6. In registration, cancel a future appointment from **Saved services** to free its slot. The original slip records the cancellation. The demo permits one future booking per patient per department.

The default database is `.data/portal.sqlite`. Records survive restart. To rehearse with fresh data, use a new filename rather than replacing the current database:

```sh
PORTAL_DATABASE=.data/rehearsal.sqlite PORT=5051 PATH="$PWD/.venv/bin:$PATH" npm start
```

## Tests

```sh
PATH="$PWD/.venv/bin:$PATH" npm test
```

The tests use temporary SQLite files. They cover the original schema upgrade, stale forms and session revocation, released report ownership, races at two desks, interrupted saves, and backup recovery.

## Private rehearsal and recovery

Keep the SQLite file on persistent local storage. Use the hospital's HTTPS reverse proxy and trusted certificate, forward the original host, and configure that exact hostname. The Flask server listens on loopback behind the proxy.

```sh
PORTAL_HTTPS_HOST=portal.ward.example PORTAL_DATABASE=/var/lib/portal/rehearsal.sqlite \
  PATH="$PWD/.venv/bin:$PATH" gunicorn 'server:create_app()' --bind 127.0.0.1:5050 --workers 1 --threads 4
```

Before an upgrade, pause writes and use SQLite's backup command with new filenames. Check a restored copy before starting the upgraded app against it. The schema upgrade runs in one transaction at startup. The integrity check should return **ok** and the foreign key check should produce no rows.

```sh
sqlite3 .data/portal.sqlite ".backup '.data/before-hardening.sqlite'"
sqlite3 .data/before-hardening.sqlite ".backup '.data/rehearsal-restored.sqlite'"
sqlite3 .data/rehearsal-restored.sqlite 'PRAGMA integrity_check; PRAGMA foreign_key_check;'
```

During rehearsal, disconnect internet access while keeping the LAN available. Check lookup, printing, booking, and dispensing from two desks. For a server outage, use the facility's manual register. When the portal returns, look up each patient and compare saved services and dispensing history before entering pending work. Restoring an older copy requires reconciling entries made after that backup.

## Render deployment

Configure a Python web service with these commands. The Python build installs the server, and npm installs the scanner library.

```sh
pip install -r requirements.txt && npm ci --omit=dev
gunicorn 'server:create_app()' --bind "0.0.0.0:$PORT" --workers 1 --threads 4
```

Use Python 3.13.13 and Node.js 24 or newer. Render supplies the public hostname and port. The hosted demo uses shared, temporary SQLite storage. Bookings, admissions, and pharmacy activity can reset when the Free service spins down, restarts, or redeploys.

Use the portal with synthetic data only. The demo credentials and simulated identity services are available to anyone who can reach it.
