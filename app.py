# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 IFF AI
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, version 3. It is distributed WITHOUT ANY WARRANTY; see the
# LICENSE file. It uses PyMuPDF and PyMuPDF4LLM (Artifex), under the same license.

"""A PDF's pages as Markdown, and its figures as images, over HTTP.

Each page is read in layout order by PyMuPDF4LLM's layout model, with its
headings, bullets joined to their text and running headers and footers left out.
The pictures the layout model finds (diagrams, charts, photos) are cut out of
their pages as JPEGs, for a model that sees images to look at. A page with more
in pictures than a few cut-out figures carry (formulas, worked answers and
slides pasted in as images) is given whole instead, as one image of the page.
A table that is really a drawing (boxes and arrows), whose Markdown splits its
lines across columns, is cut out too, and its Markdown is replaced by its lines
of text as they're laid out.

    POST /refine    the PDF's bytes (Authorization: Bearer <PDF_REFINE_KEY>)
                    -> {"pages": [<a page's Markdown>, ...],
                        "figures": [{"page": <0-based index>, "image": <JPEG data: URL>,
                                     "whole": <true for the whole page, else absent>,
                                     "table": <true when it's (or has) a drawn table, else absent>}, ...],
                        "seconds": <float>}
    GET  /health    -> {"ok": true}

Environment: PDF_REFINE_KEY (required), PDF_REFINE_PORT (8790),
PDF_REFINE_WORKERS (2: PDFs read at once, a process each, on the CPU).
"""

import hmac
import json
import os
import re
import signal
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from concurrent.futures.process import BrokenProcessPool
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

# A picture smaller than this share of its page, or this many points on a side,
# is an icon, a logo or a bullet, not a figure to explain.
MIN_FIGURE_SHARE = 0.03
MIN_FIGURE_SIDE = 48
# Room kept around a picture, in points, for its labels and axis titles set as text beside it.
FIGURE_MARGIN = 12
# A figure's longest side, in pixels, and its JPEG quality: enough to read its labels.
FIGURE_MAX_SIDE = 1024
FIGURE_MAX_DPI = 200
FIGURE_QUALITY = 75
# A picture filling this share of its page (a slide that is one screenshot) is taken for a
# background by the layout model and left out; it's kept when its page has fewer letters
# of its own than this, and it's on no other page (a slide template's backdrop is).
FULL_PAGE_SHARE = 0.5
MAX_FULL_PAGE_TEXT = 200
# The largest ones are kept, page by page and in all.
MAX_PAGE_FIGURES = 2
MAX_FIGURES = 40
# A page whose pictures left out (past MAX_PAGE_FIGURES, or too small to be figures, such as a
# formula or a worked answer pasted in as an image) cover this share of it is given whole, as
# one image: their content is in no figure and, being pictures, not in the page's text either.
# Only the layout model's pictures count, so not decorations it passes over, nor a
# background (a picture filling FULL_PAGE_SHARE of the page). Its longest side, in pixels: small print in formulas read right
# at 1536 and not at 1024.
WHOLE_PAGE_LEFT_OUT = 0.15
WHOLE_PAGE_MAX_SIDE = 1536
# The grid, in points, on which the pictures' area is measured.
AREA_GRID = 4
# A table that is really a drawing (boxes joined by arrows, a note under them spanning their
# columns) comes out of the layout model as a table whose cells split its lines, a word cut
# across two columns: it's cut out too, as a figure marked "table". It's told by lines drawn
# askew in it (arrowheads, connectors; a ruled table has none) or by U+FFFD in its rows (glyphs
# the table reader couldn't map, as in a press release whose flow chart lost a footnote this way).
TABLE_ASKEW_LINES = 2
MISREAD = "�"
# Such a table's Markdown is replaced by its lines of text as they're laid out, top to bottom
# and left to right ("(전공시험*)" then "* 컴퓨터공학, 정보보호학 중 택1" under it), not cut into
# cells, so a "*" note comes right after the item it's for. Lines starting within this many
# points below a row's first one are on that row.
TABLE_ROW_GAP = 8
# A run of a Markdown table's lines.
TABLE_BLOCK = re.compile(r"(?:^[ \t]*\|[^\n]*(?:\n|$))+", re.M)
# The lines are read from the PDF opened again, as a task of its own: reading them right after the
# layout model killed the container's worker on that press release (not on Windows). If this
# task dies or takes longer than this, the tables keep their cells.
LAY_OUT_TIMEOUT_S = 60


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


def covered(rects) -> set:
    """The AREA_GRID cells the rectangles cover, so overlapping ones count once."""
    cells = set()
    for rect in rects:
        for x in range(int(rect.x0 // AREA_GRID), int(rect.x1 // AREA_GRID)):
            for y in range(int(rect.y0 // AREA_GRID), int(rect.y1 // AREA_GRID)):
                cells.add((x, y))
    return cells


def drawn_tables(page, chunk: dict) -> list:
    """The page's "table" layout boxes that are drawings rather than ruled tables (TABLE_ASKEW_LINES)."""
    import pymupdf

    tables = [pymupdf.Rect(box["bbox"]) for box in chunk.get("page_boxes") or [] if box.get("class") == "table"]
    if not tables:
        return []
    if any(MISREAD in line for line in chunk["text"].splitlines() if line.lstrip().startswith("|")):
        return tables
    drawings = page.get_drawings()
    drawn = []
    for table in tables:
        askew = sum(
            1
            for drawing in drawings
            if pymupdf.Rect(drawing["rect"]).intersects(table)
            for item in drawing["items"]
            if item[0] == "l" and abs(item[1].x - item[2].x) > 1 and abs(item[1].y - item[2].y) > 1
        )
        if askew >= TABLE_ASKEW_LINES:
            drawn.append(table)
    return drawn


def to_lay_out(page, chunk: dict, text: str) -> list[tuple[int, tuple]]:
    """The drawn tables of a page (drawn_tables) as (their Markdown block in `text`, their box), when its blocks are its tables'."""
    drawn = drawn_tables(page, chunk)
    if not drawn:
        return []
    tables = [tuple(box["bbox"]) for box in chunk.get("page_boxes") or [] if box.get("class") == "table"]
    # The Markdown's tables are taken to be the layout boxes', in order: not when there are more or fewer.
    if len(TABLE_BLOCK.findall(text)) != len(tables):
        return []
    drawn = [tuple(rect) for rect in drawn]
    return [(index, table) for index, table in enumerate(tables) if table in drawn]


def lay_out(data: bytes, wanted: dict) -> dict:
    """The laid-out lines of the tables in `wanted` ({page: [(block, box)]}), read from the PDF opened afresh."""
    import pymupdf

    found = {}
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for index, tables in wanted.items():
            page = doc[index]
            found[index] = {}
            for block, box in tables:
                rows = []
                lines = []
                for item in page.get_text("dict", clip=pymupdf.Rect(box))["blocks"]:
                    for line in item.get("lines", []):
                        text = "".join(span["text"] for span in line["spans"]).strip()
                        if text:
                            lines.append((line["bbox"][1], line["bbox"][0], text))
                for top, left, text in sorted(lines):
                    if not rows or top - rows[-1][0] > TABLE_ROW_GAP:
                        rows.append((top, []))
                    rows[-1][1].append((left, text))
                if rows:
                    found[index][block] = "\n".join(" ".join(text for _, text in sorted(row)) for _, row in rows)
    return found


def put_lines(text: str, lines: dict) -> str:
    """`text` with its Markdown tables numbered in `lines` ({block: laid-out lines}) replaced by them."""
    blocks = list(TABLE_BLOCK.finditer(text))
    for block, match in reversed(list(enumerate(blocks))):
        if block in lines:
            text = text[: match.start()] + lines[block] + "\n" + text[match.end() :]
    return text.strip()


def figures(doc, chunks: list[dict]) -> list[dict]:
    """The pictures of each page (its "picture" layout boxes) as JPEG data: URLs, in page order.

    A picture the same on several pages (a slide template's logo or background) is left out.
    A page that is mostly one picture and little text gets that picture too (FULL_PAGE_SHARE).
    A page whose pictures left out would cover WHOLE_PAGE_LEFT_OUT of it is one image instead.
    A table that is a drawing (drawn_tables) is a figure too, marked "table".
    """
    import base64
    import hashlib

    import pymupdf

    images = [page.get_image_info(hashes=True) for page in doc]
    pages_with = {}
    for index, infos in enumerate(images):
        for info in infos:
            pages_with.setdefault(info.get("digest"), set()).add(index)

    found = []
    for index, chunk in enumerate(chunks):
        page = doc[index]
        area = page.rect.width * page.rect.height
        rects = [pymupdf.Rect(box["bbox"]) for box in chunk.get("page_boxes") or [] if box.get("class") == "picture"]
        if not rects and len(re.sub(r"\s", "", clean(chunk["text"]))) < MAX_FULL_PAGE_TEXT:
            for info in images[index]:
                rect = pymupdf.Rect(info["bbox"]) & page.rect
                if area and rect.width * rect.height >= area * FULL_PAGE_SHARE and len(pages_with[info.get("digest")]) == 1:
                    rects.append(rect)
        tables = drawn_tables(page, chunk)
        boxes = []
        for rect, table in [(rect, False) for rect in rects] + [(rect, True) for rect in tables]:
            rect = rect & page.rect
            if rect.is_empty or min(rect.width, rect.height) < MIN_FIGURE_SIDE:
                continue
            if area and rect.width * rect.height < area * MIN_FIGURE_SHARE:
                continue
            boxes.append(((rect + (-FIGURE_MARGIN, -FIGURE_MARGIN, FIGURE_MARGIN, FIGURE_MARGIN)) & page.rect, table))
        boxes.sort(key=lambda box: box[0].width * box[0].height, reverse=True)
        if area:
            content = [rect & page.rect for rect in rects]
            content = [rect for rect in content if not rect.is_empty and rect.width * rect.height < area * FULL_PAGE_SHARE]
            left_out = covered(content) - covered([rect for rect, _ in boxes[:MAX_PAGE_FIGURES]])
            if len(left_out) * AREA_GRID * AREA_GRID >= area * WHOLE_PAGE_LEFT_OUT:
                dpi = min(FIGURE_MAX_DPI, WHOLE_PAGE_MAX_SIDE * 72 / max(page.rect.width, page.rect.height))
                pix = page.get_pixmap(dpi=max(36, int(dpi)), alpha=False)
                jpeg = pix.tobytes("jpg", jpg_quality=FIGURE_QUALITY)
                found.append({"page": index, "area": area, "jpeg": jpeg, "hash": hashlib.sha1(pix.samples).digest(), "whole": True, "table": bool(tables)})
                continue
        for rect, table in boxes[:MAX_PAGE_FIGURES]:
            dpi = min(FIGURE_MAX_DPI, FIGURE_MAX_SIDE * 72 / max(rect.width, rect.height))
            pix = page.get_pixmap(clip=rect, dpi=max(36, int(dpi)), alpha=False)
            if pix.width < 1 or pix.height < 1:
                continue
            jpeg = pix.tobytes("jpg", jpg_quality=FIGURE_QUALITY)
            found.append({"page": index, "area": rect.width * rect.height, "jpeg": jpeg, "hash": hashlib.sha1(pix.samples).digest(), "table": table})

    pages_of = {}
    for figure in found:
        pages_of.setdefault(figure["hash"], set()).add(figure["page"])
    kept = [figure for figure in found if len(pages_of[figure["hash"]]) == 1]
    if len(kept) > MAX_FIGURES:
        largest = sorted(kept, key=lambda figure: figure["area"], reverse=True)[:MAX_FIGURES]
        kept = [figure for figure in kept if figure in largest]
    return [
        {"page": figure["page"], "image": "data:image/jpeg;base64," + base64.b64encode(figure["jpeg"]).decode("ascii")}
        | ({"whole": True} if figure.get("whole") else {})
        | ({"table": True} if figure.get("table") else {})
        for figure in kept
    ]


def refine(data: bytes) -> tuple[list[str], list[dict], dict]:
    """The pages' Markdown, the figures, and the drawn tables to lay out (to_lay_out), by page."""
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
        # The text is what matters most: a figure that can't be cut out leaves them all out.
        try:
            found = figures(doc, chunks)
        except Exception as e:
            print(f"figures not cut out: {e}", flush=True)
            found = []
        texts = [clean(chunk["text"]) for chunk in chunks]
        wanted = {}
        for index, chunk in enumerate(chunks):
            try:
                tables = to_lay_out(doc[index], chunk, texts[index])
            except Exception as e:
                print(f"page {index + 1}'s drawn tables not found: {e}", flush=True)
                tables = []
            if tables:
                wanted[index] = tables
    return texts, found, wanted


class Handler(BaseHTTPRequestHandler):
    # Seconds a read or write on the socket may wait. Without it, a request whose body
    # stopped coming (the upload cut off) waited forever, and the container, counting it
    # as still being answered, never slept.
    timeout = 60
    pool: ProcessPoolExecutor
    restarting = threading.Lock()

    @classmethod
    def restart(cls) -> None:
        """A new pool for a broken one, once when several requests find it broken."""
        with cls.restarting:
            old = cls.pool
            if not getattr(old, "_broken", False):
                return
            cls.pool = start_pool()
            old.shutdown(wait=False, cancel_futures=True)

    def send_json(self, status: int, value: dict) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def laid_out(self, data: bytes, wanted: dict, pages: list[str]) -> list[str]:
        """The pages with their drawn tables as laid-out lines (lay_out); as they were if that fails."""
        try:
            lines = self.pool.submit(lay_out, data, wanted).result(timeout=LAY_OUT_TIMEOUT_S)
        except BrokenProcessPool as e:
            self.log_message("drawn tables left in cells, the workers are started again: %s", e)
            Handler.restart()
            return pages
        except Exception as e:
            self.log_message("drawn tables left in cells: %s", e or type(e).__name__)
            return pages
        self.log_message("laid out %d drawn tables", sum(len(found) for found in lines.values()))
        return [put_lines(page, lines[index]) if lines.get(index) else page for index, page in enumerate(pages)]

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
            pages, found, wanted = self.pool.submit(refine, data).result(timeout=TIMEOUT_S)
        except FutureTimeout:
            self.send_json(504, {"error": "timed out"})
            return
        except BrokenProcessPool as e:
            # A worker died reading it (killed, or crashed in PyMuPDF): the pool is made again,
            # or every PDF after it would fail the same way until the service restarts.
            self.log_message("refine failed, the workers are started again: %s", e)
            Handler.restart()
            self.send_json(422, {"error": str(e)[:300]})
            return
        except Exception as e:  # a PDF that can't be read
            self.log_message("refine failed: %s", e)
            self.send_json(422, {"error": str(e)[:300]})
            return
        if wanted:
            pages = self.laid_out(data, wanted, pages)
        seconds = round(time.perf_counter() - started, 2)
        self.log_message("refined %d pages (%d bytes), %d figures, in %.2fs", len(pages), length, len(found), seconds)
        self.send_json(200, {"pages": pages, "figures": found, "seconds": seconds})


def start_pool() -> ProcessPoolExecutor:
    # A process per PDF: PyMuPDF can't read several documents at once in one process.
    # A worker is replaced every 50 PDFs, so what it holds on to doesn't build up.
    return ProcessPoolExecutor(max_workers=WORKERS, initializer=_load, max_tasks_per_child=50)


def main() -> None:
    if not KEY:
        raise SystemExit("PDF_REFINE_KEY is not set")
    # As the container's first process (PID 1) it would ignore SIGTERM without a handler,
    # so the container never slept once idle and was paid for all day. It's only sent
    # when idle, so it exits at once (the workers go with the container).
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    Handler.pool = start_pool()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"pdf-refine listening on :{PORT} with {WORKERS} workers", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
