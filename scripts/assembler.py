"""PDF Assembler — merge client docs into a single upload-ready PDF v1.4.

Document order:
  1. ID front
  2. ID back
  3. SSN front
  4. SSN back
  5. Proof of address
  6. Dispute letters (.docx → PDF)
  7. Equifax data breach proof screenshot
  8. FTC report

Usage:
    python3 assembler.py --manifest client.json --out ~/Desktop/Client_Package.pdf
    python3 assembler.py --dir ~/Desktop/client_docs/ --out ~/Desktop/Client_Package.pdf
"""
import io
import os
import stat
import sys
import tempfile
from pathlib import Path

from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

# Required document slots (in assembly order)
DOC_SLOTS = [
    "id_front",
    "id_back",
    "ssn_front",
    "ssn_back",
    "proof_of_address",
    "dispute_letters",   # list of .docx paths
    "breach_proof",
    "ftc_report",
]

SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".gif", ".webp"}
SUPPORTED_DOC_EXTS = {".pdf", ".docx"} | SUPPORTED_IMAGE_EXTS


def _validate_path(path, label):
    """Validate file exists and is under home directory."""
    resolved = os.path.realpath(os.path.expanduser(path))
    home = os.path.expanduser("~")
    if not resolved.startswith(home):
        raise ValueError(f"{label} must be under home directory: {resolved}")
    if not os.path.isfile(resolved):
        raise FileNotFoundError(f"{label} not found: {resolved}")
    return resolved


def image_to_pdf_page(image_path):
    """Convert an image file to a single-page PDF (letter size, centered)."""
    img = Image.open(image_path)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    page_w, page_h = letter
    margin = 0.5 * inch

    available_w = page_w - 2 * margin
    available_h = page_h - 2 * margin

    img_w, img_h = img.size
    scale = min(available_w / img_w, available_h / img_h, 1.0)
    draw_w = img_w * scale
    draw_h = img_h * scale

    x = (page_w - draw_w) / 2
    y = (page_h - draw_h) / 2

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)

    # Save image to temp for reportlab
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        img.save(tmp, format="JPEG", quality=95)
        tmp_path = tmp.name

    try:
        c.drawImage(tmp_path, x, y, width=draw_w, height=draw_h)
        c.save()
    finally:
        os.unlink(tmp_path)

    buf.seek(0)
    return PdfReader(buf)


def docx_to_pdf(docx_path):
    """Convert .docx to PDF using docx2pdf (requires Word on macOS)."""
    import docx2pdf

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        docx2pdf.convert(docx_path, tmp_path)
        reader = PdfReader(tmp_path)
        # Read into memory so we can delete the temp file
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        buf = io.BytesIO()
        writer.write(buf)
        buf.seek(0)
        return PdfReader(buf)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def file_to_pdf_reader(file_path):
    """Convert any supported file to a PdfReader."""
    ext = Path(file_path).suffix.lower()

    if ext == ".pdf":
        return PdfReader(file_path)
    elif ext == ".docx":
        return docx_to_pdf(file_path)
    elif ext in SUPPORTED_IMAGE_EXTS:
        return image_to_pdf_page(file_path)
    else:
        raise ValueError(f"Unsupported file type: {ext} ({file_path})")


def assemble(manifest, output_path, pdf_version="1.4"):
    """Assemble all documents into a single PDF.

    Args:
        manifest: dict mapping slot names to file paths (or lists for dispute_letters)
        output_path: where to save the final PDF
        pdf_version: target PDF version string (default "1.4")

    Returns:
        dict with output path and page count
    """
    writer = PdfWriter()
    page_log = []

    for slot in DOC_SLOTS:
        paths = manifest.get(slot)
        if not paths:
            continue

        # Normalize to list
        if isinstance(paths, str):
            paths = [paths]

        for path in paths:
            path = _validate_path(path, slot)
            ext = Path(path).suffix.lower()
            if ext not in SUPPORTED_DOC_EXTS:
                print(f"  Skipping unsupported: {path}", file=sys.stderr)
                continue

            print(f"  Adding {slot}: {os.path.basename(path)}", file=sys.stderr)
            reader = file_to_pdf_reader(path)
            start_page = len(writer.pages)
            for page in reader.pages:
                writer.add_page(page)
            end_page = len(writer.pages)
            page_log.append({
                "slot": slot,
                "file": os.path.basename(path),
                "pages": f"{start_page + 1}-{end_page}",
            })

    if not writer.pages:
        raise ValueError("No documents were added to the assembly")

    # Set PDF version to 1.4 for upload compatibility
    writer.pdf_header = f"%PDF-{pdf_version}".encode()

    # Write with restrictive permissions
    output_path = os.path.realpath(os.path.expanduser(output_path))
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        writer.write(f)

    total_pages = len(writer.pages)
    file_size = os.path.getsize(output_path)
    print(f"  Assembled: {total_pages} pages, {file_size / 1024:.0f} KB → {output_path}", file=sys.stderr)

    return {
        "output": output_path,
        "pages": total_pages,
        "size_bytes": file_size,
        "pdf_version": pdf_version,
        "sections": page_log,
    }


def auto_discover(directory):
    """Auto-discover documents from a client folder by naming convention.

    Expected filenames (case-insensitive, any supported extension):
        id_front.*, id_back.*, ssn_front.*, ssn_back.*,
        proof_of_address.* or poa.*,
        breach_proof.* or equifax.*,
        ftc_report.* or ftc.*,
        Dispute_*.docx or Isolated_*.docx
    """
    directory = os.path.expanduser(directory)
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"Directory not found: {directory}")

    manifest = {}
    files = {f.lower(): f for f in os.listdir(directory)}

    slot_patterns = {
        "id_front": ["id_front", "id-front", "idfront"],
        "id_back": ["id_back", "id-back", "idback"],
        "ssn_front": ["ssn_front", "ssn-front", "ssnfront"],
        "ssn_back": ["ssn_back", "ssn-back", "ssnback"],
        "proof_of_address": ["proof_of_address", "proof-of-address", "poa", "address_proof"],
        "breach_proof": ["breach_proof", "breach-proof", "equifax", "equifax_breach"],
        "ftc_report": ["ftc_report", "ftc-report", "ftc"],
    }

    for slot, patterns in slot_patterns.items():
        for fname_lower, fname_real in files.items():
            stem = Path(fname_lower).stem
            ext = Path(fname_lower).suffix
            if stem in patterns and ext in SUPPORTED_DOC_EXTS:
                manifest[slot] = os.path.join(directory, fname_real)
                break

    # Dispute letters: collect all Dispute_*.docx and Isolated_*.docx
    dispute_files = sorted([
        os.path.join(directory, f)
        for f in os.listdir(directory)
        if (f.startswith("Dispute_") or f.startswith("Isolated_")) and f.endswith(".docx")
    ])
    if dispute_files:
        manifest["dispute_letters"] = dispute_files

    return manifest


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Assemble client docs into upload-ready PDF v1.4")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--manifest", help="JSON file with document paths")
    group.add_argument("--dir", help="Directory to auto-discover documents from")
    parser.add_argument("--out", required=True, help="Output PDF path")
    parser.add_argument("--version", default="1.4", help="Target PDF version (default: 1.4)")
    args = parser.parse_args()

    if args.manifest:
        with open(args.manifest) as f:
            manifest = json.load(f)
    else:
        manifest = auto_discover(args.dir)
        if not manifest:
            print("Error: No documents found in directory", file=sys.stderr)
            sys.exit(1)
        print(f"Auto-discovered {len(manifest)} document slots:", file=sys.stderr)
        for slot, paths in manifest.items():
            if isinstance(paths, list):
                print(f"  {slot}: {len(paths)} files", file=sys.stderr)
            else:
                print(f"  {slot}: {os.path.basename(paths)}", file=sys.stderr)

    result = assemble(manifest, args.out, pdf_version=args.version)
    print(f"\nAssembly complete:", file=sys.stderr)
    for section in result["sections"]:
        print(f"  [{section['pages']}] {section['slot']}: {section['file']}", file=sys.stderr)


if __name__ == "__main__":
    main()
