#!/usr/bin/env python3
"""
Download the AI models once (Python standard library only).

    python3 backend/scripts/fetch_models.py                 # into backend/models/
    python3 backend/scripts/fetch_models.py --dest /opt/models

Files that already exist with the right size are skipped, so Docker builds reuse
what you downloaded and only fetch what's missing. Each model's licence is
recorded in <dest>/MODELS.md.
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.request
from pathlib import Path

# name -> (relative path, url, size in bytes, licence, notes)
MODELS = [
    (
        "D-FINE-S (Objects365→COCO) object detector — default, most accurate",
        "dfine/dfine_s_obj2coco.onnx",
        "https://huggingface.co/onnx-community/dfine_s_obj2coco-ONNX/resolve/main/onnx/model.onnx",
        41535197,
        "Apache-2.0 code; Objects365-pretrained weights are NOT assumed commercially cleared (D-FINE README)",
    ),
    (
        "D-FINE-S (COCO only) object detector — for commercial use (DFINE_WEIGHTS=coco)",
        "dfine/dfine_s_coco.onnx",
        "https://huggingface.co/onnx-community/dfine_s_coco-ONNX/resolve/main/onnx/model.onnx",
        41535197,
        "Apache-2.0, trained on COCO only",
    ),
    (
        "InsightFace SCRFD-10G face detector (buffalo_l)",
        "insightface/det_10g.onnx",
        "https://huggingface.co/immich-app/buffalo_l/resolve/main/detection/model.onnx",
        16923827,
        "InsightFace model licence: NON-COMMERCIAL research use only",
    ),
    (
        "InsightFace ArcFace R50 (WebFace600K) face recognizer (buffalo_l)",
        "insightface/w600k_r50.onnx",
        "https://huggingface.co/immich-app/buffalo_l/resolve/main/recognition/model.onnx",
        174383860,
        "InsightFace model licence: NON-COMMERCIAL research use only",
    ),
]


def fetch(dest: Path) -> int:
    failures = 0
    for title, relative, url, size, licence in MODELS:
        path = dest / relative
        if path.is_file() and path.stat().st_size == size:
            print(f"ok      {relative}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(path.suffix + ".part")
        started = time.time()
        for attempt in range(1, 6):
            try:
                print(f"get     {relative} ({size / 1e6:.0f} MB, try {attempt})", flush=True)
                urllib.request.urlretrieve(url, partial)
                if partial.stat().st_size != size:
                    raise OSError(f"size {partial.stat().st_size} != {size}")
                partial.replace(path)
                print(f"done    {relative} in {time.time() - started:.0f}s", flush=True)
                break
            except Exception as exc:  # network hiccup: retry
                print(f"retry   {relative}: {exc}", flush=True)
                time.sleep(3 * attempt)
        else:
            failures += 1
            partial.unlink(missing_ok=True)
    with (dest / "MODELS.md").open("w", encoding="utf-8") as notes:
        notes.write("# AI models in this folder\n\n| Model | File | Licence | Source |\n|---|---|---|---|\n")
        for title, relative, url, _size, licence in MODELS:
            notes.write(f"| {title} | {relative} | {licence} | {url} |\n")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dest", default=str(Path(__file__).resolve().parent.parent / "models"))
    failures = fetch(Path(parser.parse_args().dest))
    if failures:
        print(f"{failures} model(s) could not be downloaded; the app falls back to older models.", file=sys.stderr)
    return 0  # never fail a Docker build over a model; the app has fallbacks


if __name__ == "__main__":
    sys.exit(main())
