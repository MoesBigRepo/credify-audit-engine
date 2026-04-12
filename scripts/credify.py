#!/usr/bin/env python3
"""
Credify Audit Engine — unified credit repair pipeline.

Usage:
    python3 credify.py report.pdf --open
    python3 credify.py report.html --open --disputes
    python3 credify.py report.pdf --disputes --mail --live
    python3 credify.py report.pdf --json ~/Desktop/debug.json

    # Full pipeline: audit + disputes + PDF assembly + FTC + CFPB
    python3 credify.py report.pdf --disputes --assemble --client client.json
    python3 credify.py report.pdf --disputes --ftc --cfpb --client client.json --dry-run
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))


def main():
    parser = argparse.ArgumentParser(
        description="Credify — Credit Repair Strategy Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python3 credify.py report.pdf --open\n"
               "  python3 credify.py report.html --open --disputes\n"
               "  python3 credify.py report.pdf --disputes --mail --live\n"
               "  python3 credify.py report.pdf --disputes --assemble --client client.json\n"
               "  python3 credify.py report.pdf --disputes --ftc --cfpb --client client.json\n",
    )
    parser.add_argument("file", help="Credit report file (HTML or PDF)")
    parser.add_argument("--open", action="store_true", help="Open strategy HTML in browser")
    parser.add_argument("--disputes", action="store_true", help="Generate dispute letters (.docx)")
    parser.add_argument("--mail", action="store_true", help="Mail disputes via LetterStream (requires --disputes)")
    parser.add_argument("--live", action="store_true", help="Actually send mail (default: dry-run)")
    parser.add_argument("--out", help="Custom strategy HTML output path")
    parser.add_argument("--json", help="Dump parsed JSON to path (for debugging)")
    parser.add_argument("--disputes-dir", help="Custom dispute letters output directory")

    # New pipeline stages
    parser.add_argument("--assemble", action="store_true",
                       help="Assemble PDF v1.4 package (ID, SSN, disputes, etc.)")
    parser.add_argument("--ftc", action="store_true",
                       help="File FTC Identity Theft Report via browser")
    parser.add_argument("--cfpb", action="store_true",
                       help="File CFPB complaint via browser")
    parser.add_argument("--client", help="Client JSON file (required for --assemble/--ftc/--cfpb)")
    parser.add_argument("--docs-dir", help="Directory with client docs for PDF assembly")
    parser.add_argument("--dry-run", action="store_true",
                       help="Fill forms but don't submit (for --ftc/--cfpb)")
    parser.add_argument("--score-drop", type=int, metavar="PTS",
                       help="Points lost due to collection insertion (e.g. 95)")
    parser.add_argument("--discount", type=int, metavar="AMT",
                       help="Dollar amount to discount from total (e.g. 1000)")
    parser.add_argument("--discount-note", metavar="NOTE",
                       help="Label for discount line (e.g. 'Friends & Family')")
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"Error: File not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    if args.mail and not args.disputes:
        print("Error: --mail requires --disputes", file=sys.stderr)
        sys.exit(1)

    if (args.ftc or args.cfpb) and not args.client:
        print("Error: --ftc and --cfpb require --client", file=sys.stderr)
        sys.exit(1)

    if args.assemble and not (args.client or args.docs_dir):
        print("Error: --assemble requires --client or --docs-dir", file=sys.stderr)
        sys.exit(1)

    from pipeline import run

    try:
        result = run(
            args.file,
            disputes=args.disputes,
            mail=args.mail,
            live=args.live,
            out_strategy=args.out,
            out_json=args.json,
            out_disputes=args.disputes_dir,
            score_drop=args.score_drop,
            discount=args.discount,
            discount_note=args.discount_note,
        )
    except (ValueError, FileNotFoundError, NotImplementedError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Browser open — UI concern, belongs in CLI not pipeline
    if args.open and result.get("strategy"):
        strategy = result["strategy"]
        if os.path.isfile(strategy):
            if sys.platform == "darwin":
                subprocess.run(["open", strategy])
            else:
                subprocess.run(["xdg-open", strategy])

    # Load client data if provided
    client_data = None
    if args.client:
        with open(args.client) as f:
            client_data = json.load(f)

    # Stage: PDF Assembly
    pdf_path = None
    if args.assemble:
        from assembler import assemble, auto_discover
        from classify import resolve_client_last, safe_filename

        if args.docs_dir:
            manifest = auto_discover(args.docs_dir)
        elif client_data and "documents" in client_data:
            manifest = client_data["documents"]
        else:
            # Auto-discover from disputes directory
            dispute_dir = args.disputes_dir or result.get("disputes_dir", "")
            if dispute_dir and os.path.isdir(dispute_dir):
                manifest = auto_discover(dispute_dir)
            else:
                print("Error: No document directory found for assembly", file=sys.stderr)
                sys.exit(1)

        # Add dispute letters if they were just generated
        dispute_paths = result.get("disputes", [])
        if dispute_paths:
            docx_files = [p if isinstance(p, str) else p[0] for p in dispute_paths]
            manifest["dispute_letters"] = docx_files

        last_name = "Client"
        if client_data:
            last_name = client_data.get("client", client_data).get("last_name", "Client")

        pdf_path = os.path.expanduser(f"~/Desktop/{safe_filename(last_name)}_Package.pdf")
        assembly_result = assemble(manifest, pdf_path)
        print(f"\nPDF Package: {pdf_path} ({assembly_result['pages']} pages)", file=sys.stderr)

    # Stage: FTC Filing — default routes through ftc_batch_filer.py which
    # internally uses ftc_fast_filer.py (fast Playwright filer with Mullvad
    # VPN rotation, TextVerified SMS 2FA, auto-download). The legacy ftc_filer.py
    # is kept only as a fallback — do not call it from here.
    if args.ftc:
        import subprocess as _sp
        import json as _json

        # Write temp audit JSON to /tmp/ — filer cleans it up after use
        json_path = args.json
        if not json_path or not os.path.exists(os.path.expanduser(json_path)):
            json_path = "/tmp/credify_audit_temp.json"
            audit_data = result.get("_data", {})
            if audit_data:
                with open(json_path, "w") as _f:
                    _json.dump(audit_data, _f, default=str)

        _script_dir = os.path.dirname(os.path.abspath(__file__))
        _batch = os.path.join(_script_dir, "ftc_batch_filer.py")
        _cmd = ["python3", _batch, "--client", args.client, "--audit-json", json_path]
        print(f"\nFTC: routing through ftc_batch_filer.py (Mullvad rotation, auto-batches >5 derogs)", file=sys.stderr)
        _rc = _sp.call(_cmd)
        print(f"\nFTC batch filer exit code: {_rc}", file=sys.stderr)

    # Stage: CFPB Filing
    if args.cfpb:
        from cfpb_filer import file_all_bureaus

        client = client_data.get("client", client_data)
        derog = [a for a in (result.get("_data", {}).get("raw_accounts", []))
                 if a.get("is_derogatory")]

        upload_pdf = pdf_path or args.client  # Use assembled PDF if available
        cfpb_results = file_all_bureaus(client, derog, upload_pdf, dry_run=args.dry_run)
        for r in cfpb_results:
            print(f"  CFPB {r['bureau']}: {r['status']}", file=sys.stderr)


if __name__ == "__main__":
    main()
