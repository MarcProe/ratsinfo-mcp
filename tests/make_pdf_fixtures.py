#!/usr/bin/env python3
"""Erzeugt die binären PDF-Fixtures für tests/fixtures (deterministisch, offline).

Aufruf:  python tests/make_pdf_fixtures.py
(ASCII-only Text, weil die minimalen PDF-Strings latin-1 kodiert werden;
Umlaut-Varianten werden als Transliteration „ae/ue/oe/AE" getestet — realistisch
für deutsche Behörden-PDFs.)
"""
from __future__ import annotations

import io
from pathlib import Path

HERE = Path(__file__).resolve().parent / "fixtures"


def minimal_pdf(pages_texts: list[str]) -> bytes:
    """Baut ein minimales, gültiges PDF: N Seiten, je 1 Text-Object, Type1 Helvetica."""
    objects: list[tuple[int, str]] = []
    n = len(pages_texts)
    font_id = 3 + 2 * n
    kids: list[str] = []
    for i, txt in enumerate(pages_texts):
        pid, cid = 3 + 2 * i, 4 + 2 * i
        kids.append(f"{pid} 0 R")

        def esc(s: str) -> str:
            return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

        ops = ["BT /F1 12 Tf 14 TL 50 800 Td"]
        for j, line in enumerate(txt.split("\n")):
            if j > 0:
                ops.append("T*")
            ops.append(f"({esc(line)}) Tj")
        ops.append("ET")
        stream = "\n".join(ops)
        objects.append((cid, f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\nendstream"))
        objects.append((
            pid,
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {cid} 0 R >>",
        ))
    objects.append((1, "<< /Type /Catalog /Pages 2 0 R >>"))
    objects.append((2, f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {n} >>"))
    objects.append((font_id, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))
    objects.sort(key=lambda t: t[0])

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for oid, body in objects:
        offsets[oid] = out.tell()
        out.write(f"{oid} 0 obj\n{body}\nendobj\n".encode("latin-1"))
    xref_pos = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for oid, _ in objects:
        out.write(f"{offsets[oid]:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    )
    return out.getvalue()


def main() -> None:
    HERE.mkdir(parents=True, exist_ok=True)

    small = minimal_pdf([
        "Antrag Nr. A 1 /XII.-GRUENE\n"
        "Antrag zur Kaeltschutzvorsorge\n"
        "Beratung: Erstellung und Umsetzung eines Kaeltschutzplans\n"
        "fuere unsere Stadt zu pruefen.",
        "Begruendung: Klimawandel erfordert Vorsorge.\n"
        "Beschluss: Die Stadtverwaltung wird beauftragt,\n"
        "einen Kaeltschutzplan aufzustellen.",
    ])
    (HERE / "small_2pages.pdf").write_bytes(small)

    big_pages: list[str] = []
    for i in range(30):
        lines = [f"Seite {i + 1} der Niederschrift."]
        lines += [f"Eintrag {j}: Sitzungsinhalt zur Testung des Volltextes." for j in range(8)]
        if i == 5:
            lines.append("Fundstelle: KAEUTESCHUTZPLAN wird hier erwaeht.")
        big_pages.append("\n".join(lines))
    (HERE / "big_30pages.pdf").write_bytes(minimal_pdf(big_pages))

    # Selbst-Check mit pypdf
    import pypdf

    r = pypdf.PdfReader(str(HERE / "small_2pages.pdf"))
    assert len(r.pages) == 2
    assert "Kaeltschutzplan" in r.pages[0].extract_text()
    r2 = pypdf.PdfReader(str(HERE / "big_30pages.pdf"))
    assert len(r2.pages) == 30
    assert "KAEUTESCHUTZPLAN" in r2.pages[5].extract_text()
    print(f"OK: {HERE / 'small_2pages.pdf'} (2 Seiten), {HERE / 'big_30pages.pdf'} (30 Seiten)")


if __name__ == "__main__":
    main()
