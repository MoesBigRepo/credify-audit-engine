"""FTC Identity Theft Report Filer — Playwright automation for identitytheft.gov.

Files an identity theft report with the FTC for each disputed item.
Runs in HEADED mode for CAPTCHA handling and operator verification.
Integrates with TextVerified API v2 for programmatic SMS 2FA.

Usage:
    python3 ftc_filer.py --client client.json
    python3 ftc_filer.py --client client.json --audit-json audit.json --dry-run

    # With TextVerified 2FA (env vars: TEXTVERIFIED_API_KEY, TEXTVERIFIED_USERNAME)
    python3 ftc_filer.py --client client.json --tv-auto
    python3 ftc_filer.py --client client.json --tv-service identitytheft.gov --dry-run
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

FTC_URL = "https://www.identitytheft.gov/"
COMET_PATH = "/Applications/Comet.app/Contents/MacOS/Comet"

TV_BASE = "https://www.textverified.com"
TV_SMS_POLL_INTERVAL = 3
TV_SMS_POLL_TIMEOUT = 90


class TextVerifiedClient:
    """TextVerified API v2 client for programmatic SMS verification."""

    def __init__(self, api_key, username):
        self.api_key = api_key
        self.username = username
        self.token = None
        self.verif_id = None
        self.number = None

    def authenticate(self):
        resp = requests.post(f"{TV_BASE}/api/pub/v2/auth", headers={
            "X-API-KEY": self.api_key,
            "X-API-USERNAME": self.username,
        }, timeout=15)
        resp.raise_for_status()
        self.token = resp.json()["token"]
        print(f"  [TV] Authenticated (token expires in {resp.json()['expiresIn']}s)", file=sys.stderr)

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def check_balance(self):
        resp = requests.get(f"{TV_BASE}/api/pub/v2/account/me", headers=self._headers(), timeout=15)
        resp.raise_for_status()
        balance = resp.json()["currentBalance"]
        print(f"  [TV] Account balance: {balance} credits", file=sys.stderr)
        return balance

    def check_price(self, service_name, service_not_listed_name=None):
        body = {
            "serviceName": service_name,
            "areaCode": False,
            "carrier": False,
            "numberType": "mobile",
            "capability": "sms",
        }
        resp = requests.post(f"{TV_BASE}/api/pub/v2/pricing/verifications",
                             json=body, headers=self._headers(), timeout=15)
        if resp.status_code == 200:
            price = resp.json()["price"]
            print(f"  [TV] Verification price: {price} credits", file=sys.stderr)
            return price
        print(f"  [TV] Price check failed ({resp.status_code}), proceeding anyway", file=sys.stderr)
        return None

    def create_verification(self, service_name="servicenotlisted", service_not_listed_name="identitytheft.gov"):
        body = {
            "serviceName": service_name,
            "capability": "sms",
        }
        if service_name == "servicenotlisted" and service_not_listed_name:
            body["serviceNotListedName"] = service_not_listed_name
        resp = requests.post(f"{TV_BASE}/api/pub/v2/verifications",
                             json=body, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        location = resp.headers.get("Location", "")
        self.verif_id = location.split("/")[-1] if location else None
        if not self.verif_id:
            href = resp.json().get("href", "")
            self.verif_id = href.split("/")[-1] if href else None
        if not self.verif_id:
            raise RuntimeError("Failed to extract verification ID from response")
        print(f"  [TV] Verification created: {self.verif_id}", file=sys.stderr)
        return self.verif_id

    def get_number(self):
        resp = requests.get(f"{TV_BASE}/api/pub/v2/verifications/{self.verif_id}",
                            headers=self._headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        self.number = data["number"]
        state = data["state"]
        ends_at = data.get("endsAt", "?")
        print(f"  [TV] Number: {self.number} (state={state}, expires={ends_at})", file=sys.stderr)
        return self.number

    def poll_for_code(self, timeout=None, interval=None):
        timeout = timeout or TV_SMS_POLL_TIMEOUT
        interval = interval or TV_SMS_POLL_INTERVAL
        start = time.time()
        print(f"  [TV] Polling for SMS (timeout={timeout}s, interval={interval}s)...", file=sys.stderr)
        while time.time() - start < timeout:
            resp = requests.get(f"{TV_BASE}/api/pub/v2/sms",
                                params={"reservationId": self.verif_id},
                                headers=self._headers(), timeout=15)
            resp.raise_for_status()
            messages = resp.json().get("data", [])
            if messages:
                sms = messages[0]
                code = sms.get("parsedCode")
                content = sms.get("smsContent", "")
                if code:
                    print(f"  [TV] Code received: {code}", file=sys.stderr)
                    return code
                if content:
                    import re
                    match = re.search(r'\b(\d{4,8})\b', content)
                    if match:
                        code = match.group(1)
                        print(f"  [TV] Code extracted from SMS body: {code}", file=sys.stderr)
                        return code
            elapsed = int(time.time() - start)
            print(f"  [TV] Waiting for SMS... ({elapsed}s)", file=sys.stderr)
            time.sleep(interval)
        raise TimeoutError(f"No SMS received within {timeout}s")

    def cancel(self):
        if not self.verif_id:
            return
        try:
            resp = requests.post(f"{TV_BASE}/api/pub/v2/verifications/{self.verif_id}/cancel",
                                 headers=self._headers(), timeout=15)
            if resp.status_code == 200:
                print(f"  [TV] Verification canceled", file=sys.stderr)
            else:
                print(f"  [TV] Cancel returned {resp.status_code}", file=sys.stderr)
        except Exception as e:
            print(f"  [TV] Cancel failed: {e}", file=sys.stderr)

    def report_issue(self):
        if not self.verif_id:
            return
        try:
            requests.post(f"{TV_BASE}/api/pub/v2/verifications/{self.verif_id}/report",
                          headers=self._headers(), timeout=15)
            print(f"  [TV] Verification reported for issue", file=sys.stderr)
        except Exception:
            pass


def _format_accounts_for_ftc(derog_accounts):
    """Format derogatory accounts for FTC report entry."""
    entries = []
    for acct in derog_accounts:
        entries.append({
            "company": acct.get("creditor", "Unknown"),
            "account_number": acct.get("account_number", ""),
            "date_opened": acct.get("opened", ""),
            "balance": acct.get("balance", "$0"),
            "type": _classify_account_type(acct),
        })
    return entries


def _classify_account_type(acct):
    """Map account to FTC account type category."""
    if acct.get("is_collection"):
        return "Collection account"
    if acct.get("is_chargeoff"):
        return "Credit card or charge account"
    acct_type = acct.get("type", "").lower()
    if "auto" in acct_type or "car" in acct_type:
        return "Auto loan or lease"
    if "mortgage" in acct_type or "home" in acct_type:
        return "Mortgage"
    if "student" in acct_type:
        return "Student loan"
    if "card" in acct_type or "revolving" in acct_type:
        return "Credit card or charge account"
    return "Other"


FTC_MAX_ACCOUNTS_PER_REPORT = 5


def file_report(client, derog_accounts, dry_run=False, tv_client=None, tv_service="identitytheftgov", tv_service_label="identitytheft.gov"):
    """File FTC Identity Theft Reports. Auto-batches into multiple reports if >5 accounts.

    Args:
        client: dict with keys: first_name, last_name, dob, ssn, email, phone, address, city, state, zip
        derog_accounts: list of derogatory account dicts from Credify
        dry_run: if True, fill form but don't submit
        tv_client: optional TextVerifiedClient for programmatic SMS 2FA
        tv_service: TextVerified service name (default: identitytheftgov at $0.50)
        tv_service_label: label for servicenotlisted (only if tv_service=servicenotlisted)

    Returns:
        dict (single report) or list of dicts (multiple reports if >5 accounts)
    """
    all_accounts = _format_accounts_for_ftc(derog_accounts)

    if len(all_accounts) <= FTC_MAX_ACCOUNTS_PER_REPORT:
        return _file_single_report(client, all_accounts, dry_run=dry_run,
                                   tv_client=tv_client, tv_service=tv_service,
                                   tv_service_label=tv_service_label)

    # Batch into groups of 5
    batches = [all_accounts[i:i + FTC_MAX_ACCOUNTS_PER_REPORT]
               for i in range(0, len(all_accounts), FTC_MAX_ACCOUNTS_PER_REPORT)]
    total = len(batches)
    print(f"FTC limit: 5 accounts per report. {len(all_accounts)} accounts → {total} reports.", file=sys.stderr)

    results = []
    for batch_num, batch in enumerate(batches, 1):
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"  REPORT {batch_num}/{total} ({len(batch)} accounts)", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)

        # Each report needs its own TextVerified number ($0.50 each)
        batch_tv = None
        if tv_client:
            batch_tv = TextVerifiedClient(tv_client.api_key, tv_client.username)

        result = _file_single_report(client, batch, dry_run=dry_run,
                                     tv_client=batch_tv, tv_service=tv_service,
                                     tv_service_label=tv_service_label)
        results.append({**result, "batch": batch_num, "total_batches": total})

        if batch_num < total:
            print(f"\n  Next report in 5 seconds...", file=sys.stderr)
            time.sleep(5)

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  ALL DONE: {total} reports filed ({len(all_accounts)} accounts total)", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    return results


_SCREENSHOT_COUNT = 0


def _snap(page, label):
    """Take a timestamped screenshot and log URL + page title."""
    global _SCREENSHOT_COUNT
    _SCREENSHOT_COUNT += 1
    fname = f"/tmp/ftc-screenshots/{_SCREENSHOT_COUNT:02d}_{label}.png"
    try:
        page.screenshot(path=fname, full_page=False)
        url = page.url
        print(f"  [SNAP {_SCREENSHOT_COUNT:02d}] {label} | URL: {url} | saved: {fname}", file=sys.stderr)
    except Exception as e:
        print(f"  [SNAP {_SCREENSHOT_COUNT:02d}] {label} | FAILED: {e}", file=sys.stderr)


def _file_single_report(client, accounts, dry_run=False, tv_client=None, tv_service="identitytheftgov", tv_service_label="identitytheft.gov"):
    """File a single FTC Identity Theft Report (max 5 accounts)."""
    from playwright.sync_api import sync_playwright

    print(f"Filing FTC Identity Theft Report with {len(accounts)} accounts...", file=sys.stderr)
    print(f"  Dry run: {dry_run}", file=sys.stderr)
    print(f"  2FA mode: {'TextVerified (auto)' if tv_client else 'Manual'}", file=sys.stderr)

    # Step 0: If using TextVerified, get a one-time SMS number
    tv_number = None
    if tv_client:
        tv_client.authenticate()
        tv_client.check_balance()
        svc_label = tv_service_label if tv_service == "servicenotlisted" else None
        tv_client.create_verification(service_name=tv_service, service_not_listed_name=svc_label)
        tv_number = tv_client.get_number()

    # Use TextVerified number as the phone (immutable — new dict)
    if tv_number:
        phone_digits = tv_number.lstrip("+")
        if phone_digits.startswith("1") and len(phone_digits) == 11:
            phone_digits = phone_digits[1:]
        effective_client = {**client, "phone": phone_digits}
        print(f"  Phone: TextVerified → {phone_digits}", file=sys.stderr)
    else:
        effective_client = client

    with sync_playwright() as p:
        launch_args = {"headless": False, "slow_mo": 400}
        # Note: Comet browser causes net::ERR_ABORTED on identitytheft.gov
        # Use standard Chromium instead
        # if os.path.isfile(COMET_PATH):
        #     launch_args["executable_path"] = COMET_PATH
        browser = p.chromium.launch(**launch_args)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        try:
            # Navigate to identitytheft.gov
            page.goto(FTC_URL, wait_until="networkidle", timeout=30000)
            print("  Page loaded", file=sys.stderr)
            _snap(page, "01_landing")

            # Wizard: landing → Yes → Credit card → Fraudulent → /form/TheftInformation
            _click_start(page)
            print("  Wizard complete → on Theft Details form", file=sys.stderr)
            _snap(page, "02_theft_details")

            # Step 1: Theft Details — add each fraudulent account
            for i, acct in enumerate(accounts, 1):
                _add_account(page, acct, i)
                print(f"  Account {i}/{len(accounts)}: {acct['company']}", file=sys.stderr)

            # Click Continue to move to Step 2: Your Information
            _click_continue(page)
            page.wait_for_timeout(2000)
            print("  → Step 2: Your Information", file=sys.stderr)

            # Step 2: Personal info + 2FA (all on same page, in page order)
            _fill_personal_info(page, effective_client, tv_client=tv_client)
            print("  Personal info + 2FA complete", file=sys.stderr)
            _snap(page, "05_personal_info_done")

            # Click Continue to leave the personal info page
            _click_continue(page)
            page.wait_for_timeout(2000)

            # Steps 3-5: Click through each step
            statement_filled = False
            for step_num in range(8):
                page.wait_for_timeout(1000)
                url = page.url.lower()

                # Detect page by URL (most reliable)
                if "/form/comments" in url or "/form/personalstatement" in url:
                    if not statement_filled:
                        print(f"  → Personal Statement page", file=sys.stderr)
                        _fill_personal_statement(page)
                        statement_filled = True
                    _click_continue(page)
                    page.wait_for_timeout(1500)
                    continue

                if "/form/review" in url or "/form/submit" in url:
                    print(f"  → Review page reached", file=sys.stderr)
                    break

                # Fallback: detect by visible textarea (Personal Statement is the only page with one)
                textarea = page.query_selector("textarea:visible")
                if textarea and not statement_filled:
                    print(f"  → Personal Statement (textarea detected)", file=sys.stderr)
                    _fill_personal_statement(page)
                    statement_filled = True
                    _click_continue(page)
                    page.wait_for_timeout(1500)
                    continue

                # Check if page has "Check the box to sign" — that's the review page
                body_text = page.inner_text("body")[:1000]
                if "check the box to sign" in body_text.lower() or "review and sign" in body_text.lower():
                    print(f"  → Review page reached", file=sys.stderr)
                    break

                # Otherwise just click Continue
                _click_continue(page)
                page.wait_for_timeout(1500)
                print(f"  → Step {step_num + 3}", file=sys.stderr)

            # Step 6: Review
            _click_continue(page)
            page.wait_for_timeout(2000)
            print("\n  Reached review stage.", file=sys.stderr)
            _snap(page, "08_review_page")

            # Check the perjury acknowledgment checkbox (required before submit)
            _check_perjury_box(page)
            _snap(page, "09_perjury_checked")

            if dry_run:
                print("  DRY RUN — pausing for review. Close browser when done.", file=sys.stderr)
                page.wait_for_timeout(600000)
            else:
                # Click Submit/Finalize
                for btn_text in ["Submit", "Finalize", "File Report", "Submit Report"]:
                    submit = page.query_selector(f"button:has-text('{btn_text}'):visible, a:has-text('{btn_text}'):visible")
                    if submit and submit.is_visible():
                        submit.click()
                        page.wait_for_timeout(5000)
                        print(f"  SUBMITTED (clicked '{btn_text}')", file=sys.stderr)
                        break
                else:
                    # Try locator
                    try:
                        page.locator("text=/submit|finalize/i").first.click(timeout=5000)
                        page.wait_for_timeout(5000)
                        print("  SUBMITTED (locator)", file=sys.stderr)
                    except Exception:
                        print("  Submit button not found", file=sys.stderr)

            _snap(page, "10_after_finalize")
            page.wait_for_timeout(3000)

            # Check if "Missing signature" modal appeared — dismiss and retry
            if _dismiss_modal(page):
                print("  [RETRY] Modal dismissed — re-checking perjury box and re-submitting", file=sys.stderr)
                _check_perjury_box(page)
                _snap(page, "10b_perjury_retry")
                page.wait_for_timeout(1000)
                for btn_text in ["Submit", "Finalize", "File Report"]:
                    submit = page.query_selector(f"button:has-text('{btn_text}'):visible, a:has-text('{btn_text}'):visible")
                    if submit and submit.is_visible():
                        submit.click()
                        page.wait_for_timeout(5000)
                        print(f"  RE-SUBMITTED (clicked '{btn_text}')", file=sys.stderr)
                        break

            page.wait_for_timeout(3000)
            _snap(page, "11_confirmation_page")

            # Try to capture the report / confirmation
            confirmation = _capture_report(page)
            _snap(page, "12_after_capture")

            result = {
                "status": "dry_run" if dry_run else "submitted",
                "accounts_filed": len(accounts),
                "timestamp": datetime.now().isoformat(),
                "confirmation": confirmation,
                "tv_number": tv_number,
            }

        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            result = {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.now().isoformat(),
                "tv_number": tv_number,
            }
            if tv_client:
                tv_client.report_issue()
            print("  Browser kept open for manual intervention.", file=sys.stderr)
            page.wait_for_timeout(300000)

        finally:
            if tv_client:
                tv_client.cancel()
            browser.close()

    return result


def _handle_sms_verification(page, tv_client):
    """Handle the FTC SMS 2FA verification.

    Flow on /form/Consumer after selecting Phone type = Mobile:
    1. "Confirmation Code" section appears with a dropdown
    2. Select "Text Message" from the dropdown
    3. Click "Get My Code" button — this sends the SMS
    4. A code input field appears
    5. Poll TextVerified for the code, enter it
    6. Continue is now enabled
    """
    page.wait_for_timeout(1000)

    # Step 1: Select "Text Message" from the Confirmation Code dropdown
    # The dropdown appears after Phone type = Mobile is selected
    code_method = page.query_selector("select:visible")
    sms_selected = False
    for sel in page.query_selector_all("select:visible"):
        opts = sel.query_selector_all("option")
        for opt in opts:
            if "text message" in opt.inner_text().strip().lower():
                val = opt.get_attribute("value")
                sel.select_option(value=val)
                sms_selected = True
                print("  Confirmation Code: Text Message selected", file=sys.stderr)
                break
        if sms_selected:
            break

    if not sms_selected:
        print("  No 'Text Message' option found in confirmation code dropdown", file=sys.stderr)
        return

    page.wait_for_timeout(500)

    # Step 2: Click "Get My Code" button
    get_code_btn = page.query_selector("button:has-text('Get My Code'):visible")
    if not get_code_btn:
        # Try alternate selectors
        get_code_btn = page.query_selector("button:has-text('Get my code'):visible, button:has-text('Send Code'):visible")

    if not get_code_btn or not get_code_btn.is_enabled():
        print("  'Get My Code' button not found or disabled", file=sys.stderr)
        return

    # Retry loop — handle "We could not send a code to your phone" by getting a new number
    max_phone_retries = 3
    for phone_attempt in range(max_phone_retries):
        get_code_btn.click()
        print(f"  'Get My Code' clicked (attempt {phone_attempt + 1})...", file=sys.stderr)

        # Poll for error message OR code input field (whichever appears first)
        phone_rejected = False
        for check in range(8):  # Check every 500ms for 4 seconds
            page.wait_for_timeout(500)

            # Check for error: "We could not send a code" — look everywhere
            error_detected = page.evaluate("""() => {
                const text = document.body.innerText.toLowerCase();
                return text.includes('could not send a code') ||
                       text.includes('unable to send') ||
                       text.includes('cannot send') ||
                       text.includes('try again');
            }""")

            if error_detected:
                phone_rejected = True
                print(f"  [WARN] Phone number rejected — getting new number...", file=sys.stderr)
                break

            # Check if code input appeared (success — SMS is being sent)
            code_field = _find_code_input(page)
            if code_field:
                break  # SMS is being sent, move on

        if phone_rejected and tv_client:
            # Cancel current number and get a new one
            tv_client.report_issue()
            tv_client.cancel()
            tv_client.create_verification()
            new_number = tv_client.get_number()
            new_digits = new_number.lstrip("+")
            if new_digits.startswith("1") and len(new_digits) == 11:
                new_digits = new_digits[1:]
            # Clear and re-fill the phone field
            phone_inp = page.query_selector("#primePhone")
            if phone_inp:
                phone_inp.fill("")
                page.wait_for_timeout(200)
                phone_inp.fill(new_digits)
                page.wait_for_timeout(500)
                print(f"  New phone: {new_digits}", file=sys.stderr)
            # Re-find the Get My Code button (may need to wait for it to re-enable)
            page.wait_for_timeout(1000)
            get_code_btn = page.query_selector("button:has-text('Get My Code'):visible") or page.query_selector("button:has-text('Get my code'):visible")
            if not get_code_btn:
                break
            continue
        elif phone_rejected:
            print("  No TV client — cannot retry with new number", file=sys.stderr)
            break
        else:
            # No error — SMS is being sent
            break

    # Step 3: Find the code input field that appears after clicking Get My Code
    code_input = _find_code_input(page)
    if not code_input:
        # Wait a bit more for it to appear
        page.wait_for_timeout(3000)
        code_input = _find_code_input(page)

    if not code_input:
        print("  Code input field not found after clicking Get My Code", file=sys.stderr)
        return

    # Step 4: Poll TextVerified for the code — retry with new number on timeout
    if tv_client:
        sms_received = False
        for sms_attempt in range(2):  # Try up to 2 numbers for SMS
            print(f"  Polling TextVerified for SMS code (attempt {sms_attempt + 1})...", file=sys.stderr)
            try:
                code = tv_client.poll_for_code()
                code_input.fill(str(code))
                page.wait_for_timeout(500)
                print(f"  SMS code entered: {code}", file=sys.stderr)
                sms_received = True
                break
            except TimeoutError:
                print(f"  [TV] SMS timeout on attempt {sms_attempt + 1}", file=sys.stderr)
                if sms_attempt == 0:
                    # First timeout — try a new number (might be rate-limited or blocked)
                    print("  [TV] Retrying with new phone number...", file=sys.stderr)
                    tv_client.report_issue()
                    tv_client.cancel()
                    tv_client.create_verification()
                    new_number = tv_client.get_number()
                    new_digits = new_number.lstrip("+")
                    if new_digits.startswith("1") and len(new_digits) == 11:
                        new_digits = new_digits[1:]
                    phone_inp = page.query_selector("#primePhone")
                    if phone_inp:
                        phone_inp.fill("")
                        page.wait_for_timeout(200)
                        phone_inp.fill(new_digits)
                        page.wait_for_timeout(500)
                        print(f"  New phone: {new_digits}", file=sys.stderr)
                    # Re-click Get My Code
                    gcb = page.query_selector("button:has-text('Get My Code'):visible")
                    if gcb:
                        gcb.click()
                        page.wait_for_timeout(3000)
                        print("  'Get My Code' re-clicked with new number", file=sys.stderr)
                    # Find code input again
                    code_input = _find_code_input(page)
                    if not code_input:
                        page.wait_for_timeout(3000)
                        code_input = _find_code_input(page)
                    if not code_input:
                        break

        if not sms_received:
            print("  [TV] All SMS attempts failed — falling back to manual entry", file=sys.stderr)
            print("  ENTER THE VERIFICATION CODE MANUALLY in the browser.", file=sys.stderr)
            print("  Press Enter in terminal when done.", file=sys.stderr)
            input("  Press Enter after manual code entry >>> ")
    else:
        print("  Waiting for manual SMS code entry...", file=sys.stderr)
        input("  Enter the code in the browser, then press Enter here >>> ")

    # Step 5: Click "Verify" button to confirm the code
    page.wait_for_timeout(2000)

    # Screenshot for debugging what's visible
    try:
        page.screenshot(path="/tmp/ftc_before_verify.png")
    except Exception:
        pass

    # Try multiple Playwright click strategies (NOT raw JS — Angular needs proper events)
    verified = False
    for strategy in [
        lambda: page.get_by_role("button", name="Verify").click(timeout=5000),
        lambda: page.locator("button:has-text('Verify')").first.click(timeout=5000),
        lambda: page.locator("text=Verify").first.click(timeout=5000),
        lambda: page.click("text=Verify", timeout=5000),
        lambda: page.locator("button", has_text="Verify").first.click(force=True, timeout=5000),
    ]:
        try:
            strategy()
            page.wait_for_timeout(3000)
            print("  Code verified", file=sys.stderr)
            verified = True
            break
        except Exception as e:
            continue

    if not verified:
        # Last resort: dispatch proper mouse events via JS
        try:
            page.evaluate("""() => {
                const els = document.querySelectorAll('button, a, span, div, input');
                for (const el of els) {
                    if (el.textContent.trim() === 'Verify' && el.offsetParent !== null) {
                        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                        return true;
                    }
                }
                return false;
            }""")
            page.wait_for_timeout(3000)
            print("  Code verified (dispatched click event)", file=sys.stderr)
        except Exception:
            print("  'Verify' button STILL not found — screenshot at /tmp/ftc_before_verify.png", file=sys.stderr)


def _find_code_input(page):
    """Find the SMS verification code input field."""
    selectors = [
        "input[name*='code']",
        "input[name*='verif']",
        "input[name*='otp']",
        "input[name*='pin']",
        "input[id*='code']",
        "input[id*='verif']",
        "input[id*='otp']",
        "input[aria-label*='code']",
        "input[aria-label*='verif']",
        "input[placeholder*='code']",
        "input[placeholder*='verif']",
        "input[type='tel'][maxlength]",
        "input[inputmode='numeric']",
    ]
    for sel in selectors:
        el = page.query_selector(sel)
        if el and el.is_visible():
            return el

    # Heuristic: look for a short text input near "verification" or "code" text
    labels = page.query_selector_all("label")
    for label in labels:
        label_text = label.inner_text().lower()
        if any(kw in label_text for kw in ["code", "verif", "otp", "pin"]):
            for_attr = label.get_attribute("for")
            if for_attr:
                inp = page.query_selector(f"#{for_attr}")
                if inp and inp.is_visible():
                    return inp

    return None


def _click_start(page):
    """Navigate the FTC wizard: landing → assistant → form pages."""
    page.wait_for_timeout(2000)

    # Click "Report Identity Theft" button on landing page
    btn = page.query_selector("button.idt-hero_primary-cta-btn")
    if btn and btn.is_visible():
        btn.click()
        page.wait_for_timeout(2000)
    else:
        # Fallback
        for text in ["Report Identity Theft", "Get Started"]:
            el = page.query_selector(f"button:has-text('{text}'), a:has-text('{text}')")
            if el and el.is_visible():
                el.click()
                page.wait_for_timeout(2000)
                break

    # Q1: "Did someone use your information?" → Yes
    yes_btn = page.query_selector("button:has-text('Yes')")
    if yes_btn and yes_btn.is_visible():
        yes_btn.click()
        page.wait_for_timeout(2000)

    # Q2: "What did the identity thief use your info for?" → Credit card accounts
    _click_label_containing(page, "Credit card accounts")
    page.wait_for_timeout(500)
    _click_continue(page)

    # Q3: "How was your info misused?" → open a fraudulent credit card account
    _click_label_containing(page, "open a fraudulent credit card")
    page.wait_for_timeout(500)
    _click_continue(page)

    # Now on /form/information — click Continue link to reach the actual form
    cont_link = page.query_selector("a[href='/form/TheftInformation']")
    if cont_link and cont_link.is_visible():
        cont_link.click()
        page.wait_for_timeout(3000)


def _click_label_containing(page, text):
    """Click a label whose text contains the given string (for Angular custom checkboxes)."""
    for label in page.query_selector_all("label"):
        if text.lower() in label.inner_text().strip().lower():
            label.click()
            return True
    return False


def _click_continue(page):
    """Click the Continue button (waits for it to become enabled)."""
    page.wait_for_timeout(500)
    for _ in range(10):
        btn = page.query_selector("button:has-text('Continue'):visible")
        if btn and btn.is_enabled():
            btn.click()
            page.wait_for_timeout(2000)
            return True
        page.wait_for_timeout(500)
    return False


def _dismiss_modal(page):
    """Dismiss any validation modal that pops up."""
    for modal_sel in [
        "#consumerValidationErrorModal.show button",
        "#tdOopsModal.show button",
        ".modal.show button:has-text('OK')",
        ".modal.show button:has-text('Close')",
        ".modal.show button:has-text('Got it')",
        ".modal.show .close",
    ]:
        modal = page.query_selector(modal_sel)
        if modal and modal.is_visible():
            # Log what the modal says before dismissing
            modal_body = page.query_selector(".modal.show .modal-body, .modal.show p")
            if modal_body:
                print(f"  [MODAL] {modal_body.inner_text().strip()[:200]}", file=sys.stderr)
            modal.click()
            page.wait_for_timeout(500)
            return True
    return False


def _select_by_label(page, selector, label_text):
    """Select a dropdown option by its visible label text (handles Angular index:value format)."""
    sel = page.query_selector(selector)
    if not sel or not sel.is_visible():
        return False
    try:
        sel.select_option(label=label_text)
        return True
    except Exception:
        pass
    # Fallback: find option by text content
    options = sel.query_selector_all("option")
    for opt in options:
        if label_text.lower() in opt.inner_text().strip().lower():
            val = opt.get_attribute("value")
            if val:
                try:
                    sel.select_option(value=val)
                    return True
                except Exception:
                    pass
    return False


def _fill_personal_info(page, client, tv_client=None):
    """Fill personal info on Step 2 (/form/Consumer) in page order.

    Page order:
      1. Name (fName, lName)
      2. Country (select USA — reveals address fields)
      3. Phone (primePhone) + Phone type (Mobile — reveals 2FA section)
      4. *** 2FA: select Text Message → Get My Code → enter code → Verify ***
      5. Email (eAddre, confEAddr)
      6. Filing for (myself)
      7. DOB (dobYear, dobMonth, dobDay)
      8. Address (stAddr, citay, stat, openZippy)
      9. Lived at address since (haveLivedYear, haveLivedMonth) — always 4 years back
      10. Changed since theft (No)
      11. Military (No)
    """
    page.wait_for_timeout(1500)

    def _fill(field_id, value):
        if not value or value in ("TV_AUTO", "N/A"):
            return
        inp = page.query_selector(f"#{field_id}")
        if inp and inp.is_visible():
            inp.fill(str(value))
            page.wait_for_timeout(200)

    # 1. Name
    _fill("fName", client.get("first_name", ""))
    _fill("lName", client.get("last_name", ""))

    # 2. Country — USA
    _select_by_label(page, "#country", "UNITED STATES")
    page.wait_for_timeout(1000)

    # 3. Phone + Phone type
    _fill("primePhone", client.get("phone", ""))
    _select_by_label(page, "#primePhoneType", "Mobile")
    page.wait_for_timeout(1000)
    print("  Phone type: Mobile selected", file=sys.stderr)

    # 4. 2FA — handled inline
    _handle_sms_verification(page, tv_client)

    # 5. Email
    _fill("eAddre", client.get("email", ""))
    _fill("confEAddr", client.get("email", ""))

    # 6. Filing for — myself
    myself = page.query_selector("#filingFor0")
    if myself:
        page.click("label[for='filingFor0']")
        page.wait_for_timeout(300)

    # 7. DOB
    dob = client.get("dob", "")
    if dob and dob != "N/A":
        parts = dob.replace("-", "/").split("/")
        if len(parts) == 3:
            if len(parts[0]) == 4:
                year, month, day = parts
            else:
                month, day, year = parts

            _select_by_label(page, "#dobYear", year)
            page.wait_for_timeout(500)

            month_names = ["", "January", "February", "March", "April", "May", "June",
                           "July", "August", "September", "October", "November", "December"]
            month_idx = int(month)
            if 1 <= month_idx <= 12:
                _select_by_label(page, "#dobMonth", month_names[month_idx])
            page.wait_for_timeout(500)

            _select_by_label(page, "#dobDay", str(int(day)))
            page.wait_for_timeout(300)

    # 8. Address + State + Zip
    _fill("stAddr", client.get("address", ""))
    _fill("citay", client.get("city", ""))

    state = client.get("state", "")
    state_names = {
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
    if state and state in state_names:
        _select_by_label(page, "#stat", state_names[state])
    page.wait_for_timeout(300)

    _fill("openZippy", client.get("zip", ""))

    # 9. Lived at address since — always 4 years back
    now = datetime.now()
    _select_by_label(page, "#haveLivedYear", str(now.year - 4))
    page.wait_for_timeout(300)
    month_names = ["", "January", "February", "March", "April", "May", "June",
                   "July", "August", "September", "October", "November", "December"]
    _select_by_label(page, "#haveLivedMonth", month_names[now.month])
    page.wait_for_timeout(300)

    # 10. Changed since theft — No
    no_changed = page.query_selector("#changedSinceTheftNo")
    if no_changed:
        page.click("label[for='changedSinceTheftNo']")
        page.wait_for_timeout(200)

    # 11. Military — No
    no_military = page.query_selector("#MilitaryNo")
    if no_military:
        page.click("label[for='MilitaryNo']")
        page.wait_for_timeout(200)

    print("  Personal info complete", file=sys.stderr)


def _select_theft_type(page):
    """No-op — theft type is now selected in _click_start wizard flow."""
    pass


def _add_account(page, acct, index):
    """Add a single fraudulent account on /form/TheftInformation.

    Real formcontrolname attrs:
      companyName — creditor/bank name
      noticeDateMM, noticeDateYY — when you first noticed (month/year selects)
      DoTMM, DoTYY — when account was opened (month/year selects)
      amountObtained — total fraudulent charges
      accountNumber — account number
      companyRep, companyPhone, companyEmail — company contact info (optional)
    """
    page.wait_for_timeout(1000)

    if index > 1:
        add_btn = page.query_selector("button:has-text('Add another')")
        if add_btn and add_btn.is_visible():
            add_btn.click()
            page.wait_for_timeout(1500)

    # Company/bank name
    inp = page.query_selector("#companyName") or page.query_selector("input[formcontrolname='companyName']")
    if inp and inp.is_visible():
        inp.fill(acct["company"])

    # When did you first notice? → always current month/year
    now = datetime.now()
    _select_dropdown(page, "noticeDateMM", str(now.month))
    _select_dropdown(page, "noticeDateYY", str(now.year))

    # When was the account opened?
    if acct.get("date_opened"):
        parts = acct["date_opened"].replace("-", "/").split("/")
        if len(parts) >= 2:
            if len(parts[0]) == 4:
                open_month, open_year = parts[1], parts[0]
            else:
                open_month, open_year = parts[0], parts[-1]
            _select_dropdown(page, "DoTMM", str(int(open_month)))
            _select_dropdown(page, "DoTYY", str(open_year))

    # Fraudulent amount
    if acct.get("balance"):
        balance_clean = acct["balance"].replace("$", "").replace(",", "")
        amt_inp = page.query_selector("#amountObtained") or page.query_selector("input[formcontrolname='amountObtained']")
        if amt_inp and amt_inp.is_visible():
            amt_inp.fill(balance_clean)

    # Account number — replace asterisks with "x"
    if acct.get("account_number"):
        acct_num = acct["account_number"].replace("*", "x")
        acct_inp = page.query_selector("#accountNumber") or page.query_selector("input[formcontrolname='accountNumber']")
        if acct_inp and acct_inp.is_visible():
            acct_inp.fill(acct_num)


def _select_dropdown(page, fc_or_id, value):
    """Select an option in a dropdown by formcontrolname or id."""
    sel = page.query_selector(f"select[formcontrolname='{fc_or_id}']") or page.query_selector(f"#{fc_or_id}")
    if sel and sel.is_visible():
        try:
            sel.select_option(value=value)
        except Exception:
            pass


def _check_perjury_box(page):
    """Check the perjury acknowledgment checkbox on the review page.

    DOM structure:
      <input type="checkbox" id="perjuryTaxCheckbox" class="hidden-check">
      <label for="perjuryTaxCheckbox"></label>  ← THIS is the clickable toggle
      <label for="perjuryCheckbox">I understand...</label>  ← text label (wrong for attr)
    """
    page.wait_for_timeout(1000)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(500)

    # Direct target: label[for='perjuryTaxCheckbox'] — the actual toggle
    try:
        page.click("label[for='perjuryTaxCheckbox']", timeout=5000)
        page.wait_for_timeout(500)
        print("  Perjury checkbox checked (exact label)", file=sys.stderr)
        return
    except Exception:
        pass

    # Fallback: click the checkbox input directly with force
    try:
        page.locator("#perjuryTaxCheckbox").check(force=True, timeout=5000)
        page.wait_for_timeout(500)
        print("  Perjury checkbox checked (force check)", file=sys.stderr)
        return
    except Exception:
        pass

    # Try clicking the label containing "I understand"
    for label in page.query_selector_all("label"):
        try:
            text = label.inner_text().strip().lower()
            if "i understand" in text or "false statements" in text or "knowingly" in text:
                label.click()
                page.wait_for_timeout(500)
                print("  Perjury checkbox checked", file=sys.stderr)
                return
        except Exception:
            continue

    # Try via checkbox input directly
    for cb in page.query_selector_all("input[type='checkbox']"):
        try:
            cb_id = cb.get_attribute("id") or ""
            # Check associated label
            if cb_id:
                label = page.query_selector(f"label[for='{cb_id}']")
                if label:
                    label_text = label.inner_text().strip().lower()
                    if "understand" in label_text or "false" in label_text:
                        label.click()
                        page.wait_for_timeout(500)
                        print("  Perjury checkbox checked (by label)", file=sys.stderr)
                        return
            # If no matching label found, check if it's the only unchecked checkbox
            if not cb.is_checked():
                cb.check()
                page.wait_for_timeout(500)
                print("  Perjury checkbox checked (direct)", file=sys.stderr)
                return
        except Exception:
            continue

    # Locator fallback
    try:
        page.locator("text=/I understand/i").first.click(timeout=3000)
        page.wait_for_timeout(500)
        print("  Perjury checkbox checked (locator)", file=sys.stderr)
        return
    except Exception:
        pass

    # JavaScript click on the checkbox input directly + dispatch change event
    try:
        result = page.evaluate("""() => {
            const checkboxes = document.querySelectorAll('input[type="checkbox"]');
            for (const cb of checkboxes) {
                if (!cb.checked) {
                    cb.checked = true;
                    cb.dispatchEvent(new Event('change', {bubbles: true}));
                    cb.dispatchEvent(new Event('input', {bubbles: true}));
                    cb.dispatchEvent(new Event('click', {bubbles: true}));
                    // Also try clicking the parent/label
                    const label = cb.closest('label') || cb.parentElement;
                    if (label) label.click();
                    return true;
                }
            }
            return false;
        }""")
        if result:
            page.wait_for_timeout(500)
            print("  Perjury checkbox checked (JS)", file=sys.stderr)
            return
    except Exception:
        pass

    # Force click via Playwright on the checkbox area
    try:
        page.locator("input[type='checkbox']").first.check(force=True, timeout=3000)
        page.wait_for_timeout(500)
        print("  Perjury checkbox checked (force check)", file=sys.stderr)
        return
    except Exception:
        pass

    # Strategy: scroll to bottom of page and click the checkbox area by text
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)
        # Click directly on the "I understand" text which is the label/clickable area
        page.click("text=I understand that knowingly", timeout=5000)
        page.wait_for_timeout(500)
        print("  Perjury checkbox checked (text click after scroll)", file=sys.stderr)
        return
    except Exception:
        pass

    # Last resort: click by coordinates near the checkbox
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(500)
        # Find the checkbox visual element and click it
        box = page.locator(".idt-checkbox-button, [class*='checkbox']").first
        box.scroll_into_view_if_needed()
        box.click(force=True, timeout=5000)
        page.wait_for_timeout(500)
        print("  Perjury checkbox checked (class selector click)", file=sys.stderr)
        return
    except Exception:
        pass

    print("  Perjury checkbox NOT checked — may need manual intervention", file=sys.stderr)


FTC_PERSONAL_STATEMENT = (
    "I AM A VICTIM OF THE EQUIFAX AND TRANSUNION DATA BREACHES AND AM REQUESTING "
    "THAT THESE FRAUDULENT ACCOUNTS BE DELETED FROM MY CREDIT REPORTS\n\n"
    "As per FCRA 605B I am demanding that these accounts be blocked and deleted in 4 days"
)


def _fill_personal_statement(page):
    """Fill the Personal Statement textarea on Step 5. Tries every possible method."""
    page.wait_for_timeout(2000)

    # Screenshot for debugging
    try:
        page.screenshot(path="/tmp/ftc_personal_statement.png")
    except Exception:
        pass

    # Strategy 1: Direct query_selector with wait loop
    for attempt in range(15):
        for sel in [
            "textarea:visible",
            "textarea[formcontrolname='personalStatement']",
            "#personalStatement",
            "textarea[name='personalStatement']",
            "textarea[id*='statement']",
            "textarea[id*='Statement']",
            "textarea",
        ]:
            ta = page.query_selector(sel)
            if ta:
                try:
                    ta.fill(FTC_PERSONAL_STATEMENT)
                    print("  Personal statement filled", file=sys.stderr)
                    return
                except Exception:
                    continue
        page.wait_for_timeout(500)

    # Strategy 2: Playwright locator API
    for strategy in [
        lambda: page.locator("textarea").first.fill(FTC_PERSONAL_STATEMENT, timeout=5000),
        lambda: page.get_by_role("textbox").first.fill(FTC_PERSONAL_STATEMENT, timeout=5000),
        lambda: page.get_by_label("Personal Statement").fill(FTC_PERSONAL_STATEMENT, timeout=5000),
        lambda: page.locator("[formcontrolname='personalStatement']").fill(FTC_PERSONAL_STATEMENT, timeout=5000),
    ]:
        try:
            strategy()
            print("  Personal statement filled (locator)", file=sys.stderr)
            return
        except Exception:
            continue

    # Strategy 3: JavaScript direct set
    try:
        filled = page.evaluate("""(text) => {
            const textareas = document.querySelectorAll('textarea');
            for (const ta of textareas) {
                ta.value = text;
                ta.dispatchEvent(new Event('input', {bubbles: true}));
                ta.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
            }
            return false;
        }""", FTC_PERSONAL_STATEMENT)
        if filled:
            print("  Personal statement filled (JS)", file=sys.stderr)
            return
    except Exception:
        pass

    # Strategy 4: Type character by character (last resort)
    try:
        ta = page.query_selector("textarea")
        if ta:
            ta.click()
            page.keyboard.type(FTC_PERSONAL_STATEMENT, delay=10)
            print("  Personal statement filled (keyboard type)", file=sys.stderr)
            return
    except Exception:
        pass

    print("  Personal statement textarea NOT FOUND — screenshot at /tmp/ftc_personal_statement.png", file=sys.stderr)


def _click_next(page):
    """Click Continue button or link. Handles validation modals."""
    for attempt in range(3):
        _dismiss_modal(page)
        try:
            btn = page.query_selector("button:has-text('Continue'):visible")
            if btn and btn.is_enabled():
                btn.click(timeout=5000)
                page.wait_for_timeout(2000)
                # Check if modal appeared after click
                if _dismiss_modal(page):
                    print(f"  [WARN] Validation error on Continue (attempt {attempt+1})", file=sys.stderr)
                    continue
                return
        except Exception:
            pass
        # Try link
        link = page.query_selector("a:has-text('Continue'):visible")
        if link:
            link.click()
            page.wait_for_timeout(2000)
            return
    page.keyboard.press("Enter")
    page.wait_for_timeout(2000)


def _capture_report(page):
    """Skip account creation, download FTC report PDF, capture report number."""
    page.wait_for_timeout(3000)

    # Skip account creation — look for "No thanks, submit without an account"
    for skip_text in ["No thanks, submit without an account", "No thanks", "no thanks", "submit without an account",
                       "Skip", "Continue without", "Dont create", "just download", "Download without", "No, thanks"]:
        skip_btn = page.query_selector(f"a:has-text('{skip_text}'), button:has-text('{skip_text}')")
        if skip_btn and skip_btn.is_visible():
            skip_btn.click()
            page.wait_for_timeout(3000)
            print("  Skipped account creation", file=sys.stderr)
            break

    # Also try clicking via locator for more flexible matching
    try:
        page.locator("text=/no.?thanks/i").first.click(timeout=3000)
        page.wait_for_timeout(2000)
        print("  Skipped account creation (locator)", file=sys.stderr)
    except Exception:
        pass

    # Handle "Are you sure?" modal (id=optOutConfirmModal) — click "submit without an account" INSIDE the modal
    page.wait_for_timeout(2000)
    try:
        # Target the button specifically inside the modal
        modal_btn = page.locator("#optOutConfirmModal >> text=submit without an account").first
        modal_btn.click(timeout=5000)
        page.wait_for_timeout(3000)
        print("  Confirmed: submit without an account (modal)", file=sys.stderr)
    except Exception:
        # Fallback: try clicking any visible element with that text
        try:
            page.locator("text=/submit without an account/i").first.click(timeout=5000)
            page.wait_for_timeout(3000)
            print("  Confirmed: submit without an account (locator)", file=sys.stderr)
        except Exception:
            # JS fallback inside modal
            page.evaluate("""() => {
                const modal = document.getElementById('optOutConfirmModal');
                if (modal) {
                    const links = modal.querySelectorAll('a, button');
                    for (const el of links) {
                        if (el.textContent.toLowerCase().includes('submit without')) {
                            el.click();
                            return true;
                        }
                    }
                }
                return false;
            }""")
            page.wait_for_timeout(3000)
            print("  Confirmed: submit without an account (JS modal)", file=sys.stderr)

    # Handle "Submitting without an account" page — click "Continue"
    page.wait_for_timeout(2000)
    try:
        page.locator("button:has-text('Continue'), a:has-text('Continue')").first.click(timeout=10000)
        page.wait_for_timeout(5000)
        print("  Clicked Continue on submit-without-account page", file=sys.stderr)
    except Exception:
        print("  Continue button not found on submit-without-account page", file=sys.stderr)

    # Extract FTC Report Number from page text
    report_number = None
    text = page.inner_text("body")
    for line in text.split("\n"):
        line = line.strip()
        if "report number" in line.lower() or "ftc report" in line.lower():
            # Extract the number
            import re
            match = re.search(r'(\d{6,12})', line)
            if match:
                report_number = match.group(1)
                print(f"  FTC Report Number: {report_number}", file=sys.stderr)
            else:
                report_number = line
            break

    # Download the report PDF — may trigger download or open new tab
    for dl_text in ["Download Report (PDF)", "Download Report", "Download", "Print",
                     "Save", "Download PDF", "Save PDF", "Print Report", "Get Report"]:
        dl_btn = page.query_selector(f"a:has-text('{dl_text}'), button:has-text('{dl_text}')")
        if dl_btn and dl_btn.is_visible():
            save_path = os.path.expanduser(f"~/Desktop/ftc_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf")

            # Strategy 1: expect_download
            try:
                with page.expect_download(timeout=10000) as download_info:
                    dl_btn.click()
                download = download_info.value
                download.save_as(save_path)
                print(f"  Report downloaded: {save_path}", file=sys.stderr)
                return {"report_number": report_number, "pdf_path": save_path, "url": page.url}
            except Exception:
                pass

            # Strategy 2: expect_popup (new tab with PDF)
            try:
                with page.expect_popup(timeout=10000) as popup_info:
                    dl_btn.click()
                popup = popup_info.value
                popup.wait_for_load_state("networkidle", timeout=15000)
                popup.pdf(path=save_path)
                popup.close()
                print(f"  Report downloaded (popup): {save_path}", file=sys.stderr)
                return {"report_number": report_number, "pdf_path": save_path, "url": page.url}
            except Exception:
                pass

            # Strategy 3: click and get the href to fetch directly
            try:
                href = dl_btn.get_attribute("href")
                if href:
                    import requests as dl_requests
                    full_url = href if href.startswith("http") else f"https://www.identitytheft.gov{href}"
                    # Get cookies from browser
                    cookies = page.context.cookies()
                    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                    resp = dl_requests.get(full_url, headers={"Cookie": cookie_str}, timeout=30)
                    if resp.status_code == 200 and len(resp.content) > 1000:
                        fd = os.open(save_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                        with os.fdopen(fd, "wb") as f:
                            f.write(resp.content)
                        print(f"  Report downloaded (direct fetch): {save_path}", file=sys.stderr)
                        return {"report_number": report_number, "pdf_path": save_path, "url": page.url}
            except Exception as e:
                print(f"  Download failed ({e}), trying print-to-PDF fallback", file=sys.stderr)
                break

    # Fallback: print page to PDF
    try:
        pdf_path = os.path.expanduser(f"~/Desktop/ftc_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf")
        page.pdf(path=pdf_path)
        print(f"  Report saved (print-to-PDF): {pdf_path}", file=sys.stderr)
        return {"report_number": report_number, "pdf_path": pdf_path, "url": page.url}
    except Exception:
        pass

    return {"report_number": report_number, "url": page.url}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="File FTC Identity Theft Report")
    parser.add_argument("--client", required=True, help="Client JSON file path")
    parser.add_argument("--audit-json", help="Credify audit JSON (for account data)")
    parser.add_argument("--dry-run", action="store_true", help="Fill form but don't submit")
    parser.add_argument("--tv-auto", action="store_true",
                        help="Use TextVerified for SMS 2FA (requires TEXTVERIFIED_API_KEY and TEXTVERIFIED_USERNAME env vars)")
    parser.add_argument("--tv-service", default="identitytheftgov",
                        help="TextVerified service name (default: identitytheftgov — $0.50)")
    parser.add_argument("--tv-service-label", default="identitytheft.gov",
                        help="Label for unlisted service (only used if --tv-service=servicenotlisted)")
    args = parser.parse_args()

    with open(args.client) as f:
        client_data = json.load(f)

    client = client_data.get("client", client_data)

    derog_accounts = []
    if args.audit_json:
        with open(args.audit_json) as f:
            audit = json.load(f)
        derog_accounts = [a for a in audit.get("raw_accounts", []) if a.get("is_derogatory")]
    elif "derogatory_accounts" in client_data:
        derog_accounts = client_data["derogatory_accounts"]

    if not derog_accounts:
        print("Error: No derogatory accounts found. Provide --audit-json or include in client JSON.", file=sys.stderr)
        sys.exit(1)

    # TextVerified setup
    tv_client = None
    if args.tv_auto:
        api_key = os.environ.get("TEXTVERIFIED_API_KEY")
        username = os.environ.get("TEXTVERIFIED_USERNAME")
        if not api_key or not username:
            print("Error: --tv-auto requires TEXTVERIFIED_API_KEY and TEXTVERIFIED_USERNAME env vars.", file=sys.stderr)
            sys.exit(1)
        tv_client = TextVerifiedClient(api_key, username)

    result = file_report(client, derog_accounts, dry_run=args.dry_run,
                         tv_client=tv_client, tv_service=args.tv_service,
                         tv_service_label=args.tv_service_label)

    # Save results log
    log_path = os.path.expanduser(f"~/Desktop/ftc_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nResults saved: {log_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
