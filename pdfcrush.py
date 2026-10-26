#!/usr/bin/env python3
"""
pdfcrush - force any PDF under a hard size limit (default 499,000 bytes).

Drop a file in, get one PDF out that is guaranteed as small as possible and,
whenever physically feasible, <= the target.

How it works
------------
1. Render every page to grayscale.
2. For every page, build a ladder of "rungs" (resolution x colour-depth):
       1-bit black/white (CCITT G4)   -> smallest
       2-bit gray (4 levels)
       4-bit gray (16 levels)         -> nicest
   Near-white pixels are snapped to pure white, near-black to pure black,
   and the grays in between are posterised, so every flat area becomes
   identical bits that compress to almost nothing.
3. Measure the real encoded size of every rung for every page.
4. Pick one rung per page: start everything at the cheapest rung, then spend
   the remaining byte budget on the pages that matter most (first page,
   image-heavy pages, dense pages) in order of "quality gained per byte".
5. Build the PDF, measure it, and if it is still too big, shrink the budget
   and pick again, until it fits.
"""
import argparse
import io
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import cv2
import img2pdf
import numpy as np
import pikepdf
import pypdfium2 as pdfium
from PIL import Image

DEFAULT_TARGET = 499_000          # bytes. < 500,000 and < 500*1024, so any "500KB" rule passes.
MASTER_DPI = 170                  # best quality we ever render at
MAX_MASTER_SIDE = 2600            # px cap for huge pages (posters, maps)

# (mode, dpi) rungs. Order does not matter, they are sorted by real size later.
RUNG_SPECS = (
    [("bw", d) for d in (10, 14, 20, 28, 40, 55, 72, 100, 130, 170)]
    + [("g2", d) for d in (55, 72, 100, 130)]
    + [("g4", d) for d in (72, 100, 130, 170)]
)
MODE_FACTOR = {"bw": 1.0, "g2": 1.3, "g4": 1.6}   # rough "quality" multiplier per mode
MODE_LEVELS = {"g2": 4, "g4": 16}


# --------------------------------------------------------------------------- #
# per-page work (runs in worker processes)
# --------------------------------------------------------------------------- #
def _render_master(pdf, index):
    page = pdf[index]
    w_pt, h_pt = page.get_size()
    scale = MASTER_DPI / 72.0
    longest = max(w_pt, h_pt) * scale
    if longest > MAX_MASTER_SIDE:
        scale *= MAX_MASTER_SIDE / longest
    bmp = page.render(scale=scale, grayscale=True)
    arr = np.array(bmp.to_numpy())
    if arr.ndim == 3:
        arr = arr[:, :, 0]
    return arr.astype(np.uint8), scale * 72.0, (w_pt, h_pt)


def _odd(n):
    n = int(n)
    return n if n % 2 == 1 else n + 1


def _encode_bw(gray, dpi, white_cut, photo_like):
    block = _odd(max(11, dpi * (0.35 if photo_like else 0.12)))
    c = 4 if photo_like else 10
    local = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block, c
    )
    black = (local == 0) & (gray < white_cut)       # darker than neighbourhood AND not "white enough"
    black |= gray < 55                              # solid dark areas stay black
    if dpi >= 100:                                  # despeckle scan noise
        n, lab, stats, _ = cv2.connectedComponentsWithStats(black.astype(np.uint8), connectivity=8)
        small = np.where(stats[:, cv2.CC_STAT_AREA] <= 3)[0]
        small = small[small != 0]
        if small.size:
            black[np.isin(lab, small)] = False
    img = Image.fromarray(~black)                   # bool array -> mode "1", True = white
    buf = io.BytesIO()
    img.save(buf, format="TIFF", compression="group4")
    return buf.getvalue()


def _encode_gray(gray, levels, white_cut, black_ref):
    black_cut = black_ref + (white_cut - black_ref) * 0.15
    v = np.clip((gray.astype(np.float32) - black_cut) / max(1.0, white_cut - black_cut), 0, 1)
    q = np.rint(v * (levels - 1)).astype(np.uint8)  # 0 = black ... levels-1 = pure white
    pal = []
    for i in range(levels):
        g = int(round(i * 255 / (levels - 1)))
        pal += [g, g, g]
    pimg = Image.frombytes("P", (q.shape[1], q.shape[0]), q.tobytes())
    pimg.putpalette(pal + [0] * (768 - len(pal)))
    buf = io.BytesIO()
    pimg.save(buf, format="PNG", optimize=True, bits=int(math.log2(levels)))
    return buf.getvalue()


def process_page(args):
    path, index = args
    pdf = pdfium.PdfDocument(path)
    master, master_dpi, size_pt = _render_master(pdf, index)
    pdf.close()

    sample = master[::3, ::3]
    p2, p97 = np.percentile(sample, [2, 97])
    white_ref = float(p97)
    white_cut = white_ref * 0.88 if white_ref >= 150 else white_ref * 0.95
    black_ref = float(p2)

    mid = float(np.mean((sample > 70) & (sample < white_cut)))      # share of "gray-ish" pixels
    ink = float(np.mean(sample < white_cut))                         # share of non-white pixels
    photo_like = mid > 0.22
    blank = ink < 0.0015

    weight = 1.0 + (1.0 if photo_like else 0.0) + min(1.0, ink * 6)
    if index == 0:
        weight += 2.0
    if blank:
        weight = 0.2

    rungs = {}
    for mode, dpi in RUNG_SPECS:
        dpi_eff = min(dpi, master_dpi)
        key = (mode, round(dpi_eff, 1))
        if key in rungs:
            continue
        if dpi_eff < master_dpi - 0.5:
            f = dpi_eff / master_dpi
            small = cv2.resize(
                master,
                (max(8, int(master.shape[1] * f)), max(8, int(master.shape[0] * f))),
                interpolation=cv2.INTER_AREA,
            )
        else:
            small = master
        if mode == "bw":
            data = _encode_bw(small, dpi_eff, white_cut, photo_like)
            kind = "tiff"
        else:
            data = _encode_gray(small, MODE_LEVELS[mode], white_cut, black_ref)
            kind = "png"
        rungs[key] = (MODE_FACTOR[mode] * dpi_eff, len(data), data, kind, mode, dpi_eff)

    # keep only the Pareto front: more bytes must buy more quality
    ordered = sorted(rungs.values(), key=lambda r: (r[1], -r[0]))
    front, best = [], -1.0
    for r in ordered:
        if r[0] > best:
            front.append(r)
            best = r[0]
    return index, weight, size_pt, front


# --------------------------------------------------------------------------- #
# choosing rungs under a byte budget
# --------------------------------------------------------------------------- #
def choose(pages, budget):
    """pages: list of (weight, [rungs sorted by size asc]). Returns chosen rung index per page."""
    pick = [0] * len(pages)
    total = sum(p[1][0][1] for p in pages)
    while True:
        best, best_u = None, 0.0
        for i, (w, rungs) in enumerate(pages):
            j = pick[i]
            if j + 1 >= len(rungs):
                continue
            extra = rungs[j + 1][1] - rungs[j][1]
            if total + extra > budget:
                continue
            gain = math.log(rungs[j + 1][0] / rungs[j][0])
            u = w * gain / max(extra, 1)
            if u > best_u:
                best, best_u = i, u
        if best is None:
            break
        total += pages[best][1][pick[best] + 1][1] - pages[best][1][pick[best]][1]
        pick[best] += 1
    return pick, total


def build_pdf(pages, sizes, pick, out_path):
    images = [pages[i][1][pick[i]][2] for i in range(len(pages))]
    it = iter(sizes)

    def layout(_w, _h, _dpi):
        pw, ph = next(it)
        return pw, ph, pw, ph

    raw = img2pdf.convert(images, layout_fun=layout)
    with pikepdf.open(io.BytesIO(raw)) as pdf:
        pdf.save(
            out_path,
            compress_streams=True,
            recompress_flate=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
        )
    return os.path.getsize(out_path)


# --------------------------------------------------------------------------- #
# main entry
# --------------------------------------------------------------------------- #
def compress(src, dst, target=DEFAULT_TARGET, workers=None, log=print):
    t0 = time.time()
    orig = os.path.getsize(src)
    try:
        n = len(pdfium.PdfDocument(src))
    except Exception as e:
        raise RuntimeError(f"Could not open PDF (corrupt or password protected?): {e}")
    if n == 0:
        raise RuntimeError("PDF has no pages")
    log(f"input: {orig/1e6:.2f} MB, {n} pages, target <= {target/1000:.0f} KB")

    workers = workers or max(1, (os.cpu_count() or 2))
    jobs = [(src, i) for i in range(n)]
    if workers == 1 or n == 1:
        results = [process_page(j) for j in jobs]
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(process_page, jobs, chunksize=1))
    results.sort(key=lambda r: r[0])
    sizes = [r[2] for r in results]
    pages = [(r[1], r[3]) for r in results]
    log(f"analysed all pages in {time.time()-t0:.1f}s")

    floor = sum(p[1][0][1] for p in pages)
    budget = target - 1500 - 450 * n          # first guess at PDF overhead
    result_size = None
    for attempt in range(1, 9):
        pick, est = choose(pages, budget)
        result_size = build_pdf(pages, sizes, pick, dst)
        log(f"pass {attempt}: {result_size/1000:.1f} KB (budget {budget/1000:.1f} KB)")
        if result_size <= target:
            break
        budget -= (result_size - target) + 1000   # overshoot -> tighten and retry
        if budget < floor and attempt > 1:
            pick = [0] * n
            result_size = build_pdf(pages, sizes, pick, dst)
            break

    ok = result_size <= target
    kinds = {}
    for i in range(n):
        r = pages[i][1][pick[i]]
        kinds[f"{r[4]}@{r[5]:.0f}dpi"] = kinds.get(f"{r[4]}@{r[5]:.0f}dpi", 0) + 1
    log(f"done in {time.time()-t0:.1f}s: {orig/1e6:.2f} MB -> {result_size/1000:.1f} KB "
        f"({'OK' if ok else 'COULD NOT REACH TARGET'}); page quality mix: {kinds}")
    return ok, result_size


def main():
    ap = argparse.ArgumentParser(description="Force a PDF under a size limit.")
    ap.add_argument("input")
    ap.add_argument("-o", "--output")
    ap.add_argument("-t", "--target-kb", type=float, default=DEFAULT_TARGET / 1000,
                    help="target in KB (1 KB = 1000 bytes). default 499")
    ap.add_argument("-j", "--workers", type=int, default=None)
    a = ap.parse_args()
    out = a.output or os.path.splitext(a.input)[0] + "_small.pdf"
    ok, _ = compress(a.input, out, int(a.target_kb * 1000), a.workers)
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
