"""Credify mailer — LetterStream API client for physical mail delivery.

Letters go TO bureaus (Experian, TransUnion, Equifax), FROM client (return address).
Dry-run by default — requires --live flag to actually send."""
import hashlib
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

from classify import resolve_client_name

API_URL = "https://www.letterstream.com/apis/index.php"


def _get_credentials():
    """Load API credentials from environment variables."""
    api_id = os.environ.get("LETTERSTREAM_API_ID")
    api_key = os.environ.get("LETTERSTREAM_API_KEY")
    if not api_id or not api_key:
        raise ValueError(
            "LetterStream credentials not configured. "
            "Set LETTERSTREAM_API_ID and LETTERSTREAM_API_KEY environment variables."
        )
    return api_id, api_key


def _compute_auth(api_key):
    """Compute LetterStream auth hash: MD5(base64(last6 + key + first6))."""
    t = str(int(time.time()))
    raw = t[-6:] + api_key + t[:6]
    h = hashlib.md5(base64.b64encode(raw.encode())).hexdigest()
    return t, h


def _parse_bureau_address(bureau_info):
    """Parse bureau dict into structured address fields for LetterStream."""
    name = bureau_info.get("name", "")
    raw_addr = bureau_info.get("address", "")
    lines = [l.strip() for l in raw_addr.split("\n") if l.strip()]

    street = lines[0] if lines else ""
    city, state, zipcode = "", "", ""
    if len(lines) >= 2:
        # "Allen, TX 75013" or "Chester, PA 19016" or "Atlanta, GA 30374-0256"
        import re
        m = re.match(r'([A-Za-z ]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)', lines[1])
        if m:
            city, state, zipcode = m.group(1).strip(), m.group(2), m.group(3)

    return {"first": name, "last": "", "address": street, "city": city, "state": state, "zip": zipcode}


def _build_sender(client):
    """Build sender (return address) from client data."""
    name = resolve_client_name(client)
    parts = name.split(None, 1)
    return {
        "first": parts[0] if parts else "Credify",
        "last": parts[1] if len(parts) > 1 else "Client",
        "address": client.get("address", ""),
        "city": client.get("city", ""),
        "state": client.get("state", ""),
        "zip": client.get("zip", ""),
    }


def send_letter(docx_path, recipient, sender, mail_type="certified"):
    """Send a single letter via LetterStream.

    Args:
        docx_path: Path to .docx file
        recipient: dict with keys: first, last, address, city, state, zip (the BUREAU)
        sender: dict with keys: first, last, address, city, state, zip (the CLIENT)
        mail_type: 'firstclass', 'certified', 'certnoerr'
    """
    api_id, api_key = _get_credentials()
    t, h = _compute_auth(api_key)

    doc_id = f"crd{int(time.time())}"
    to_str = ":".join([
        doc_id, recipient["first"], recipient["last"],
        recipient["address"], "", recipient["city"],
        recipient["state"], recipient["zip"],
    ])
    from_str = ":".join([
        sender["first"], sender["last"],
        sender["address"], "", sender["city"],
        sender["state"], sender["zip"],
    ])

    boundary = f"----CredifyBoundary{int(time.time())}"
    body = []

    for key, val in [("a", api_id), ("h", h), ("t", t),
                     ("job", f"credify-{int(time.time())}"),
                     ("to[]", to_str), ("from", from_str),
                     ("pages", "1"), ("mailtype", mail_type),
                     ("responseformat", "json")]:
        body.append(f"--{boundary}")
        body.append(f'Content-Disposition: form-data; name="{key}"')
        body.append("")
        body.append(val)

    body.append(f"--{boundary}")
    body.append(f'Content-Disposition: form-data; name="single_file"; filename="{os.path.basename(docx_path)}"')
    body.append("Content-Type: application/octet-stream")
    body.append("")

    body_bytes = "\r\n".join(body).encode()
    with open(docx_path, "rb") as f:
        file_data = f.read()
    body_bytes += b"\r\n" + file_data + f"\r\n--{boundary}--\r\n".encode()

    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        API_URL,
        data=body_bytes,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )

    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        result = json.loads(resp.read().decode())

    code = result.get("code") or result.get("status")
    if str(code) not in ("-100", "-200", "ok"):
        raise RuntimeError(f"LetterStream error (code {code}): {result}")

    return result


def batch_send(dispute_results, client, *, live=False, mail_type="certified"):
    """Send dispute letters to bureaus via physical mail.

    Args:
        dispute_results: list of (docx_path, bureau_info) tuples from generate_disputes().
            bureau_info: {"name": "Experian", "address": "P.O. Box 4500\nAllen, TX 75013"}
        client: parsed client dict (first_name, last_name, address, etc.)
        live: If False (default), dry-run — log what would be sent but skip HTTP.
        mail_type: LetterStream mail type.

    Returns:
        list of result dicts with status per letter.
    """
    sender = _build_sender(client)
    results = []

    for docx_path, bureau_info in dispute_results:
        # Skip FTC summary and entries without addresses
        if not bureau_info.get("address"):
            continue

        recipient = _parse_bureau_address(bureau_info)
        bureau_name = bureau_info.get("name", "Unknown")

        if not live:
            print(f"  [DRY-RUN] Would mail {os.path.basename(docx_path)} → {bureau_name}", file=sys.stderr)
            results.append({"path": docx_path, "bureau": bureau_name, "status": "dry-run"})
            continue

        try:
            r = send_letter(docx_path, recipient, sender, mail_type)
            results.append({"path": docx_path, "bureau": bureau_name, "status": "sent", "result": r})
            print(f"  Mailed: {os.path.basename(docx_path)} → {bureau_name}", file=sys.stderr)
        except ssl.SSLError as e:
            print(f"  SSL ERROR: {e} — aborting batch", file=sys.stderr)
            raise
        except urllib.error.URLError as e:
            results.append({"path": docx_path, "bureau": bureau_name, "status": "failed", "error": str(e)})
            print(f"  Failed: {os.path.basename(docx_path)} → {bureau_name} — {e}", file=sys.stderr)

    return results
