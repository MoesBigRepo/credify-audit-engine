---
name: "credify-audit-engine"
description: "Credify Audit Engine — unified credit repair pipeline. Parses credit reports (HTML/PDF), generates Credit Repair Strategy HTML, dispute letters (.docx), PDF assembly (v1.4), FTC Identity Theft Reports, CFPB complaints, and optional LetterStream mailing. Supports 8 providers. Use when the user says: 'audit this report', 'credify', 'run credify', 'audit engine', 'generate strategy', 'file cfpb', 'file ftc', 'assemble pdf', or provides a credit report file for processing."
---

# Credify Audit Engine

Unified credit repair pipeline: parse → strategy → disputes → FTC → PDF assembly → CFPB → mail.

## Usage

```bash
# Strategy only
python3 credify.py report.pdf --open

# Strategy + dispute letters
python3 credify.py report.pdf --open --disputes

# Full pipeline (strategy + disputes + mail)
python3 credify.py report.pdf --open --disputes --mail

# PDF Assembly (merge ID, SSN, disputes, breach proof into v1.4 PDF)
python3 credify.py report.pdf --disputes --assemble --docs-dir ~/Desktop/client_docs/

# FTC Identity Theft Report — auto-batches >5 derogs, Mullvad VPN rotation, TextVerified SMS
python3 credify.py report.pdf --ftc --client client.json

# CFPB Complaint (browser automation, all 3 bureaus)
python3 credify.py report.pdf --disputes --cfpb --client client.json --dry-run

# Full pipeline: audit + disputes + assemble + FTC + CFPB
python3 credify.py report.pdf --disputes --assemble --ftc --cfpb --client client.json

# Debug: dump parsed JSON
python3 credify.py report.pdf --json /tmp/debug.json
```

## Standalone Tools

```bash
# PDF Assembler (standalone)
python3 assembler.py --dir ~/Desktop/client_docs/ --out ~/Desktop/Package.pdf
python3 assembler.py --manifest manifest.json --out ~/Desktop/Package.pdf

# FTC Filer (canonical — batch orchestrator, Mullvad rotation, auto-batches >5 derogs)
python3 ftc_batch_filer.py --client client.json --audit-json audit.json

# FTC Filer (single report — fast filer direct, caps at 5 derogs via --offset/--limit)
python3 ftc_fast_filer.py --client client.json --audit-json audit.json [--offset N --limit 5]

# FTC Filer (LEGACY — slow ~60s, no VPN rotation, kept only as fallback)
python3 ftc_filer.py --client client.json --audit-json audit.json --dry-run

# CFPB Filer (standalone)
python3 cfpb_filer.py --client client.json --pdf Package.pdf --bureau ALL --dry-run
```

## Client JSON Template

See `client_template.json` for the required format. Key fields:
- `client`: name, email, phone, address, DOB, SSN last 4
- `documents`: paths to ID front/back, SSN front/back, proof of address, breach proof, FTC report

## Supported Providers

| Provider | Format | Bureaus | Status |
|----------|--------|---------|--------|
| MyScoreIQ | HTML | 3 | Working |
| IdentityIQ | PDF | 3 | Working |
| Experian.com | PDF | 1 | Working |
| TransUnion.com | PDF | 1 | Working |
| MyFICO | PDF/HTML | 1-3 | Stub |
| SmartCredit | PDF/HTML | 3 | Stub |
| MyFreeScoreNow | PDF | 3 | Working |
| AnnualCreditReport | PDF | 1 | Stub |

## Architecture

Single-process pipeline — no subprocess calls, no intermediate disk I/O:

```
File → detect_provider → Parser.parse() → partition_accounts()
  → build_context() → render_html()
  → [generate_disputes()] → [assemble_pdf()] → [file_ftc()] → [file_cfpb()] → [send_mail()]
```

## Pipeline Stages

| Stage | Script | Automated? | Notes |
|-------|--------|-----------|-------|
| Parse + Classify | pipeline.py | Fully | 8 provider parsers |
| Strategy HTML | renderer.py | Fully | Score projections, auto/Chase qualification |
| Dispute Letters | disputes.py | Fully | .docx with legal body, 5 accounts per letter |
| PDF Assembly | assembler.py | Fully | v1.4 output, auto-discover from folder |
| FTC Report | ftc_batch_filer.py → ftc_fast_filer.py | Fully | Default entry point. Auto-batches >5 derogs into reports of 5, Mullvad CLI rotation between reports, TextVerified SMS 2FA, ~45-75s per report. Legacy ftc_filer.py kept as fallback only. |
| CFPB Complaint | cfpb_filer.py | Semi | Browser automation, pause for review |
| Mail | mailer.py | Fully | LetterStream API, certified mail |
