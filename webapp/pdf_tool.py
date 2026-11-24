"""Validate or rasterize a scan in an isolated, time-limited process."""
import math
from pathlib import Path
import sys

import pymupdf


def page_count(path, max_pages, max_raster_mp):
    with path.open("rb") as stream:
        if not stream.read(8).startswith(b"%PDF-"):
            raise ValueError("The selected file is not a PDF.")
        stream.seek(-min(path.stat().st_size, 4096), 2)
        if b"%%EOF" not in stream.read():
            raise ValueError("The PDF is incomplete or corrupted.")
    with pymupdf.open(path, filetype="pdf") as doc:
        if doc.needs_pass or doc.is_encrypted or doc.metadata.get("encryption"):
            raise ValueError("Encrypted PDFs are unsupported. Upload an unlocked copy.")
        if doc.is_repaired or not doc.is_pdf:
            raise ValueError("The PDF is malformed or damaged.")
        collection = doc.xref_get_key(doc.pdf_catalog(), "Collection")
        if collection[0] != "null" or not doc.page_count:
            raise ValueError("Upload a regular PDF with at least one page.")
        if doc.page_count > max_pages:
            raise ValueError(f"This PDF exceeds the limit of {max_pages} pages.")
        total_pixels = 0
        for page in doc:
            width, height = page.rect.width, page.rect.height
            if not all(math.isfinite(size) and 72 <= size <= 1440 for size in (width, height)):
                raise ValueError("One or more PDF pages have unsupported dimensions.")
            pixels = math.ceil(width * 300 / 72) * math.ceil(height * 300 / 72)
            total_pixels += pixels
            if pixels > 14_000_000 or total_pixels > max_raster_mp * 1_000_000:
                raise ValueError("The scanned page area is too large. Split the PDF into smaller batches.")
            page.get_displaylist()
        if pymupdf.TOOLS.mupdf_warnings():
            raise ValueError("The PDF contains damaged or unsupported page content.")
        return doc.page_count


def main():
    mode, filename, number, *extra = sys.argv[1:]
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    try:
        if mode == "validate":
            print(page_count(Path(filename), int(number), int(extra[0])))
        elif mode == "preview":
            with pymupdf.open(filename, filetype="pdf") as doc:
                image = doc[int(number) - 1].get_pixmap(dpi=110, colorspace=pymupdf.csGRAY)
                sys.stdout.buffer.write(image.tobytes("png"))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    except Exception:
        print("The PDF could not be opened or rendered.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
