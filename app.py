# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 IFF AI
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, version 3. It is distributed WITHOUT ANY WARRANTY; see the
# LICENSE file. It uses PyMuPDF and PyMuPDF4LLM (Artifex), under the same license.

"""A PDF's pages as Markdown, over HTTP.

Each page is read in layout order by PyMuPDF4LLM's layout model, with its
headings, bullets joined to their text and running headers and footers left out.

    POST /refine    the PDF's bytes (Authorization: Bearer <PDF_REFINE_KEY>)
                    -> {"pages": [<a page's Markdown>, ...], "seconds": <float>}
    GET  /health    -> {"ok": true}

Environment: PDF_REFINE_KEY (required), PDF_REFINE_PORT (8790),
PDF_REFINE_WORKERS (2: PDFs read at once, a process each, on the CPU).
"""

import hmac
import json
import os
import re
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = os.environ.get("PDF_REFINE_KEY", "")
PORT = int(os.environ.get("PDF_REFINE_PORT", "8790"))
WORKERS = int(os.environ.get("PDF_REFINE_WORKERS", "2"))
# A little above the largest PDF the callers send (50MB).
MAX_BYTES = 60 * 1024 * 1024
# A 500-page PDF takes a few minutes; callers may give up sooner for shorter ones.
TIMEOUT_S = 600

# Text in a picture (labels of a diagram) comes between these, its lines split by <br>.
PICTURE_TEXT = re.compile(r"<!--\s*(?:Start|End) of picture text\s*-->")
SUP = re.compile(r"<sup>(.*?)</sup>", re.S)
SUB = re.compile(r"<sub>(.*?)</sub>", re.S)
TAG = re.compile(r"</?[a-zA-Z][^>]*>")
# "**BLOOD FL** **OW IN**": one bold run split in two.
SPLIT_BOLD = re.compile(r"\*\*[ \t]+\*\*")


def clean(markdown: str) -> str:
    """A page's Markdown as it's returned: no HTML, a line per line, no blank lines."""
    text = PICTURE_TEXT.sub("\n", markdown)
    text = re.sub(r"<br\s*/?>", " ", text)
    text = SUP.sub(r"^\1", text)
    text = SUB.sub(r"_\1", text)
    text = TAG.sub("", text)
    text = SPLIT_BOLD.sub(" ", text)
    return "\n".join(line.rstrip() for line in text.splitlines() if line.strip())


def _load():
    # Loads the layout model once per worker process, before its first PDF.
    import pymupdf4llm  # noqa: F401


def refine(data: bytes) -> list[str]:
    import pymupdf
    import pymupdf4llm

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            raise ValueError("the PDF is encrypted")
        chunks = pymupdf4llm.to_markdown(
            doc, page_chunks=True, header=False, footer=False, use_ocr=False, show_progress=False
        )
        if len(chunks) != doc.page_count:
            raise ValueError(f"{len(chunks)} pages read of {doc.page_count}")
    return [clean(chunk["text"]) for chunk in chunks]


class Handler(BaseHTTPRequestHandler):
    pool: ProcessPoolExecutor

    def send_json(self, status: int, value: dict) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(200, {"ok": True})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/refine":
            self.send_json(404, {"error": "not found"})
            return
        auth = self.headers.get("Authorization", "")
        if not hmac.compare_digest(auth.encode(), f"Bearer {KEY}".encode()):
            self.send_json(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BYTES:
            self.send_json(413, {"error": "no body or too large"})
            return
        data = self.rfile.read(length)
        if not data.startswith(b"%PDF"):
            self.send_json(400, {"error": "not a PDF"})
            return

        started = time.perf_counter()
        try:
            pages = self.pool.submit(refine, data).result(timeout=TIMEOUT_S)
        except FutureTimeout:
            self.send_json(504, {"error": "timed out"})
            return
        except Exception as e:  # a PDF that can't be read
            self.log_message("refine failed: %s", e)
            self.send_json(422, {"error": str(e)[:300]})
            return
        seconds = round(time.perf_counter() - started, 2)
        self.log_message("refined %d pages (%d bytes) in %.2fs", len(pages), length, seconds)
        self.send_json(200, {"pages": pages, "seconds": seconds})


def main() -> None:
    if not KEY:
        raise SystemExit("PDF_REFINE_KEY is not set")
    # A process per PDF: PyMuPDF can't read several documents at once in one process.
    # A worker is replaced every 50 PDFs, so what it holds on to doesn't build up.
    Handler.pool = ProcessPoolExecutor(max_workers=WORKERS, initializer=_load, max_tasks_per_child=50)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"pdf-refine listening on :{PORT} with {WORKERS} workers", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
