"""Minimal text->PDF writer for tests (no dependencies).

Generates a valid single-font PDF whose text pypdf can extract line-by-line, so
PDF-ingestion tests can be driven from our own .fountain fixtures at test time.
Rights hygiene: PDFs are written to temp dirs only — screenplay containers are
never committed (scripts/rights_guard.py blocks them repo-wide).
"""

from pathlib import Path


def _esc(s: str) -> str:
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def text_to_pdf(text: str, path, lines_per_page: int = 54) -> None:
    lines = text.splitlines()
    pages = [lines[i:i + lines_per_page] for i in range(0, len(lines), lines_per_page)] or [[]]

    n_pages = len(pages)
    page_nums = [4 + 2 * i for i in range(n_pages)]
    content_nums = [5 + 2 * i for i in range(n_pages)]

    body: list = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        ("2 0 obj\n<< /Type /Pages /Kids ["
         + " ".join(f"{n} 0 R" for n in page_nums)
         + f"] /Count {n_pages} >>\nendobj\n").encode(),
        b"3 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>\nendobj\n",
    ]
    for i, page_lines in enumerate(pages):
        parts = []
        y = 770
        for ln in page_lines:
            if ln.strip():
                parts.append(f"BT /F1 10 Tf 72 {y} Td ({_esc(ln)}) Tj ET")
            y -= 13
        stream = "\n".join(parts).encode("latin-1", "replace")
        body.append(
            (f"{page_nums[i]} 0 obj\n<< /Type /Page /Parent 2 0 R "
             f"/MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
             f"/Contents {content_nums[i]} 0 R >>\nendobj\n").encode()
        )
        body.append(
            f"{content_nums[i]} 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode()
            + stream + b"\nendstream\nendobj\n"
        )

    out = b"%PDF-1.4\n"
    offsets = []
    for chunk in body:
        offsets.append(len(out))
        out += chunk
    xref_pos = len(out)
    n_objs = len(body) + 1
    xref = f"xref\n0 {n_objs}\n0000000000 65535 f \n".encode()
    for off in offsets:
        xref += f"{off:010d} 00000 n \n".encode()
    out += xref
    out += f"trailer\n<< /Size {n_objs} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    Path(path).write_bytes(out)
