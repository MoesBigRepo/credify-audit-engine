"""FTC Fast Filer — production Playwright automation for identitytheft.gov.

~25-30s per filing. Features: parallel TV auth + browser launch, asset caching,
JS batch fill, ProtonVPN rotation on SMS timeout (up to 5 retries), smart waits.

Usage:
    python3 ftc_fast_filer.py --client client.json --audit-json audit.json

The run always goes all the way through to the FTC download page (manual PDF download click).

Requires: TEXTVERIFIED_API_KEY and TEXTVERIFIED_USERNAME env vars (see ~/.zshenv).
"""
import requests, time, sys, os, json, random, string, re, subprocess, argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from playwright.sync_api import sync_playwright
from pathlib import Path

CHROMIUM_PROFILE = Path.home() / ".credify_chromium_profile"


def _pin_save_as_pdf():
    """Pre-seed Chromium Preferences so print preview opens on 'Save as PDF'
    regardless of the system default printer. AppleScript's Enter then hits
    Save instead of Print on a CUPS printer."""
    d = CHROMIUM_PROFILE / "Default"
    d.mkdir(parents=True, exist_ok=True)
    app_state = json.dumps({
        "version": 2,
        "recentDestinations": [{"id": "Save as PDF", "origin": "local", "account": ""}],
    })
    (d / "Preferences").write_text(json.dumps({
        "printing": {"print_preview_sticky_settings": {"appState": app_state}}
    }))


TV_BASE = "https://www.textverified.com"
API_KEY = os.environ.get("TEXTVERIFIED_API_KEY")
USERNAME = os.environ.get("TEXTVERIFIED_USERNAME")
if not API_KEY or not USERNAME:
    print("Error: TEXTVERIFIED_API_KEY and TEXTVERIFIED_USERNAME must be set (see ~/.zshenv)", file=sys.stderr)
    sys.exit(1)

MAX_VPN_ROTATIONS = 5
ASSET_CACHE_DIR = Path("/tmp/ftc_asset_cache")

# === CLI ===
_parser = argparse.ArgumentParser(description="FTC fast filer for identitytheft.gov")
_parser.add_argument("--client", required=True, help="client.json path (first_name, last_name, dob, email, address, city, state, zip)")
_parser.add_argument("--audit-json", required=True, help="Credify audit JSON path (reads raw_accounts where is_derogatory)")
_parser.add_argument("--offset", type=int, default=0, help="Skip the first N derogs (for multi-batch filings)")
_parser.add_argument("--limit", type=int, default=5, help="Max accounts per report (FTC caps at 5)")
args = _parser.parse_args()

with open(os.path.expanduser(args.client)) as _f:
    CLIENT = json.load(_f)
with open(os.path.expanduser(args.audit_json)) as _f:
    _audit = json.load(_f)

for _req in ("first_name", "last_name", "dob", "email", "address", "city", "state", "zip"):
    if not CLIENT.get(_req):
        print(f"Error: client.json missing required field: {_req}", file=sys.stderr); sys.exit(1)

# STATE_NAMES must be defined before pre-flight check
STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}

# === PRE-FLIGHT: address validation gate (runs BEFORE TextVerified rental — zero credits burned on failure) ===
_preflight_errors = []
if CLIENT["state"] not in STATE_NAMES:
    _preflight_errors.append(f"state '{CLIENT['state']}' not in STATE_NAMES (must be a 2-letter abbreviation like NY, FL, CA)")
if len(CLIENT["address"].strip()) < 3:
    _preflight_errors.append(f"address too short: '{CLIENT['address']}' (need a real street address)")
if len(CLIENT["city"].strip()) < 2:
    _preflight_errors.append(f"city too short: '{CLIENT['city']}'")
if not CLIENT["zip"].strip() or len(CLIENT["zip"].strip()) < 5:
    _preflight_errors.append(f"zip invalid: '{CLIENT['zip']}' (need 5-digit ZIP)")
if _preflight_errors:
    print("ABORT: address pre-flight check failed (no TV credits burned):", file=sys.stderr)
    for e in _preflight_errors:
        print(f"  - {e}", file=sys.stderr)
    print(f"\nFix client.json at: {os.path.expanduser(args.client)}", file=sys.stderr)
    sys.exit(1)
print(f"[PREFLIGHT] Address OK: {CLIENT['address']}, {CLIENT['city']}, {STATE_NAMES[CLIENT['state']]} {CLIENT['zip']}", file=sys.stderr)

BATCH_INDEX = os.environ.get("FTC_BATCH_INDEX", "")

def normalize_account_number(num):
    """Replace asterisks with X's. Always use X for masked digits."""
    if not num: return ""
    return num.replace("*", "X")

# Pull derogatory accounts from audit JSON, apply offset/limit slice.
_derog_all = [a for a in _audit.get("raw_accounts", []) if a.get("is_derogatory")]
if not _derog_all:
    print("Error: no derogatory accounts in audit JSON (raw_accounts with is_derogatory=true)", file=sys.stderr)
    sys.exit(1)

_derog = _derog_all[args.offset : args.offset + min(args.limit, 5)]
if not _derog:
    print(f"Error: offset {args.offset} past end of {len(_derog_all)} derogs — nothing to file", file=sys.stderr)
    sys.exit(1)

print(f"[SLICE] Filing derogs {args.offset + 1}-{args.offset + len(_derog)} of {len(_derog_all)}", file=sys.stderr)

ACCOUNTS = [
    {
        "name": re.sub(r'[/\-&]+', ' ', (a.get("creditor") or a.get("name") or "UNKNOWN")).upper().strip(),
        "number": normalize_account_number(str(a.get("account_number") or a.get("number") or "")),
        "opened": a.get("opened", ""),  # e.g. "08/2016" — MM/YYYY from credit report
        "balance": str(a.get("balance", 0)).replace("$", "").replace(",", "") or "0",
    }
    for a in _derog
]

STATEMENT = ("I AM A VICTIM OF THE EQUIFAX AND TRANSUNION DATA BREACHES AND AM REQUESTING "
             "THAT THESE FRAUDULENT ACCOUNTS BE DELETED FROM MY CREDIT REPORTS\n\n"
             "As per FCRA 605B I am demanding that these accounts be blocked and deleted in 4 days")

MULLVAD_COUNTRIES = ["us"]  # US-only — avoids latency from overseas servers

def _mullvad_ip():
    try:
        r = requests.get("https://am.i.mullvad.net/json", timeout=5)
        j = r.json()
        return j.get("ip") if j.get("mullvad_exit_ip") else None
    except: return None

US_CITIES = ["qas", "atl", "chi", "dal", "mia", "nyc", "lax", "sea", "sjc", "den", "hou", "phx", "uyk", "was"]

def rotate_vpn():
    """Rotate Mullvad VPN via CLI. Picks a random US city and reconnects.
    Returns new exit IP on success, None on failure."""
    print("[VPN] Rotating Mullvad to new US city...", file=sys.stderr)
    old_ip = _mullvad_ip()
    city = random.choice(US_CITIES)
    print(f"[VPN] Target: us-{city} (was on IP {old_ip})", file=sys.stderr)
    try:
        subprocess.run(["mullvad", "relay", "set", "location", "us", city], capture_output=True, timeout=10, check=False)
        subprocess.run(["mullvad", "reconnect"], capture_output=True, timeout=10, check=False)
    except Exception as e:
        print(f"[VPN] Mullvad CLI error: {e}", file=sys.stderr)
        return None
    for _ in range(15):
        time.sleep(2)
        ip = _mullvad_ip()
        if ip and ip != old_ip:
            print(f"[VPN] New IP: {ip} (was {old_ip}) — us-{city}", file=sys.stderr)
            return ip
    print("[VPN] Rotation failed — IP didn't change after 30s", file=sys.stderr)
    return None

# === OPTIMIZATION #1: Asset cache setup ===
ASSET_CACHE_DIR.mkdir(exist_ok=True)
asset_cache = {}

def load_asset_cache():
    cache_file = ASSET_CACHE_DIR / "manifest.json"
    if cache_file.exists():
        try:
            manifest = json.loads(cache_file.read_text())
            for url, info in manifest.items():
                data_file = ASSET_CACHE_DIR / info["file"]
                if data_file.exists():
                    asset_cache[url] = {"body": data_file.read_bytes(), "content_type": info["content_type"]}
            print(f"[CACHE] Loaded {len(asset_cache)} cached assets", file=sys.stderr)
        except: pass

def save_asset_cache():
    manifest = {}
    for url, info in asset_cache.items():
        fname = f"asset_{hash(url) & 0xFFFFFFFF:08x}"
        (ASSET_CACHE_DIR / fname).write_bytes(info["body"])
        manifest[url] = {"file": fname, "content_type": info["content_type"]}
    (ASSET_CACHE_DIR / "manifest.json").write_text(json.dumps(manifest))
    print(f"[CACHE] Saved {len(manifest)} assets to cache", file=sys.stderr)

load_asset_cache()

t0 = time.time()
def log(msg): print(f"[{time.time()-t0:.1f}s] {msg}", file=sys.stderr)

now = datetime.now()
month_names = ["","January","February","March","April","May","June","July","August","September","October","November","December"]
dob_parts = CLIENT["dob"].split("/")
dob_month, dob_day, dob_year = dob_parts[0], dob_parts[1], dob_parts[2]

batch_label = f" [Report {BATCH_INDEX}]" if BATCH_INDEX else ""
acct_names = [a["name"] for a in ACCOUNTS]
log(f"START{batch_label} — {CLIENT['first_name']} {CLIENT['last_name']}, {CLIENT['city']} {CLIENT['state']} — {len(ACCOUNTS)} account(s): {', '.join(acct_names)}")
tv_result = {}

def rent_number():
    r = requests.post(f"{TV_BASE}/api/pub/v2/auth", headers={"X-API-KEY": API_KEY, "X-API-USERNAME": USERNAME}, timeout=15)
    token = r.json()["token"]
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(f"{TV_BASE}/api/pub/v2/verifications", json={"serviceName": "identitytheftgov", "capability": "sms"}, headers=h, timeout=15)
    vid = r.headers.get("Location", "").split("/")[-1]
    r2 = requests.get(f"{TV_BASE}/api/pub/v2/verifications/{vid}", headers=h, timeout=15)
    number = r2.json()["number"]
    digits = number.lstrip("+")
    if digits.startswith("1") and len(digits) == 11: digits = digits[1:]
    tv_result.update({"token": token, "vid": vid, "digits": digits})
    log(f"Number: {digits}")

with ThreadPoolExecutor(max_workers=1) as executor:
    tv_future = executor.submit(rent_number)

    with sync_playwright() as p:
        # headless=True for form filling — prevents macOS window-minimize stalling Angular rendering.
        # Browser is brought visible at the download page via a new CDP session.
        # Persistent profile pins "Save as PDF" as the print destination (prevents
        # AppleScript from accidentally sending report to a CUPS printer).
        _pin_save_as_pdf()
        ctx = p.chromium.launch_persistent_context(
            str(CHROMIUM_PROFILE), headless=False, slow_mo=0,
            viewport={"width": 1280, "height": 900}, accept_downloads=True,
        )
        browser = ctx.browser
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Hide Chromium from dock/taskbar but do NOT minimize (minimizing stalls Angular rendering on macOS)
        try: subprocess.Popen(["osascript", "-e", 'tell application "System Events" to set visible of process "Google Chrome for Testing" to false'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except: pass

        # === OPTIMIZATION #1: Route interception — cache assets, block analytics ===
        def handle_route(route):
            url = route.request.url
            # Block analytics/tracking
            for block in ["google-analytics", "googletagmanager", "hotjar", "newrelic", "doubleclick", "facebook.net", "analytics"]:
                if block in url.lower():
                    route.abort()
                    return
            # Serve from cache if available
            if url in asset_cache:
                route.fulfill(body=asset_cache[url]["body"],
                             content_type=asset_cache[url]["content_type"])
                return
            route.continue_()

        def capture_response(response):
            url = response.url
            ct = response.headers.get("content-type", "")
            if any(url.endswith(ext) for ext in [".js", ".css", ".woff2", ".woff", ".ttf"]):
                try:
                    body = response.body()
                    if len(body) > 1000:
                        asset_cache[url] = {"body": body, "content_type": ct}
                except: pass

        page.route("**/*", handle_route)
        page.on("response", capture_response)

        # Load page
        page.goto("https://www.identitytheft.gov/", wait_until="networkidle", timeout=30000)
        log("Loaded")

        # === WIZARD ===
        page.click("button.idt-hero_primary-cta-btn"); page.wait_for_timeout(100)
        page.click("button:has-text('Yes')"); page.wait_for_timeout(100)
        for l in page.query_selector_all("label"):
            if "credit card accounts" in l.inner_text().strip().lower(): l.click(); break
        page.wait_for_timeout(50); page.click("button:has-text('Continue')"); page.wait_for_timeout(100)
        for l in page.query_selector_all("label"):
            if "open a fraudulent" in l.inner_text().strip().lower(): l.click(); break
        page.wait_for_timeout(50); page.click("button:has-text('Continue')"); page.wait_for_timeout(100)
        page.click("a[href='/form/TheftInformation']")
        # Wait for Angular to fully initialize the TheftInformation reactive form.
        # Without this, the first 1-2 Add Another clicks silently fail because
        # Angular's form model hasn't bound the controls yet — DOM values are set
        # but the model is empty, so the save-and-clear doesn't persist.
        page.wait_for_selector("#companyName", state="visible", timeout=10000)
        page.wait_for_timeout(2000)
        log("Wizard done")

        # === STEP 1: ACCOUNTS ===
        # FTC uses SAVE-AND-CLEAR: "Add another" saves the current account internally and
        # clears the form for the next entry. There is always only 1 set of fields.
        # Flow: fill → click #addCompanyButton (saves+clears) → fill next → repeat.
        # Last account: fill but DON'T click Add Another — leave it for Continue.
        num_accounts = len(ACCOUNTS)
        dot_mm = str(now.month)
        dot_yy = str(now.year)

        for acct_idx, acct in enumerate(ACCOUNTS):
            acct_name = acct["name"]
            acct_num = acct["number"] if acct["number"] else "".join(random.choices(string.digits, k=6)) + "XXXX"
            acct_amt = acct.get("balance", "0") or "0"
            if not acct_amt or acct_amt == "0":
                acct_amt = str(random.randint(500, 5000))

            # Parse opened date from credit report (e.g. "08/2016" → month=8, year=2016)
            _opened = acct.get("opened", "")
            if _opened and "/" in _opened:
                _parts = _opened.split("/")
                open_mm = str(int(_parts[0]))  # strip leading zero
                open_yy = _parts[1] if len(_parts[1]) == 4 else str(now.year)
            else:
                open_mm = dot_mm
                open_yy = dot_yy

            # Fill using Playwright's native methods — proper InputEvents for Angular
            page.fill("#companyName", acct_name)
            page.select_option("#noticeDateMM", value=dot_mm)      # Date fraud discovered = filing month
            page.select_option("#noticeDateYY", value=dot_yy)      # Date fraud discovered = filing year
            page.select_option("#DoTMM", value=open_mm)            # Date account opened = from credit report
            page.select_option("#DoTYY", value=open_yy)            # Date account opened = from credit report
            page.fill("#amountObtained", acct_amt)
            page.fill("#accountNumber", acct_num)
            log(f"Account {acct_idx + 1}/{num_accounts}: {acct_name} #{acct_num} opened={open_mm}/{open_yy} amt=${acct_amt}")

            # For all except the last: click "Add another" to save this account + clear form
            if acct_idx < num_accounts - 1:
                page.evaluate("""() => {
                    const m = document.querySelector('.modal.show');
                    if (m) { const b = m.querySelector('button'); if (b) b.click(); }
                }""")
                page.wait_for_timeout(200)
                _added = False
                # Primary: Playwright click (triggers real mouse event that Angular responds to)
                _btn = page.query_selector("#addCompanyButton")
                if _btn and _btn.is_visible():
                    try:
                        _btn.click(timeout=5000)
                        _added = True
                    except Exception as _e:
                        log(f"Playwright click failed: {str(_e)[:60]} — trying JS fallback")
                # Fallback: JS click
                if not _added:
                    _added = page.evaluate("""() => {
                        const btn = document.getElementById('addCompanyButton');
                        if (btn) { btn.click(); return true; }
                        return false;
                    }""")
                if _added:
                    # Wait for Angular to save the account and clear the form
                    # Verify companyName is empty (form reset) before proceeding
                    for _wait in range(10):
                        page.wait_for_timeout(500)
                        _cleared = page.evaluate("() => { const c = document.getElementById('companyName'); return c && c.value === ''; }")
                        if _cleared:
                            break
                    if not _cleared:
                        log(f"WARN: form did not clear after Add Another for account {acct_idx + 1} — forcing clear")
                        page.evaluate("() => { const c = document.getElementById('companyName'); if (c) c.value = ''; }")
                        page.wait_for_timeout(500)
                else:
                    log(f"ABORT: #addCompanyButton not found for account {acct_idx + 1}")
                    break

        # POST-ACCOUNT VERIFICATION: FTC uses save-and-clear — each "Add another" click saves
        # the account and clears the form. Saved accounts get a visible "Delete" button.
        # Total = Delete buttons (saved) + 1 (current unsaved form if companyName has value).
        _form_count = page.evaluate("""() => {
            // Count visible Delete buttons = saved accounts
            let saved = 0;
            for (const btn of document.querySelectorAll('button')) {
                const t = (btn.textContent || '').trim().toLowerCase();
                if (t === 'delete' && btn.offsetParent !== null) saved++;
            }
            // Check if current form has a value (the last account, not yet saved)
            const cur = document.getElementById('companyName');
            const hasCurrent = cur && cur.value && cur.value.trim().length > 0 ? 1 : 0;
            return { saved, hasCurrent, total: saved + hasCurrent };
        }""")
        _total = _form_count.get("total", 0)
        log(f"Account count: {_form_count.get('saved',0)} saved + {_form_count.get('hasCurrent',0)} current = {_total}")
        if _total < num_accounts:
            log(f"ABORT: only {_total}/{num_accounts} accounts on form — 'Add another' likely failed")
            log("Closing browser WITHOUT submitting — fix Add Another button handling")
            try:
                requests.post(f"{TV_BASE}/api/pub/v2/verifications/{tv_result.get('vid','')}/cancel",
                              headers={"Authorization": f"Bearer {tv_result.get('token','')}"}, timeout=5)
            except Exception: pass
            browser.close()
            save_asset_cache()
            sys.exit(1)
        log(f"Account verification OK: {_total}/{num_accounts} on form")

        # Click Continue to personal info — dismiss any modal (tdOopsModal etc) first, retry on failure
        _continue_ok = False
        for _retry in range(5):
            # Dismiss any visible modal (tdOopsModal, validation popups, etc)
            page.evaluate("""() => {
                const modals = document.querySelectorAll('.modal.show');
                for (const m of modals) {
                    const closers = m.querySelectorAll('button, a');
                    for (const c of closers) {
                        const t = (c.textContent || '').toLowerCase().trim();
                        if (t === 'ok' || t === 'close' || t === 'dismiss' || t === 'continue' || t === 'got it') {
                            c.click(); return;
                        }
                    }
                    // fallback: click the last button in the modal (typically the primary CTA)
                    const btns = m.querySelectorAll('button');
                    if (btns.length) btns[btns.length - 1].click();
                }
            }""")
            page.wait_for_timeout(400)
            try:
                btn = page.query_selector("button:has-text('Continue'):visible")
                if btn and btn.is_enabled():
                    btn.click(timeout=5000)
                    _continue_ok = True
                    break
            except Exception as e:
                log(f"Continue click attempt {_retry + 1} failed: {str(e)[:80]}")
                page.wait_for_timeout(500)
        if not _continue_ok:
            log("WARN: Continue click failed after 5 retries — personal info step may not advance")
        page.wait_for_timeout(500)
        log(f"All {num_accounts} accounts done")

        # Wait for TextVerified number
        tv_future.result(timeout=30)
        token = tv_result["token"]
        vid = tv_result["vid"]
        digits = tv_result["digits"]

        # === STEP 2: PERSONAL INFO + 2FA (JS batch fill for text fields) ===
        page.evaluate(f"""() => {{
            function fill(id, val) {{
                const el = document.getElementById(id);
                if (!el) return;
                el.value = val;
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
            }}
            fill('fName', '{CLIENT["first_name"]}');
            fill('lName', '{CLIENT["last_name"]}');
        }}""")
        from playwright.sync_api import expect
        sel_fn = lambda pg, s, text: None  # placeholder
        # Country select
        el = page.query_selector("#country")
        if el:
            try: el.select_option(label="UNITED STATES")
            except: pass
        page.wait_for_timeout(150)

        # Phone
        pi = page.query_selector("#primePhone")
        if pi: pi.fill(digits)
        el = page.query_selector("#primePhoneType")
        if el:
            try: el.select_option(label="Mobile")
            except:
                for o in el.query_selector_all("option"):
                    if "mobile" in o.inner_text().strip().lower():
                        try: el.select_option(value=o.get_attribute("value")); break
                        except: pass
        page.wait_for_timeout(200)

        # 2FA
        for s in page.query_selector_all("select:visible"):
            for o in s.query_selector_all("option"):
                if "text message" in o.inner_text().strip().lower():
                    s.select_option(value=o.get_attribute("value")); break
        page.wait_for_timeout(100)

        def click_get_my_code():
            gcb = page.query_selector("button:has-text('Get My Code'):visible")
            if gcb: gcb.click()
            page.wait_for_timeout(1500)

        def detect_send_failure():
            """Look for 'We could not send a code to your phone' error on the page.
            Signals a phone-side problem (bad TV number), not a VPN issue."""
            try:
                err_selectors = [
                    "text=/could not send.*code.*phone/i",
                    "text=/unable to send.*code/i",
                    "text=/invalid.*phone number/i",
                ]
                for sel in err_selectors:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        return el.inner_text().strip()[:120]
            except: pass
            return None

        def swap_phone_number(old_vid, old_token):
            """Cancel current TV reservation, rent a new number, refill the form.
            Returns (new_vid, new_token, new_digits) or None on failure.
            Free — TV only charges when SMS is received."""
            log("[PHONE] Cancelling current number, getting new one...")
            try: requests.post(f"{TV_BASE}/api/pub/v2/verifications/{old_vid}/cancel", headers={"Authorization": f"Bearer {old_token}"}, timeout=5)
            except: pass

            try:
                r = requests.post(f"{TV_BASE}/api/pub/v2/auth", headers={"X-API-KEY": API_KEY, "X-API-USERNAME": USERNAME}, timeout=15)
                new_token = r.json()["token"]
                h = {"Authorization": f"Bearer {new_token}", "Content-Type": "application/json"}
                r = requests.post(f"{TV_BASE}/api/pub/v2/verifications", json={"serviceName": "identitytheftgov", "capability": "sms"}, headers=h, timeout=15)
                new_vid = r.headers.get("Location", "").split("/")[-1]
                r2 = requests.get(f"{TV_BASE}/api/pub/v2/verifications/{new_vid}", headers=h, timeout=15)
                number = r2.json()["number"]
                digits = number.lstrip("+")
                if digits.startswith("1") and len(digits) == 11: digits = digits[1:]
                log(f"[PHONE] New number: {digits}")
            except Exception as _e:
                log(f"[PHONE] Failed to rent new number: {_e}")
                return None

            # Refill the primary phone field
            try:
                phone_sel = "input[id*='phone']:visible, input[name*='phone']:visible, input[formcontrolname*='phone']:visible"
                phone_input = page.query_selector(phone_sel)
                if phone_input:
                    phone_input.fill("")
                    page.wait_for_timeout(100)
                    phone_input.fill(digits)
                    log("[PHONE] Form refilled with new number")
            except Exception as _e:
                log(f"[PHONE] Failed to refill form: {_e}")
                return None

            # Click Get My Code again
            click_get_my_code()
            return new_vid, new_token, digits

        click_get_my_code()
        log("Get My Code clicked — polling SMS")

        MAX_NUMBER_SWAPS = 3
        number_swaps = 0

        # Poll SMS — with early-exit on detected send failure
        code = None
        poll_start = time.time()
        while time.time() - poll_start < 10:
            r = requests.get(f"{TV_BASE}/api/pub/v2/sms", params={"reservationId": vid}, headers={"Authorization": f"Bearer {token}"}, timeout=10)
            data = r.json().get("data", [])
            if data and data[0].get("parsedCode"):
                code = data[0]["parsedCode"]; break

            # Early-exit: FTC displayed "could not send code" — swap number immediately
            err = detect_send_failure()
            if err and number_swaps < MAX_NUMBER_SWAPS:
                log(f"[PHONE] FTC reported: {err}")
                swap = swap_phone_number(vid, token)
                if swap:
                    vid, token, _ = swap
                    number_swaps += 1
                    poll_start = time.time()  # reset poll window for new number
                    continue
                else:
                    break

            time.sleep(0.5)

        # If still no code after 45s with no explicit failure, try one more number swap
        # (handles silent SMS blocks) before falling back to VPN rotation
        if not code and number_swaps < MAX_NUMBER_SWAPS:
            log("[PHONE] SMS timed out silently — trying new number first (free)")
            swap = swap_phone_number(vid, token)
            if swap:
                vid, token, _ = swap
                number_swaps += 1
                poll_start = time.time()
                while time.time() - poll_start < 10:
                    r = requests.get(f"{TV_BASE}/api/pub/v2/sms", params={"reservationId": vid}, headers={"Authorization": f"Bearer {token}"}, timeout=10)
                    data = r.json().get("data", [])
                    if data and data[0].get("parsedCode"):
                        code = data[0]["parsedCode"]; break
                    time.sleep(0.5)

        if not code:
            log("SMS TIMEOUT after number swaps — rotating VPN")
            try: requests.post(f"{TV_BASE}/api/pub/v2/verifications/{vid}/cancel", headers={"Authorization": f"Bearer {token}"}, timeout=5)
            except: pass
            browser.close()
            save_asset_cache()
            rotation_count = getattr(sys.modules[__name__], '_rotation_count', 0) + 1
            if rotation_count > MAX_VPN_ROTATIONS:
                print(f"FAILED after {MAX_VPN_ROTATIONS} rotations", file=sys.stderr); sys.exit(1)
            sys.modules[__name__]._rotation_count = rotation_count
            new_ip = rotate_vpn()
            if new_ip:
                log(f"Retrying (attempt {rotation_count + 1})...")
                os.execv(sys.executable, [sys.executable] + sys.argv)
            else:
                sys.exit(1)

        log(f"CODE: {code}")
        code_filled = False
        for s_sel in ["input[name*='code']", "input[id*='code']", "input[placeholder*='code']",
                  "input[formcontrolname*='code']", "input[formcontrolname*='Code']",
                  "input[aria-label*='code']", "input[inputmode='numeric']"]:
            ci = page.query_selector(s_sel)
            if ci and ci.is_visible():
                ci.fill(code); code_filled = True; log(f"Code entered in: {s_sel}"); break
        if not code_filled:
            for inp in page.query_selector_all("input:visible"):
                ml = inp.get_attribute("maxlength") or ""
                if ml and int(ml) <= 10 and not inp.get_attribute("value"):
                    inp.fill(code); code_filled = True; log("Code entered in short input"); break

        # Verify click
        verify_ok = False
        try: page.get_by_role("button", name="Verify").click(timeout=3000); verify_ok = True
        except:
            try: page.locator("button:has-text('Verify')").first.click(force=True, timeout=2000); verify_ok = True
            except:
                verify_ok = page.evaluate("""() => {
                    const els = document.querySelectorAll('button, a, span, div, input');
                    for (const el of els) {
                        if (el.textContent.trim() === 'Verify' && el.offsetParent !== null) {
                            el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                            return true;
                        }
                    }
                    return false;
                }""")

        try: page.wait_for_function("() => !document.body.innerText.includes('Sending code')", timeout=10000)
        except: page.wait_for_timeout(1000)
        log(f"2FA {'verified' if verify_ok else 'FAILED'}")

        # === OPTIMIZATION #3: JS batch fill for ALL remaining personal info fields ===
        # FIX 1: wait for address field to be visible before filling (no silent skip)
        try:
            page.wait_for_selector("#stAddr", state="visible", timeout=5000)
        except Exception:
            log("WARN: #stAddr not visible after 5s — will attempt fill anyway")

        # FIX 2: state dropdown FIRST (Angular may gate address fields on state binding)
        # FIX 3: use exact text match (trimmed) instead of includes() to avoid substring false positives
        _state_full = STATE_NAMES.get(CLIENT["state"], CLIENT["state"])
        page.evaluate(f"""() => {{
            function fill(id, val) {{
                const el = document.getElementById(id);
                if (!el) {{ console.warn('[FTC] field not found: ' + id); return false; }}
                el.value = val;
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
                el.dispatchEvent(new Event('blur', {{bubbles: true}}));
                return true;
            }}
            function setSelect(id, matchText) {{
                const sel = document.getElementById(id);
                if (!sel) {{ console.warn('[FTC] select not found: ' + id); return false; }}
                for (const opt of sel.options) {{
                    if (opt.text.trim() === matchText) {{
                        sel.value = opt.value;
                        sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                        sel.dispatchEvent(new Event('input', {{bubbles: true}}));
                        return true;
                    }}
                }}
                // Fallback: substring match if exact fails
                for (const opt of sel.options) {{
                    if (opt.text.includes(matchText)) {{
                        sel.value = opt.value;
                        sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                        sel.dispatchEvent(new Event('input', {{bubbles: true}}));
                        return true;
                    }}
                }}
                console.warn('[FTC] no match for select ' + id + ' with text: ' + matchText);
                return false;
            }}
            // State FIRST — Angular may need this before address fields are bound
            setSelect('stat', '{_state_full}');
            // Email
            fill('eAddre', '{CLIENT["email"]}');
            fill('confEAddr', '{CLIENT["email"]}');
            // Address (after state so Angular form model is ready)
            fill('stAddr', '{CLIENT["address"]}');
            fill('citay', '{CLIENT["city"]}');
            fill('openZippy', '{CLIENT["zip"]}');
            // DOB
            setSelect('dobYear', '{dob_year}');
            setSelect('dobMonth', '{month_names[int(dob_month)]}');
            setSelect('haveLivedYear', '{now.year - 4}');
            setSelect('haveLivedMonth', '{month_names[now.month]}');
        }}""")
        # DOB day needs a tick for Angular to register month first
        page.wait_for_timeout(250)
        page.evaluate(f"""() => {{
            const sel = document.getElementById('dobDay');
            if (!sel) return;
            for (const opt of sel.options) {{
                if (opt.text.trim() === '{int(dob_day)}') {{
                    sel.value = opt.value;
                    sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                    break;
                }}
            }}
        }}""")

        # Click radios
        page.click("label[for='filingFor0']")
        page.click("label[for='changedSinceTheftNo']")
        page.click("label[for='MilitaryNo']")
        page.wait_for_timeout(300)

        # FIX 4: post-fill verification — read back critical fields and log mismatches
        _verify = page.evaluate("""() => {
            const fields = {
                stAddr: document.getElementById('stAddr')?.value || '',
                citay: document.getElementById('citay')?.value || '',
                openZippy: document.getElementById('openZippy')?.value || '',
                stat: document.getElementById('stat')?.selectedOptions[0]?.text || '',
            };
            return fields;
        }""")
        _expected = {"stAddr": CLIENT["address"], "citay": CLIENT["city"], "openZippy": CLIENT["zip"], "stat": _state_full}
        _mismatches = []
        for k, expected in _expected.items():
            actual = _verify.get(k, "")
            if not actual or (expected and expected not in actual):
                _mismatches.append(f"{k}: expected='{expected}' got='{actual}'")
        if _mismatches:
            log(f"ABORT: post-fill verification FAILED — address not on form: {'; '.join(_mismatches)}")
            log("Closing browser WITHOUT submitting — no broken report filed")
            try:
                requests.post(f"{TV_BASE}/api/pub/v2/verifications/{vid}/cancel",
                              headers={"Authorization": f"Bearer {token}"}, timeout=5)
                log("TV verification cancelled")
            except Exception:
                pass
            browser.close()
            save_asset_cache()
            sys.exit(1)
        log("Post-fill verification OK: address + state confirmed")
        log("Personal info done (batch)")

        # Continue — dismiss modals
        for retry in range(3):
            page.evaluate("document.querySelector('.modal.show button')?.click()")
            page.wait_for_timeout(150)
            try:
                btn = page.query_selector("button:has-text('Continue'):visible")
                if btn and btn.is_enabled(): btn.click(timeout=5000)
                page.wait_for_timeout(500)
                break
            except:
                page.evaluate("document.querySelector('.modal.show button')?.click()")
                page.wait_for_timeout(300)

        # Steps 3-5
        prev_url = page.url
        for step in range(10):
            url = page.url.lower()
            if "/form/summary" in url:
                log("Review page"); break
            elif "/form/suspect" in url:
                log("Suspect page — skip")
            elif "/form/additionalinformation" in url:
                for rid in ["creditReportYes", "fruadAccountsYes", "breachQuestionYes"]:
                    r_el = page.query_selector(f"#{rid}")
                    if r_el: page.click(f"label[for='{rid}']")
                log("Additional info done")
            elif "/form/comments" in url:
                dc = page.query_selector("#debtCollectorContactYes")
                if dc: page.click("label[for='debtCollectorContactYes']")
                ta = page.query_selector("#Comments")
                if ta and ta.is_visible(): ta.fill(STATEMENT); log("Statement filled")
                else:
                    page.wait_for_timeout(200)
                    ta = page.query_selector("#Comments")
                    if ta: ta.fill(STATEMENT); log("Statement filled (retry)")

            page.evaluate("document.querySelector('.modal.show button')?.click()")
            prev_url = page.url
            try:
                btn = page.query_selector("#BottomNav_cont") or page.query_selector("button:has-text('Continue'):visible")
                if btn and btn.is_enabled(): btn.click(timeout=3000)
                try: page.wait_for_function(f"() => window.location.href !== '{prev_url}'", timeout=5000)
                except: page.wait_for_timeout(200)
            except: pass

        # Perjury + Finalize
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)"); page.wait_for_timeout(200)
        try: page.click("label[for='perjuryTaxCheckbox']", timeout=5000); log("Perjury checked")
        except:
            try: page.locator("#perjuryTaxCheckbox").check(force=True, timeout=3000); log("Perjury (force)")
            except: log("Perjury FAILED")

        for txt in ["Finalize", "Submit"]:
            btn = page.query_selector(f"button:has-text('{txt}'):visible, a:has-text('{txt}'):visible")
            if btn and btn.is_visible(): btn.click(); log(f"Clicked {txt}"); break

        # Post-finalize
        try: page.wait_for_selector("#accountNo", state="visible", timeout=5000)
        except: pass
        try: page.click("#accountNo", timeout=2000); log("No thanks")
        except:
            try: page.locator("text=No thanks").first.click(timeout=2000); log("No thanks (text)")
            except: pass

        try: page.wait_for_selector("#optOutConfirmModal", state="visible", timeout=3000)
        except: page.wait_for_timeout(300)
        try:
            page.locator("#optOutConfirmModal >> text=submit without an account").first.click(timeout=2000)
            log("Confirmed no account")
        except:
            page.evaluate("""() => { const m = document.getElementById('optOutConfirmModal'); if(m){const l=m.querySelectorAll('a,button'); for(const e of l){if(e.textContent.toLowerCase().includes('submit without')){e.click();return true;}}} return false; }""")
            log("Confirmed no account (JS)")

        try: page.locator("button:has-text('Continue'):visible").first.click(timeout=3000); log("Continue clicked")
        except:
            try: page.locator("button.btn-info.ml-3").first.click(timeout=2000); log("Continue (btn-info)")
            except: pass

        try: page.wait_for_url("**/confirmation/genericsteps", timeout=8000)
        except: page.wait_for_timeout(500)

        # Result
        try: page.screenshot(path="/tmp/ftc_test_result.png", timeout=5000)
        except: pass
        body = page.inner_text("body")[:500]
        if "success" in body.lower():
            log("SUCCESS!")
            match = re.search(r'(\d{6,12})', body)
            if match: log(f"Report Number: {match.group(1)}")

        log(f"Final URL: {page.url}")
        print(f"\n=== FILING COMPLETE: {time.time()-t0:.1f}s ({len(ACCOUNTS)} account{'s' if len(ACCOUNTS) != 1 else ''}) ===", file=sys.stderr)

        # Save asset cache for next run
        save_asset_cache()

        # Bring browser on-screen
        try:
            cdp = ctx.new_cdp_session(page)
            win = cdp.send("Browser.getWindowForTarget")
            wid = win["windowId"]
            cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
            page.wait_for_timeout(300)
            cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"left": 100, "top": 100, "width": 1100, "height": 800}})
            cdp.detach()
        except: pass
        try: subprocess.Popen(["osascript", "-e", 'tell application "System Events" to set visible of process "Google Chrome for Testing" to true'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except: pass
        page.wait_for_timeout(300)
        try: subprocess.Popen(["osascript", "-e", 'tell application "Google Chrome for Testing" to activate'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except: pass
        page.set_viewport_size({"width": 1100, "height": 800})
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
        log("Browser on-screen")

        # ====================================================================
        # LAYER 1.5 — Resolve save path (pure logic, no I/O)
        # ====================================================================
        _batch_idx = os.environ.get("FTC_BATCH_INDEX", "1")
        _report_name = f"{CLIENT['first_name']} FTC {_batch_idx}"
        _pdf_filename = f"{_report_name}.pdf"
        _staging_dir = Path("/private/tmp/ftc_staging")
        _staging_dir.mkdir(exist_ok=True)
        _staging_path = _staging_dir / _pdf_filename

        # Auto-detect client folder for final destination
        # Match on full name (first + last) to avoid collisions with other clients sharing a last name
        _clients_base = "/Users/mo/Library/CloudStorage/GoogleDrive-moe@getcredify.io/My Drive/Credit Repair/Clients/Active Clients"
        _first_name_part = CLIENT["first_name"].strip().lower()
        _last_name_part = CLIENT["last_name"].split()[-1].lower()
        _full_name_match = f"{_first_name_part} {_last_name_part}"
        _final_dir = None
        try:
            folders = os.listdir(_clients_base)
            # Prefer exact full-name match
            for d in folders:
                if d.lower() == _full_name_match:
                    _candidate = os.path.join(_clients_base, d, "FTC Reports")
                    if os.path.isdir(_candidate):
                        _final_dir = _candidate
                        break
            # Fall back to substring match on full name
            if not _final_dir:
                for d in folders:
                    if _first_name_part in d.lower() and _last_name_part in d.lower():
                        _candidate = os.path.join(_clients_base, d, "FTC Reports")
                        if os.path.isdir(_candidate):
                            _final_dir = _candidate
                            break
        except Exception:
            pass
        if not _final_dir:
            _final_dir = os.path.expanduser("~/Desktop/ftc_reports")
            Path(_final_dir).mkdir(exist_ok=True)

        _final_path = os.path.join(_final_dir, _pdf_filename)
        log(f"[SAVE] staging: {_staging_path}")
        log(f"[SAVE] final:   {_final_path}")

        # Clean stale staging file if present from a prior run
        if _staging_path.exists():
            _staging_path.unlink()

        # ====================================================================
        # LAYER 2a — Trigger OS-level print dialog via "Download Report" button
        # ====================================================================
        _download_triggered = False
        try:
            _dl_btn = page.query_selector("button:has-text('Download Report')")
            if _dl_btn and _dl_btn.is_visible():
                _dl_btn.click(timeout=5000)
                _download_triggered = True
                log("[SAVE] Download Report button clicked")
            else:
                page.evaluate("""() => {
                    for (const b of document.querySelectorAll('button')) {
                        if (b.textContent.includes('Download Report')) { b.click(); return true; }
                    }
                    return false;
                }""")
                _download_triggered = True
                log("[SAVE] Download Report clicked (JS fallback)")
        except Exception as _e:
            log(f"[SAVE] FAIL Layer 2a — download button click: {str(_e)[:80]}")

        _file_verified = False
        _routed = False

        if not _download_triggered:
            log("[SAVE] FAIL — could not trigger download. Skipping save.")
        else:
            # Wait for Chromium print dialog to render (6s — preview needs time)
            page.wait_for_timeout(6000)

            # ================================================================
            # LAYER 2b — Native save bridge (AppleScript handles OS dialog)
            # ================================================================
            _script = Path(__file__).parent / "ftc_save_report.applescript"
            _bridge_success = False
            _bridge_stdout = ""
            _bridge_stderr = ""
            log("[SAVE] Invoking AppleScript native save bridge...")
            try:
                _result = subprocess.run(
                    ["osascript", str(_script), _report_name, str(_staging_dir)],
                    capture_output=True, text=True, timeout=60
                )
                _bridge_stdout = _result.stdout.strip()
                _bridge_stderr = _result.stderr.strip()
                _bridge_success = _result.returncode == 0
                log(f"[SAVE] AppleScript returned (rc={_result.returncode})")
            except subprocess.TimeoutExpired:
                log("[SAVE] FAIL Layer 2b — AppleScript timed out (60s)")
                _bridge_stderr = "timeout"
            except Exception as _e:
                log(f"[SAVE] FAIL Layer 2b — AppleScript error: {_e}")
                _bridge_stderr = str(_e)

            # ================================================================
            # LAYER 3a — File verification + stabilization
            # ================================================================
            if _bridge_success or _bridge_stderr == "":
                for _poll in range(15):
                    time.sleep(1)
                    if _staging_path.exists() and _staging_path.stat().st_size > 0:
                        _size1 = _staging_path.stat().st_size
                        time.sleep(1)
                        if _staging_path.exists():
                            _size2 = _staging_path.stat().st_size
                            if _size2 == _size1 and _size2 > 1000:
                                _file_verified = True
                                log(f"[SAVE] File verified in staging: {_size2} bytes, stable")
                                break
                            else:
                                log(f"[SAVE] File still writing: {_size1} -> {_size2}")

                if not _file_verified:
                    if os.path.exists(_final_path) and os.path.getsize(_final_path) > 1000:
                        _file_verified = True
                        log(f"[SAVE] File found directly in final dir: {os.path.getsize(_final_path)} bytes")

                # Fallback: Chromium may have saved without .pdf extension
                if not _file_verified:
                    _extensionless = _staging_dir / _report_name
                    if _extensionless.exists() and _extensionless.stat().st_size > 1000:
                        _extensionless.rename(_staging_path)
                        _file_verified = True
                        log(f"[SAVE] Found extensionless file, renamed to .pdf: {_staging_path.stat().st_size} bytes")

                if not _file_verified:
                    log("[SAVE] FAIL Layer 3a — file not found or unstable after 15s")
                    log(f"[SAVE]   AppleScript stdout: {_bridge_stdout}")
                    log(f"[SAVE]   AppleScript stderr: {_bridge_stderr[:200]}")

            # ================================================================
            # LAYER 3b — Route file from staging to final destination
            # ================================================================
            if _file_verified and _staging_path.exists():
                try:
                    import shutil as _shutil
                    _shutil.move(str(_staging_path), _final_path)
                    _routed = True
                    log(f"[SAVE] Routed to final: {_final_path}")
                except Exception as _e:
                    log(f"[SAVE] FAIL Layer 3b — move failed: {_e}")
                    log(f"[SAVE] File preserved in staging: {_staging_path}")
            elif _file_verified and os.path.exists(_final_path):
                _routed = True
                log(f"[SAVE] Already in final dir (no move needed)")

            # ================================================================
            # LAYER 4 — Final status
            # ================================================================
            if _routed:
                log(f"[SAVE] SUCCESS — {_final_path}")
            elif _file_verified:
                log(f"[SAVE] PARTIAL — file verified but routing failed. Check staging: {_staging_path}")
            elif _bridge_success:
                log(f"[SAVE] FAIL — AppleScript ran but no file detected")
            else:
                log(f"[SAVE] FAIL — AppleScript did not complete successfully")

        # ====================================================================
        # Cleanup: delete audit JSON if it was a temp file, close browser
        # ====================================================================
        _audit_path = os.path.expanduser(args.audit_json)
        if os.path.exists(_audit_path) and "/tmp/" in _audit_path:
            os.unlink(_audit_path)
            log(f"Cleaned up temp audit: {_audit_path}")

        log("Closing browser")
        try:
            browser.close()
        except Exception:
            pass
        log("Done.")

# Cleanup
requests.post(f"{TV_BASE}/api/pub/v2/verifications/{tv_result.get('vid','')}/cancel", headers={"Authorization": f"Bearer {tv_result.get('token','')}"}, timeout=15)
