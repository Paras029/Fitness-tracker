"""PDF page splitting -- used to chunk a long lab report into a handful
of smaller PDFs before sending each to Gemini, instead of either sending
the whole document in one call (which truncates Gemini's output on a long
report) or one call per page (which burns through a free-tier quota fast
on a 40+ page document). See health_service.py's chunk-size heuristic for
how many pages per chunk / how many chunks that works out to.

Uses pypdf -- pure Python, no compiled extensions, in keeping with this
app's Termux-friendly dependency choices (see gemini/client.py's docstring
on why the official Gemini SDK is avoided for the same reason).
"""

import io

from pypdf import PdfReader, PdfWriter


def count_pdf_pages(pdf_bytes):
    """Returns the page count, or None if the bytes aren't a readable PDF
    (caller should fall back to treating it as a single unsplit upload)."""
    try:
        return len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    except Exception:
        return None


def split_pdf_pages(pdf_bytes, chunk_size):
    """Returns a list of PDF byte blobs, each containing up to
    `chunk_size` consecutive pages from the original, in order. Returns
    [pdf_bytes] unchanged (one chunk) if the file can't be read as a PDF
    or already fits within one chunk."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        page_count = len(reader.pages)
    except Exception:
        return [pdf_bytes]
    if page_count <= chunk_size:
        return [pdf_bytes]

    chunks = []
    for start in range(0, page_count, chunk_size):
        writer = PdfWriter()
        for i in range(start, min(start + chunk_size, page_count)):
            writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        chunks.append(buf.getvalue())
    return chunks
