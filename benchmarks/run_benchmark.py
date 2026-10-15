"""Repeatable ClearSpace benchmark on synthetic datasets.

Usage:
    python benchmarks/run_benchmark.py                 # baseline-comparable dataset (5,001 entries)
    python benchmarks/run_benchmark.py --dirs 200 --files 500 --repeat 3

Measures enumeration, classification, duplicate hashing, cache load, throughput and peak RAM.
Everything happens inside a temporary directory that is deleted afterwards.
"""
from __future__ import annotations

import argparse
import os
import shutil
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.cache import load_drive_index, load_drive_result, set_cache_db_path  # noqa: E402
from app.core.classifier import ALL_MODES, classify_and_filter  # noqa: E402
from app.core.duplicates import find_duplicate_groups  # noqa: E402
from app.core.engine import scan_drive  # noqa: E402
from app.core.fast_walk import walk_drive_parallel  # noqa: E402
from app.core.models import ScanParameters  # noqa: E402

# Measured on the original implementation (before this rework) with the same
# 50 x 100 + 1 empty-directory dataset and all six modes enabled.
BASELINE = {
    "files": 5001,
    "enum_s": 3.4716,
    "files_per_sec": 1441,
    "class_s": 0.5486,
    "items": 5199,
    "peak_mb": 9.02,
}


def build_dataset(root: Path, n_dirs: int, n_files: int, dup_every: int) -> dict[str, int]:
    now = time.time()
    expected_dups = 0
    for d in range(n_dirs):
        sub = root / f"dir_{d:04d}" / "nested"
        sub.mkdir(parents=True)
        for f in range(n_files):
            p = sub / f"file_{f:05d}.dat"
            if dup_every and f % dup_every == 0:
                payload = b"DUPLICATE-PAYLOAD-" * 4000  # 72 KB, identical across dirs
                expected_dups += 1
            else:
                payload = f"{d}-{f}-".encode() * (1 + f % 40)
            p.write_bytes(payload)
            if f % 10 == 0:
                ts = now - 400 * 86400
                os.utime(p, (ts, ts))
    (root / "empty_folder").mkdir()
    return {"entries": n_dirs * n_files + 1, "dup_files": expected_dups}


def measure(root: Path, repeat: int) -> dict[str, float]:
    params = ScanParameters.from_options(
        [str(root)], ALL_MODES, {"min_mb": 100, "min_kb": 1, "days_old": 365, "days_downloads": 90}
    )
    enum_times: list[float] = []
    class_times: list[float] = []
    hash_times: list[float] = []
    records = []
    items = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        records = walk_drive_parallel(str(root))
        enum_times.append(time.perf_counter() - t0)

        t0 = time.perf_counter()
        items = classify_and_filter(records, params)
        class_times.append(time.perf_counter() - t0)

        t0 = time.perf_counter()
        find_duplicate_groups([r for r in records if not r.is_dir], min_size_bytes=1024)
        hash_times.append(time.perf_counter() - t0)

    tracemalloc.start()
    walk_records = walk_drive_parallel(str(root))
    classify_and_filter(walk_records, params)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    opts = params.to_options_dict()
    scan_drive(str(root), list(params.modes), opts, use_cache=True, force=True)
    t0 = time.perf_counter()
    idx = load_drive_index(str(root), custom_exclusions=())
    cache_index_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    res = load_drive_result(str(root), list(params.modes), opts)
    cache_result_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    refilter = scan_drive(str(root), ["old_files"], {"days_old": 365}, use_cache=True)
    refilter_s = time.perf_counter() - t0

    enum_s = statistics.median(enum_times)
    files = sum(1 for r in records if not r.is_dir)
    cats: dict[str, int] = {}
    for it in items:
        for c in it.categories:
            cats[c] = cats.get(c, 0) + 1
    return {
        "records": len(records),
        "files": files,
        "enum_s": enum_s,
        "files_per_sec": files / enum_s if enum_s else 0.0,
        "class_s": statistics.median(class_times),
        "hash_s": statistics.median(hash_times),
        "items": len(items),
        "cats": cats,
        "peak_mb": peak / (1024 * 1024),
        "cache_index_s": cache_index_s if idx else float("nan"),
        "cache_result_s": cache_result_s if res else float("nan"),
        "refilter_from_index_s": refilter_s,
        "refilter_from_cache": refilter.from_cache,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dirs", type=int, default=50)
    ap.add_argument("--files", type=int, default=100)
    ap.add_argument("--dup-every", type=int, default=25, help="every N-th file per dir is an identical copy")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--base", default=None, help="directory for the dataset (default: %%TEMP%%)")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="clearspace_bench_", dir=args.base))
    try:
        set_cache_db_path(tmp / "bench_cache.db")
        data = tmp / "data"
        data.mkdir()
        t0 = time.perf_counter()
        info = build_dataset(data, args.dirs, args.files, args.dup_every)
        print(f"dataset: {info['entries']:,} entries ({info['dup_files']:,} duplicate copies) "
              f"built in {time.perf_counter() - t0:.1f}s at {data}")
        m = measure(data, args.repeat)
        expected_old = sum(1 for _ in range(args.dirs) for f in range(args.files) if f % 10 == 0)
        expected_dup = info["dup_files"] if info["dup_files"] >= 2 else 0
        print()
        print(f"{'metric':<26}{'value':>14}")
        for key in ("files", "enum_s", "files_per_sec", "class_s", "hash_s", "items", "peak_mb",
                    "cache_index_s", "cache_result_s", "refilter_from_index_s"):
            val = m[key]
            print(f"{key:<26}{val:>14,.4f}" if isinstance(val, float) else f"{key:<26}{val:>14,}")
        print(f"{'categories':<26}{m['cats']}")
        print(f"expected: old_file={expected_old} duplicate={expected_dup} empty_dir=1 temp=0 large=0")

        if (args.dirs, args.files) == (50, 100):
            print("\ncomparison with original implementation (same dataset shape):")
            for key, label in (("enum_s", "enumeration s"), ("files_per_sec", "files / s"),
                               ("class_s", "classification s"), ("peak_mb", "peak RAM MB")):
                before, after = BASELINE[key], m[key]
                ratio = (after / before) if key == "files_per_sec" else (before / after if after else 0.0)
                print(f"  {label:<18} before={before:>10,.4f}  after={after:>10,.4f}  ({ratio:,.1f}x)")
            print(f"  {'result rows':<18} before={BASELINE['items']:>10,}  after={m['items']:>10,}  "
                  f"(expected {expected_old + expected_dup + 1 - args.dirs * 2:,}; extra rows before were false positives)")
    finally:
        set_cache_db_path(None)
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
