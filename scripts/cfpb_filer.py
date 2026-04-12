"""CFPB Complaint Filer — Playwright automation for consumerfinance.gov/complaint.

Files a consumer complaint with the CFPB, uploads supporting documentation.
Runs in HEADED mode so the operator can handle CAPTCHAs and verify before submit.

Usage:
    python3 cfpb_filer.py --client client.json --pdf package.pdf
    python3 cfpb_filer.py --client client.json --pdf package.pdf --dry-run
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

CFPB_URL = "https://www.consumerfinance.gov/complaint/"
COMET_PATH = "/Applications/Comet.app/Contents/MacOS/Comet"

# Product/issue mapping for credit report disputes
PRODUCT_TEXT = "Credit reporting or other personal consumer reports"
SUB_PRODUCT_TEXT = "Credit reporting"
ISSUE_TEXT = "Incorrect information on your report"
SUB_ISSUE_TEXT = "Information belongs to someone else"

# Bureau company names as they appear on CFPB
BUREAU_COMPANIES = {
    "EQF": "EQUIFAX, INC.",
    "EXP": "EXPERIAN INFORMATION SOLUTIONS INC.",
    "TU": "TRANS UNION, LLC",
}


def _build_narrative(client, derog_accounts):
    """Build complaint narrative from client data and derogatory accounts."""
    name = f"{client.get('first_name', '')} {client.get('last_name', '')}".strip()
    account_lines = []
    for i, acct in enumerate(derog_accounts, 1):
        creditor = acct.get("creditor", "Unknown")
        acct_num = acct.get("account_number", "")
        balance = acct.get("balance", "$0")
        line = f"{i}. {creditor}"
        if acct_num:
            line += f" (Account: {acct_num})"
        line += f" — Balance: {balance}"
        account_lines.append(line)

    accounts_text = "\n".join(account_lines)

    narrative = (
        f"I am a victim of identity theft resulting from the Equifax data breach of 2017, "
        f"which compromised the personal information of over 147 million Americans. "
        f"The following fraudulent accounts appear on my credit report and were not opened "
        f"or authorized by me:\n\n"
        f"{accounts_text}\n\n"
        f"I have filed an FTC Identity Theft Report documenting these fraudulent accounts. "
        f"I have previously disputed these items directly with the credit bureaus, but they "
        f"continue to report unverified information in violation of the Fair Credit Reporting "
        f"Act (FCRA), Section 611 (15 U.S.C. §1681i).\n\n"
        f"Under the FCRA, credit bureaus are required to conduct a reasonable investigation "
        f"within 30 days and either verify the accounts with proper documentation or delete "
        f"them entirely. The bureaus have failed to properly investigate my disputes and "
        f"continue to report these fraudulent accounts.\n\n"
        f"I am requesting that the CFPB investigate this matter and ensure these fraudulent "
        f"accounts are removed from my credit report immediately."
    )
    return narrative


def file_complaint(client, derog_accounts, pdf_path, bureau_code="EQF", dry_run=False):
    """File a CFPB complaint via browser automation.

    Args:
        client: dict with keys: first_name, last_name, email, phone, address, city, state, zip
        derog_accounts: list of derogatory account dicts from Credify
        pdf_path: path to assembled PDF package
        bureau_code: which bureau to complain about (EQF, EXP, TU)
        dry_run: if True, fill form but don't submit

    Returns:
        dict with status and confirmation details
    """
    from playwright.sync_api import sync_playwright

    company = BUREAU_COMPANIES.get(bureau_code, BUREAU_COMPANIES["EQF"])
    narrative = _build_narrative(client, derog_accounts)

    print(f"Filing CFPB complaint against {company}...", file=sys.stderr)
    print(f"  Dry run: {dry_run}", file=sys.stderr)

    with sync_playwright() as p:
        launch_args = {"headless": False, "slow_mo": 300}
        if os.path.isfile(COMET_PATH):
            launch_args["executable_path"] = COMET_PATH
        browser = p.chromium.launch(**launch_args)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        try:
            # Navigate to complaint form
            page.goto(CFPB_URL, wait_until="networkidle", timeout=30000)
            print("  Page loaded", file=sys.stderr)

            # Step 1: Select product
            _click_text(page, PRODUCT_TEXT)
            _wait_and_click_next(page)
            print("  Step 1: Product selected", file=sys.stderr)

            # Step 2: Select sub-product
            _click_text(page, SUB_PRODUCT_TEXT)
            _wait_and_click_next(page)
            print("  Step 2: Sub-product selected", file=sys.stderr)

            # Step 3: Select issue
            _click_text(page, ISSUE_TEXT)
            _wait_and_click_next(page)
            print("  Step 3: Issue selected", file=sys.stderr)

            # Step 4: Select sub-issue
            _click_text(page, SUB_ISSUE_TEXT)
            _wait_and_click_next(page)
            print("  Step 4: Sub-issue selected", file=sys.stderr)

            # Step 5: What happened (narrative)
            page.wait_for_selector("textarea", timeout=10000)
            page.fill("textarea", narrative)
            print(f"  Step 5: Narrative filled ({len(narrative)} chars)", file=sys.stderr)

            # Desired resolution
            desired = (
                "I want these fraudulent accounts removed from my credit report immediately. "
                "I also want the credit bureau to conduct a proper investigation as required "
                "by federal law, rather than simply parroting unverified information from furnishers."
            )
            resolution_textarea = page.query_selector_all("textarea")
            if len(resolution_textarea) > 1:
                resolution_textarea[1].fill(desired)
            _wait_and_click_next(page)
            print("  Step 5: Narrative + resolution filled", file=sys.stderr)

            # Step 6: Company name
            company_input = page.wait_for_selector(
                "input[type='text'][placeholder*='company'], input[type='text'][name*='company'], "
                "input[type='search'], input[aria-label*='company'], input[aria-label*='Company']",
                timeout=10000,
            )
            if company_input:
                company_input.fill(company)
                page.wait_for_timeout(1500)
                # Select from autocomplete dropdown
                suggestion = page.query_selector(f"text={company}")
                if suggestion:
                    suggestion.click()
                else:
                    page.keyboard.press("Enter")
            _wait_and_click_next(page)
            print(f"  Step 6: Company set to {company}", file=sys.stderr)

            # Step 7: Contact information
            _fill_contact(page, client)
            print("  Step 7: Contact info filled", file=sys.stderr)

            # Step 8: File upload
            if pdf_path and os.path.isfile(pdf_path):
                file_input = page.query_selector("input[type='file']")
                if file_input:
                    file_input.set_input_files(pdf_path)
                    page.wait_for_timeout(3000)
                    print(f"  Step 8: Uploaded {os.path.basename(pdf_path)}", file=sys.stderr)

            _wait_and_click_next(page)

            # Step 9: Review
            print("  Step 9: Review page", file=sys.stderr)

            if dry_run:
                print("\n  DRY RUN — pausing for review. Close browser when done.", file=sys.stderr)
                page.wait_for_timeout(600000)  # 10 min pause for review
            else:
                # Pause for human review before submit
                print("\n  REVIEW THE COMPLAINT. Press Enter in terminal to submit, or close browser to cancel.", file=sys.stderr)
                input("  Press Enter to submit >>> ")

                submit = page.query_selector("button[type='submit'], button:has-text('Submit')")
                if submit:
                    submit.click()
                    page.wait_for_timeout(5000)
                    print("  SUBMITTED", file=sys.stderr)

            # Capture confirmation
            confirmation = _capture_confirmation(page)
            result = {
                "status": "dry_run" if dry_run else "submitted",
                "bureau": bureau_code,
                "company": company,
                "timestamp": datetime.now().isoformat(),
                "confirmation": confirmation,
            }

        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            result = {
                "status": "error",
                "bureau": bureau_code,
                "error": str(e),
                "timestamp": datetime.now().isoformat(),
            }
            # Keep browser open for manual recovery
            print("  Browser kept open for manual intervention.", file=sys.stderr)
            page.wait_for_timeout(300000)

        finally:
            browser.close()

    return result


def _click_text(page, text):
    """Click an element containing the given text."""
    el = page.get_by_text(text, exact=False).first
    if el:
        el.click()
        page.wait_for_timeout(500)


def _wait_and_click_next(page):
    """Click the Next/Continue button."""
    page.wait_for_timeout(1000)
    for selector in [
        "button:has-text('Next')",
        "button:has-text('Continue')",
        "button[type='submit']:has-text('Next')",
        "a:has-text('Next')",
    ]:
        btn = page.query_selector(selector)
        if btn and btn.is_visible():
            btn.click()
            page.wait_for_timeout(2000)
            return
    # Fallback: press Enter
    page.keyboard.press("Enter")
    page.wait_for_timeout(2000)


def _fill_contact(page, client):
    """Fill contact information fields."""
    field_map = {
        "first_name": ["first_name", "firstName", "first-name"],
        "last_name": ["last_name", "lastName", "last-name"],
        "email": ["email", "emailAddress", "email_address"],
        "phone": ["phone", "phoneNumber", "phone_number", "telephone"],
        "address": ["address", "street", "address1", "street_address"],
        "city": ["city"],
        "state": ["state"],
        "zip": ["zip", "zipCode", "zip_code", "postal"],
    }

    for data_key, selector_hints in field_map.items():
        value = client.get(data_key, "")
        if not value:
            continue

        for hint in selector_hints:
            selectors = [
                f"input[name*='{hint}']",
                f"input[id*='{hint}']",
                f"input[aria-label*='{hint}']",
                f"input[placeholder*='{hint}']",
            ]
            for sel in selectors:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.fill(str(value))
                    break
            else:
                continue
            break

    # State dropdown
    state = client.get("state", "")
    if state:
        select = page.query_selector("select[name*='state'], select[id*='state']")
        if select:
            select.select_option(value=state)


def _capture_confirmation(page):
    """Try to capture confirmation number from the page."""
    text = page.inner_text("body")
    for line in text.split("\n"):
        line = line.strip()
        if "confirmation" in line.lower() or "complaint number" in line.lower():
            return line
    return page.url


def file_all_bureaus(client, derog_accounts, pdf_path, bureaus=None, dry_run=False):
    """File CFPB complaints against multiple bureaus.

    Args:
        bureaus: list of bureau codes to file against (default: all 3)
    """
    bureaus = bureaus or ["EQF", "EXP", "TU"]
    results = []
    for bureau in bureaus:
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"Filing against {BUREAU_COMPANIES.get(bureau, bureau)}", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)
        result = file_complaint(client, derog_accounts, pdf_path, bureau, dry_run)
        results.append(result)
    return results


def main():
    import argparse

    parser = argparse.ArgumentParser(description="File CFPB complaint via browser automation")
    parser.add_argument("--client", required=True, help="Client JSON file path")
    parser.add_argument("--pdf", required=True, help="Assembled PDF package path")
    parser.add_argument("--bureau", choices=["EQF", "EXP", "TU", "ALL"], default="ALL",
                       help="Which bureau to file against (default: ALL)")
    parser.add_argument("--dry-run", action="store_true", help="Fill form but don't submit")
    parser.add_argument("--audit-json", help="Path to Credify audit JSON (for account data)")
    args = parser.parse_args()

    with open(args.client) as f:
        client_data = json.load(f)

    client = client_data.get("client", client_data)

    # Load derogatory accounts from audit JSON if provided
    derog_accounts = []
    if args.audit_json:
        with open(args.audit_json) as f:
            audit = json.load(f)
        derog_accounts = [a for a in audit.get("raw_accounts", []) if a.get("is_derogatory")]
    elif "derogatory_accounts" in client_data:
        derog_accounts = client_data["derogatory_accounts"]

    if args.bureau == "ALL":
        results = file_all_bureaus(client, derog_accounts, args.pdf, dry_run=args.dry_run)
    else:
        results = [file_complaint(client, derog_accounts, args.pdf, args.bureau, args.dry_run)]

    # Save results log
    log_path = os.path.expanduser(f"~/Desktop/cfpb_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved: {log_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
