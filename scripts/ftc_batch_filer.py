"""FTC Batch Filer — canonical entry point for FTC identity-theft reports.

Takes a client.json and a credify audit JSON, auto-batches derogs into reports
of 5 (FTC's per-report cap), and calls ftc_fast_filer.py for each batch with
Mullvad CLI rotation between reports.

Usage:
    python3 ftc_batch_filer.py --client client.json --audit-json audit.json

Requirements:
    - TEXTVERIFIED_API_KEY + TEXTVERIFIED_USERNAME env vars (see ~/.zshenv)
    - mullvad CLI installed (/usr/local/bin/mullvad), VPN active
    - Playwright with Chromium
"""
import subprocess, sys, os, time, json, argparse, random
from datetime import datetime
from pathlib import Path

ACCOUNTS_PER_REPORT = 5
SCRIPT_DIR = Path(__file__).resolve().parent
FAST_FILER = SCRIPT_DIR / "ftc_fast_filer.py"

MULLVAD_COUNTRIES = ["us", "gb", "de", "nl", "ch", "se", "jp", "ca", "au", "sg", "fr", "es", "it", "no"]


def log(msg: str) -> None:
    print(f"[BATCH {datetime.now():%H:%M:%S}] {msg}", flush=True)


def mullvad_ip():
    """Return current Mullvad exit IP, or None if not routing through Mullvad."""
    try:
        import requests
        r = requests.get("https://am.i.mullvad.net/json", timeout=5)
        j = r.json()
        return j.get("ip") if j.get("mullvad_exit_ip") else None
    except Exception:
        return None


def rotate_mullvad():
    """Rotate Mullvad to a random country and return the new exit IP, or None on failure."""
    old_ip = mullvad_ip()
    country = random.choice(MULLVAD_COUNTRIES)
    log(f"Mullvad rotating -> {country.upper()} (was on {old_ip})")
    try:
        subprocess.run(["mullvad", "relay", "set", "location", country], capture_output=True, timeout=10, check=False)
        subprocess.run(["mullvad", "reconnect"], capture_output=True, timeout=10, check=False)
    except Exception as e:
        log(f"Mullvad CLI error: {e}")
        return None
    for _ in range(15):
        time.sleep(2)
        ip = mullvad_ip()
        if ip and ip != old_ip:
            log(f"Mullvad new IP: {ip} ({country.upper()})")
            return ip
    log("Mullvad rotation failed - IP didn't change after 30s")
    return None


def load_derogs(audit_path):
    with open(os.path.expanduser(audit_path)) as f:
        audit = json.load(f)
    return [a for a in audit.get("raw_accounts", []) if a.get("is_derogatory")]


def run_report(client_path, audit_path, offset, limit, report_idx, total_reports):
    """Spawn ftc_fast_filer.py for one batch. Returns subprocess exit code."""
    log(f"=== REPORT {report_idx + 1}/{total_reports} - derogs {offset + 1}-{offset + limit} ===")
    cmd = [
        sys.executable, str(FAST_FILER),
        "--client", client_path,
        "--audit-json", audit_path,
        "--offset", str(offset),
        "--limit", str(limit),
    ]
    env = os.environ.copy()
    env["FTC_BATCH_INDEX"] = str(report_idx + 1)
    start = time.time()
    rc = subprocess.call(cmd, env=env)
    elapsed = time.time() - start
    log(f"Report {report_idx + 1}/{total_reports} exit={rc} ({elapsed:.1f}s)")
    return rc


def main():
    parser = argparse.ArgumentParser(description="FTC batch filer - canonical entry point")
    parser.add_argument("--client", required=True, help="client.json path")
    parser.add_argument("--audit-json", required=True, help="credify audit JSON path")
    args = parser.parse_args()

    if not FAST_FILER.exists():
        print(f"Error: fast filer not found at {FAST_FILER}", file=sys.stderr)
        sys.exit(1)

    derogs = load_derogs(args.audit_json)
    if not derogs:
        print("Error: no derogatory accounts in audit JSON", file=sys.stderr)
        sys.exit(1)

    total_reports = (len(derogs) + ACCOUNTS_PER_REPORT - 1) // ACCOUNTS_PER_REPORT
    log(f"Total derogs: {len(derogs)} -> {total_reports} report(s)")

    results = []
    batch_start = time.time()
    for i in range(total_reports):
        offset = i * ACCOUNTS_PER_REPORT
        limit = min(ACCOUNTS_PER_REPORT, len(derogs) - offset)

        rc = run_report(args.client, args.audit_json, offset, limit, i, total_reports)
        results.append({"report": i + 1, "offset": offset, "limit": limit, "exit_code": rc})

        # Only rotate VPN if the report failed (likely SMS rate-limit), then retry
        if rc != 0 and i < total_reports - 1:
            log("Report failed — rotating Mullvad before next attempt")
            rotate_mullvad()
            time.sleep(2)

    elapsed = time.time() - batch_start
    log(f"\n=== BATCH COMPLETE - {elapsed:.0f}s ({elapsed / 60:.1f} min) ===")
    for r in results:
        status = "OK" if r["exit_code"] == 0 else f"FAIL(exit={r['exit_code']})"
        log(f"Report {r['report']}: {status} - derogs {r['offset'] + 1}-{r['offset'] + r['limit']}")

    # Exit nonzero if any report failed
    sys.exit(0 if all(r["exit_code"] == 0 for r in results) else 1)


if __name__ == "__main__":
    main()
