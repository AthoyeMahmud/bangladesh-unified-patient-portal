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
3. Choose **Find another patient** and open **Karim Uddin**. Confirm the identity, select **Admission**, and save the clinician's order with an available bed.
4. Sign out and select **Demo pharmacy**. Open Amina's request. Confirm her identity and record partial quantities. Try **Mark unavailable**, then supply the remaining quantities.
5. Try birth registration number `DEMO-BRN-1003`, PIN `BUP-1004` for sparse records, and the three labelled fingerprint simulation outcomes.

The default database is `.data/portal.sqlite`. Records survive restart. To rehearse with fresh data, use a new filename rather than replacing the current database:

```sh
PORTAL_DATABASE=.data/rehearsal.sqlite PORT=5051 PATH="$PWD/.venv/bin:$PATH" npm start
```

## Tests

```sh
PATH="$PWD/.venv/bin:$PATH" npm test
```

The tests create temporary databases and cover staff roles, identity confirmation, appointment conflicts, admission orders and beds, released report ownership, and medicine collection retries and limits.

## Render deployment

Configure a Python web service with these commands. The Python build installs the server, and npm installs the scanner library.

```sh
pip install -r requirements.txt && npm ci --omit=dev
gunicorn 'server:create_app()' --bind "0.0.0.0:$PORT" --workers 1 --threads 4
```

Use Python 3.13.13 and Node.js 24 or newer. Render supplies the public hostname and port. The hosted demo uses shared, temporary SQLite storage. Bookings, admissions, and pharmacy activity can reset when the Free service spins down, restarts, or redeploys.

Use the portal with synthetic data only. The demo credentials and simulated identity services are available to anyone who can reach it.
