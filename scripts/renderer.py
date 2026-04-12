"""Credify HTML renderer — Jinja2 template rendering only."""
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATE_DIR = Path(__file__).parent.parent / "templates"

_ENV = None

def _get_env():
    global _ENV
    if _ENV is None:
        _ENV = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
    return _ENV


def render(ctx, output_path):
    """Render template context to HTML file. Returns resolved output path."""
    template = _get_env().get_template("credit_strategy_v2_template.html")
    html = template.render(**ctx)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return str(out.resolve())
