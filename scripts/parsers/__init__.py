"""Credit report parser registry."""
import os
import importlib
import pkgutil

PARSERS = []

def register(cls):
    """Decorator to register a parser class."""
    PARSERS.append(cls)
    return cls

def _extract_pdf_text_for_detect(file_path):
    """Extract text from first 5 pages of PDF for detection fallback."""
    try:
        import fitz
        doc = fitz.open(file_path)
        pages = min(5, doc.page_count)
        text = "\n".join(doc[i].get_text() for i in range(pages))
        doc.close()
        return text.encode("utf-8", errors="ignore")
    except Exception:
        return b""


def detect_provider(file_path):
    """Auto-detect provider from file content. Returns instantiated parser.
    Two-pass: raw bytes first (fast), then extracted text for compressed PDFs."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()
    with open(file_path, "rb") as f:
        content = f.read(16384)  # first 16KB for detection

    eligible = [p for p in PARSERS if not p.SUPPORTED_EXTENSIONS or ext in p.SUPPORTED_EXTENSIONS]
    scores = [(p, p.detect(file_path, content)) for p in eligible]
    scores.sort(key=lambda x: x[1], reverse=True)

    if scores and scores[0][1] >= 0.3:
        return scores[0][0]()

    # Pass 2: extract text from PDF first page (handles browser-printed PDFs)
    if ext == ".pdf":
        text_content = _extract_pdf_text_for_detect(file_path)
        if text_content:
            scores = [(p, p.detect(file_path, text_content)) for p in eligible]
            scores.sort(key=lambda x: x[1], reverse=True)
            if scores and scores[0][1] >= 0.3:
                return scores[0][0]()

    raise ValueError(f"Could not identify credit report provider for: {file_path}")

def validate_or_die(data):
    """Validate critical fields in parsed data. Raises ValueError on failure."""
    errors = []
    
    client = data.get("client", {})
    if not client.get("last_name") and not client.get("name"):
        errors.append("Client name is missing")
    
    scores = data.get("scores", [])
    if not scores:
        import sys
        print("Warning: No credit scores extracted from report — projection will use fallback base of 500", file=sys.stderr)
    for s in scores:
        val = s.get("value")
        if val is not None and not isinstance(val, (int, float)):
            errors.append(f"Score value is not numeric: {val}")

    accounts = data.get("raw_accounts", [])
    if not accounts:
        import sys
        print("Warning: No accounts extracted from report", file=sys.stderr)
    
    for i, a in enumerate(accounts):
        if not a.get("creditor"):
            errors.append(f"Account {i+1}: missing creditor name")
        bal = a.get("balance", "0")
        try:
            b = float(str(bal).replace("$", "").replace(",", ""))
            if b < 0:
                creditor_name = a.get('creditor', '?')
                errors.append(f"Account {i+1} ({creditor_name}): negative balance {bal}")
        except (ValueError, TypeError):
            pass
    
    if errors:
        raise ValueError("Validation failed:\n  - " + "\n  - ".join(errors))

# Auto-discover and import all parser modules in this package
_pkg_dir = os.path.dirname(__file__)
for _importer, _modname, _ispkg in pkgutil.iter_modules([_pkg_dir]):
    if _modname != "__init__":
        importlib.import_module(f".{_modname}", __package__)
