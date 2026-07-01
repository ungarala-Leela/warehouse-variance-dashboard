"""
Run this script ONCE on your local machine to create a slimmed variance.csv.
Then upload the slimmed file as a GitHub Release asset (replacing the old one).

Usage (from the folder containing your variance.csv):
    python slim_variance.py

Or specify paths:
    python slim_variance.py --input "C:/path/to/variance.csv" --output "C:/path/to/variance_slim.csv"
"""

import argparse
import os
import sys

# Only these columns are used by the dashboard
KEEP_COLS = [
    "full_date",
    "variance_warehouse_id",
    "variance_reason_type",
    "variance_quantity",
    "VALUE",
    "product_detail_fsn",
    "variance_id",
]


def slim(input_path: str, output_path: str):
    try:
        import polars as pl
    except ImportError:
        try:
            import pandas as pd
            print("Using pandas (polars not found)...")
            df = pd.read_csv(input_path, usecols=lambda c: c in KEEP_COLS)
            df.to_csv(output_path, index=False)
            before = os.path.getsize(input_path) / 1e6
            after = os.path.getsize(output_path) / 1e6
            print(f"Done! {before:.0f} MB → {after:.0f} MB  ({df.shape[0]:,} rows)")
            return
        except ImportError:
            print("ERROR: Install either polars or pandas first:")
            print("  pip install polars")
            sys.exit(1)

    print(f"Reading {input_path} ...")
    lf = pl.scan_csv(input_path, low_memory=True, ignore_errors=True)
    available = list(lf.schema)
    cols = [c for c in KEEP_COLS if c in available]
    missing = [c for c in KEEP_COLS if c not in available]
    if missing:
        print(f"Warning: these columns were not found and will be skipped: {missing}")
    print(f"Keeping columns: {cols}")
    df = lf.select(cols).collect()
    df.write_csv(output_path)
    before = os.path.getsize(input_path) / 1e6
    after = os.path.getsize(output_path) / 1e6
    print(f"\nDone!")
    print(f"  Rows:   {df.shape[0]:,}")
    print(f"  Before: {before:.0f} MB")
    print(f"  After:  {after:.0f} MB  ({100*(1-after/before):.0f}% smaller)")
    print(f"\nOutput saved to: {output_path}")
    print("\nNext step: upload variance_slim.csv as a GitHub Release asset")
    print("(replace the old variance.csv — keep the filename the same)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="variance.csv",
                        help="Path to original variance.csv")
    parser.add_argument("--output", default="variance_slim.csv",
                        help="Path for slimmed output file")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"ERROR: Input file not found: {args.input}")
        print("Run from the same folder as variance.csv, or use --input to specify the path.")
        sys.exit(1)

    slim(args.input, args.output)
