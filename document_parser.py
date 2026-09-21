"""
document_parser.py
V5-A — Inbound document/RFP parsing (free alternative to multimodal vision
extraction).

Most documents a lead would attach (an RFP, a spec sheet, a requirements
doc) are TEXT, not images — PDF and DOCX text extraction is free, local,
and completely reliable (PyMuPDF, python-docx), no AI model needed at all.
Only genuinely scanned/image-based documents need OCR (pytesseract, also
free and local) as a fallback.

Once we have plain text, it feeds directly into the EXISTING qualification
pipeline (qualify.py's extract_requirements()) — no new extraction logic
needed. This avoids depending on an unconfirmed/unreliable free vision
model, which is the more fragile approach the original design called for.

Fails soft throughout: if a library or the Tesseract OCR binary isn't
available, returns whatever text could be extracted (possibly empty)
rather than crashing — document parsing is an enrichment step, never a
pipeline blocker.
"""

import io

MAX_OCR_PAGES = 10  # cap OCR fallback cost/time on very long scanned documents


def _extract_pdf_text_direct(file_bytes: bytes) -> str:
    """Extracts text directly from a PDF's text layer (works for any text-based PDF)."""
    import pymupdf as fitz  # PyMuPDF (fitz is the deprecated import alias)

    text_parts = []
    with fitz.open(stream=file_bytes, filetype="pdf") as doc:
        for page in doc:
            text_parts.append(page.get_text())
    return "\n".join(text_parts).strip()


def _extract_pdf_text_via_ocr(file_bytes: bytes) -> str:
    """
    Fallback for scanned/image-only PDFs: renders each page as an image and
    runs OCR. Only used when direct text extraction comes back empty/tiny.
    """
    import pymupdf as fitz
    import pytesseract
    from PIL import Image

    text_parts = []
    with fitz.open(stream=file_bytes, filetype="pdf") as doc:
        for i, page in enumerate(doc):
            if i >= MAX_OCR_PAGES:
                break
            pix = page.get_pixmap(dpi=150)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            text_parts.append(pytesseract.image_to_string(img))
    return "\n".join(text_parts).strip()


def _extract_docx_text(file_bytes: bytes) -> str:
    """Extracts text from a Word document."""
    import docx

    doc = docx.Document(io.BytesIO(file_bytes))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n".join(paragraphs).strip()


def _extract_image_text(file_bytes: bytes) -> str:
    """OCRs a plain image file (PNG/JPG) directly."""
    import pytesseract
    from PIL import Image

    img = Image.open(io.BytesIO(file_bytes))
    return pytesseract.image_to_string(img).strip()


def extract_text_from_file(filename: str, file_bytes: bytes) -> dict:
    """
    Dispatches to the right extractor based on file extension.
    Returns {"text": str, "method": str, "error": str|None}.
    Never raises — always returns a result dict, with "text": "" on failure.
    """
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    try:
        if ext == "pdf":
            text = _extract_pdf_text_direct(file_bytes)
            if len(text) < 20:  # essentially empty -> likely a scanned PDF
                try:
                    ocr_text = _extract_pdf_text_via_ocr(file_bytes)
                    if len(ocr_text) > len(text):
                        return {"text": ocr_text, "method": "pdf_ocr", "error": None}
                except Exception as e:
                    print(f"[document_parser] OCR fallback failed (using whatever direct text was found): {e}")
            return {"text": text, "method": "pdf_direct", "error": None}

        elif ext == "docx":
            text = _extract_docx_text(file_bytes)
            return {"text": text, "method": "docx", "error": None}

        elif ext in ("png", "jpg", "jpeg"):
            text = _extract_image_text(file_bytes)
            return {"text": text, "method": "image_ocr", "error": None}

        else:
            return {"text": "", "method": "unsupported", "error": f"Unsupported file type: .{ext}"}

    except Exception as e:
        print(f"[document_parser] Extraction failed for {filename}: {e}")
        return {"text": "", "method": "failed", "error": str(e)}