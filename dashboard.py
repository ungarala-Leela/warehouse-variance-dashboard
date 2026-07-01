import streamlit as st
import polars as pl
import os
import glob
import requests
import io

st.set_page_config(page_title="Warehouse Variance Dashboard", layout="wide")
st.title("📊 Warehouse Variance & FSN Performance Dashboard")
st.markdown("---")

PREFIX_LEN = 7

# --- 1. DATA SOURCES ---
DATA_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

GITHUB_REPO       = "ungarala-Leela/warehouse-variance-dashboard"
MAIN_FILE_ASSET   = "variance.csv"
MAIN_FILE_URL     = f"https://github.com/{GITHUB_REPO}/releases/latest/download/{MAIN_FILE_ASSET}"
LOCAL_CACHE_PATH  = os.path.join("/tmp", MAIN_FILE_ASSET)


# ---------- helpers ----------
def list_data_files(folder):
    if not os.path.exists(folder):
        return []
    files = []
    for p in ["*.csv", "*.xlsx", "*.xls"]:
        files.extend(glob.glob(os.path.join(folder, p)))
    return [f for f in files if os.path.isfile(f) and not os.path.basename(f).startswith("~$")]


def find_by_keyword(files, keywords):
    for f in files:
        if any(kw in os.path.basename(f).lower() for kw in keywords):
            return f
    return None


def strip_col_names(df: pl.DataFrame) -> pl.DataFrame:
    return df.rename({c: c.strip() for c in df.columns if c != c.strip()})


def read_any_file(path):
    if path.lower().endswith((".xlsx", ".xls")):
        return pl.read_excel(path)
    # Use lazy scan for large CSVs to reduce peak memory usage
    try:
        return pl.scan_csv(path, low_memory=True).collect()
    except Exception:
        return pl.read_csv(path, low_memory=True)


def add_prefix(df: pl.DataFrame, col: str, alias: str) -> pl.DataFrame:
    return df.with_columns(
        pl.col(col).cast(pl.String).str.to_lowercase().str.slice(0, PREFIX_LEN).alias(alias)
    )


# ---------- download large file ----------
@st.cache_data(show_spinner=True)
def download_main_file(url: str, version_tag: str) -> str:
    """Download from GitHub Release (follows redirects) and cache locally."""
    headers = {"Accept": "application/octet-stream"}
    resp = requests.get(url, headers=headers, allow_redirects=True, timeout=300)
    resp.raise_for_status()
    with open(LOCAL_CACHE_PATH, "wb") as f:
        f.write(resp.content)
    return LOCAL_CACHE_PATH


# ---------- sidebar: data source panel ----------
all_files = list_data_files(DATA_FOLDER)
HUB_PATH  = find_by_keyword(all_files, ["hub"])
FSN_PATH  = find_by_keyword(all_files, ["fsn"])

with st.sidebar:
    st.header("📁 Data Source")
    version_tag = st.text_input(
        "Data version (change after uploading a new Release file to force re-download):",
        value="v1",
    )
    try:
        MAIN_PATH = download_main_file(MAIN_FILE_URL, version_tag)
        st.success(f"✅ Main file ready ({MAIN_FILE_ASSET})")
    except Exception as e:
        MAIN_PATH = None
        st.error(f"❌ Download failed: {e}")

    st.success(f"✅ Hub: {os.path.basename(HUB_PATH)}")  if HUB_PATH  else st.warning("⚠️ Hub file not found in data/")
    st.success(f"✅ FSN: {os.path.basename(FSN_PATH)}")  if FSN_PATH  else st.warning("⚠️ FSN file not found in data/")

    if st.button("🔄 Clear cache & reload"):
        st.cache_data.clear()
        st.rerun()
    st.markdown("---")


# ---------- guard ----------
if not MAIN_PATH:
    st.error(
        f"❌ Could not download the variance file.\n\n"
        f"URL tried: `{MAIN_FILE_URL}`\n\n"
        "Make sure you have published a GitHub Release and attached "
        f"`{MAIN_FILE_ASSET}` to it."
    )
    st.stop()


# ---------- data engine ----------
@st.cache_data(show_spinner=True)
def load_and_merge_data(main_path, hub_path, fsn_path):
    main_df = read_any_file(main_path)
    main_df = strip_col_names(main_df)

    expected = {"variance_warehouse_id", "variance_quantity"}
    if not expected.issubset(set(main_df.columns)):
        raise ValueError(
            f"Variance file missing columns: {expected - set(main_df.columns)}. "
            f"Found: {main_df.columns}"
        )

    if "variance_id" in main_df.columns:
        main_df = main_df.drop("variance_id")

    main_df = main_df.with_columns(
        pl.col("variance_quantity").cast(pl.Float64, strict=False),
        pl.col("VALUE").cast(pl.Float64, strict=False),
    )

    main_df = add_prefix(main_df, "variance_warehouse_id", "match_key")
    metadata_cols = ["Zone", "City", "Town", "Store Name"]

    # ---- hub mapping ----
    if hub_path:
        try:
            df_hub = strip_col_names(read_any_file(hub_path))
            rename_map = {}
            for c in df_hub.columns:
                k = c.strip().replace("_", " ").lower()
                if k == "warehouse id":  rename_map[c] = "Warehouse ID"
                elif k == "site id":     rename_map[c] = "Site_ID"
                elif k == "zone":        rename_map[c] = "Zone"
                elif k == "city":        rename_map[c] = "City"
                elif k == "town":        rename_map[c] = "Town"
                elif k == "store name":  rename_map[c] = "Store Name"
            df_hub = df_hub.rename(rename_map)
            keep = [c for c in ["Warehouse ID", "Site_ID"] + metadata_cols if c in df_hub.columns]
            df_hub = df_hub.select(keep)

            lookup_wh = None
            if "Warehouse ID" in df_hub.columns:
                lookup_wh = add_prefix(df_hub.drop_nulls(["Warehouse ID"]), "Warehouse ID", "match_key") \
                    .unique(["match_key"]) \
                    .select(["match_key"] + [c for c in metadata_cols if c in df_hub.columns])

            lookup_site = None
            if "Site_ID" in df_hub.columns:
                lookup_site = add_prefix(df_hub.drop_nulls(["Site_ID"]), "Site_ID", "match_key") \
                    .unique(["match_key"]) \
                    .select(["match_key"] + [c for c in metadata_cols if c in df_hub.columns])

            if lookup_wh is not None:
                main_df = main_df.join(lookup_wh, on="match_key", how="left", coalesce=True)
            else:
                for c in metadata_cols:
                    main_df = main_df.with_columns(pl.lit(None).cast(pl.String).alias(c))

            if lookup_site is not None and "Zone" in main_df.columns:
                missing = main_df.filter(pl.col("Zone").is_null())
                matched = main_df.filter(pl.col("Zone").is_not_null())
                if missing.height > 0:
                    drop_cols = [c for c in metadata_cols if c in missing.columns]
                    missing = missing.drop(drop_cols).join(lookup_site, on="match_key", how="left", coalesce=True)
                    main_df = pl.concat([matched, missing], how="diagonal_relaxed")
        except Exception as e:
            st.warning(f"⚠️ Hub mapping failed: {e}")

    if "match_key" in main_df.columns:
        main_df = main_df.drop("match_key")

    for col in metadata_cols:
        if col not in main_df.columns:
            main_df = main_df.with_columns(pl.lit("").alias(col))
        else:
            main_df = main_df.with_columns(pl.col(col).fill_null(""))

    # ---- FSN cost mapping ----
    if fsn_path:
        try:
            df_fsn = strip_col_names(read_any_file(fsn_path))
            fsn_col  = [c for c in df_fsn.columns if "fsn"  in c.lower()]
            cost_col = [c for c in df_fsn.columns if "cost" in c.lower()]
            if fsn_col and cost_col:
                df_fsn = df_fsn.select([
                    pl.col(fsn_col[0]).alias("product_detail_fsn"),
                    pl.col(cost_col[0]).cast(pl.String).str.replace_all(",", "")
                      .str.strip_chars().cast(pl.Float64, strict=False).alias("cost_pu"),
                ]).unique(["product_detail_fsn"])
                main_df = main_df.join(df_fsn, on="product_detail_fsn", how="left", coalesce=True)
        except Exception as e:
            st.warning(f"⚠️ FSN mapping failed: {e}")

    if "cost_pu" not in main_df.columns:
        main_df = main_df.with_columns(pl.lit(None).cast(pl.Float64).alias("cost_pu"))

    # ---- NLC (signed) ----
    qty_sign = pl.when(pl.col("variance_quantity") < 0).then(-1).otherwise(1)
    main_df = main_df.with_columns(
        pl.when(pl.col("cost_pu").is_not_null())
          .then(pl.col("cost_pu") * pl.col("variance_quantity"))
          .otherwise(pl.col("VALUE").abs() * 0.8 * qty_sign)
          .round(0)
          .alias("nlc_value")
    )
    return main_df


# ---------- load ----------
with st.spinner("Processing files..."):
    try:
        df = load_and_merge_data(MAIN_PATH, HUB_PATH, FSN_PATH)
    except Exception as e:
        st.error(f"🚨 Load crash: {e}")
        st.stop()

if df is None:
    st.error("❌ Dataset is None after loading.")
    st.stop()


# ---------- sidebar filters ----------
st.sidebar.header("📊 Global Controls")

required_cols = ["Zone", "City", "variance_warehouse_id", "full_date", "variance_reason_type"]
missing_cols  = [c for c in required_cols if c not in df.columns]
if missing_cols:
    st.error(f"❌ Required column(s) missing: {missing_cols}")
    st.stop()

unique_zones      = sorted({z for z in df["Zone"].to_list() if z})
unique_cities     = sorted({c for c in df["City"].to_list() if c})
unique_warehouses = sorted({w for w in df["variance_warehouse_id"].to_list() if w})
unique_dates      = sorted({d for d in df["full_date"].to_list() if d})
unique_reasons    = sorted({r for r in df["variance_reason_type"].to_list() if r})

sel_zones      = st.sidebar.multiselect("Zone(s):",             unique_zones)
sel_cities     = st.sidebar.multiselect("City/Cities:",         unique_cities)
sel_warehouses = st.sidebar.multiselect("Warehouse ID(s):",     unique_warehouses)
sel_dates      = st.sidebar.multiselect("Date(s):",             unique_dates)
sel_reasons    = st.sidebar.multiselect("Variance Reason(s):",  unique_reasons)

st.sidebar.markdown("---")
st.sidebar.header("🎯 FSN / Sort Filters")

qty_type   = st.sidebar.radio("Quantity Type:",  ["All Quantities", "Negative (-VE) Only", "Positive (+VE) Only"])
sort_by    = st.sidebar.radio("Sort By:",        ["Variance Quantity", "NLC Value"])
sort_order = st.sidebar.radio("Sort Order:",     ["Descending (Highest)", "Ascending (Lowest)"])
top_n      = st.sidebar.slider("Top N FSNs:",    5, 100, 10)
st.sidebar.markdown("---")
net_filter = st.sidebar.radio("Net NLC Filter:", ["All", "Net Positive Only", "Net Negative Only"])


# ---------- apply filters ----------
fdf = df
if sel_zones:      fdf = fdf.filter(pl.col("Zone").is_in(sel_zones))
if sel_cities:     fdf = fdf.filter(pl.col("City").is_in(sel_cities))
if sel_warehouses: fdf = fdf.filter(pl.col("variance_warehouse_id").is_in(sel_warehouses))
if sel_dates:      fdf = fdf.filter(pl.col("full_date").is_in(sel_dates))
if sel_reasons:    fdf = fdf.filter(pl.col("variance_reason_type").is_in(sel_reasons))
if qty_type == "Negative (-VE) Only": fdf = fdf.filter(pl.col("variance_quantity") < 0)
if qty_type == "Positive (+VE) Only": fdf = fdf.filter(pl.col("variance_quantity") > 0)

is_desc = sort_order == "Descending (Highest)"
n_rows  = fdf.height


# ---------- KPIs ----------
total_qty = fdf["variance_quantity"].sum() if n_rows else 0
total_nlc = fdf["nlc_value"].sum()         if n_rows else 0

c1, c2 = st.columns(2)
c1.metric("Total Variance Qty",    f"{total_qty:,.0f}")
c2.metric("Total Calculated NLC",  f"{total_nlc:,.0f}")
st.markdown("---")


def agg_nlc(df_in, group_keys):
    return (
        df_in.group_by(group_keys).agg([
            pl.col("variance_quantity").sum().round(0).cast(pl.Int64).alias("Total_Variance_Qty"),
            pl.col("nlc_value").sum().round(0).cast(pl.Int64).alias("Total_NLC_Value"),
            pl.when(pl.col("variance_quantity") > 0).then(pl.col("nlc_value")).otherwise(0)
              .sum().round(0).cast(pl.Int64).alias("Positive_NLC_Value"),
            pl.when(pl.col("variance_quantity") < 0).then(pl.col("nlc_value")).otherwise(0)
              .sum().round(0).cast(pl.Int64).alias("Negative_NLC_Value"),
        ])
        .with_columns(
            (pl.col("Positive_NLC_Value") + pl.col("Negative_NLC_Value")).alias("Net_NLC_Value")
        )
    )


sort_col_map = {"Variance Quantity": "Total_Variance_Qty", "NLC Value": "Total_NLC_Value"}


# ---------- Top FSN ----------
st.subheader(f"🔝 Top {top_n} FSNs by {sort_by}")
if n_rows > 0 and "product_detail_fsn" in fdf.columns:
    fsn_sum = agg_nlc(fdf, ["Zone", "City", "variance_warehouse_id", "product_detail_fsn"])
    fsn_sum = fsn_sum.sort(sort_col_map[sort_by], descending=is_desc).head(top_n)
    st.dataframe(fsn_sum.to_pandas(), use_container_width=True)
else:
    st.warning("⚠️ No data matches the current filters.")
st.markdown("---")


# ---------- City Summary ----------
st.subheader(f"🏙️ City-wise Summary (sorted by {sort_by})")
if n_rows > 0:
    city_sum = agg_nlc(fdf, ["Zone", "City"])
    city_sum = city_sum.sort(sort_col_map[sort_by], descending=is_desc)
    st.dataframe(city_sum.to_pandas(), use_container_width=True)
else:
    st.warning("⚠️ No data matches the current filters.")
st.markdown("---")


# ---------- Net NLC FSN ----------
st.subheader("⚖️ Net NLC FSN Analysis (FSNs with both Excess and Shortage)")
if n_rows > 0 and "product_detail_fsn" in fdf.columns:
    net = agg_nlc(fdf, ["Zone", "City", "variance_warehouse_id", "product_detail_fsn"])
    net = net.filter((pl.col("Positive_NLC_Value") != 0) & (pl.col("Negative_NLC_Value") != 0))
    if net_filter == "Net Positive Only": net = net.filter(pl.col("Net_NLC_Value") > 0)
    if net_filter == "Net Negative Only": net = net.filter(pl.col("Net_NLC_Value") < 0)
    net = net.drop(["Total_Variance_Qty", "Total_NLC_Value"]).sort("Net_NLC_Value", descending=is_desc)
    if net.height > 0:
        st.dataframe(net.to_pandas(), use_container_width=True)
    else:
        st.info("ℹ️ No FSNs with both +ve and -ve NLC under current filters.")
else:
    st.warning("⚠️ No data matches the current filters.")
st.markdown("---")


# ---------- Raw Ledger ----------
st.subheader("📋 Raw Filtered Data Ledger (Top 5000 rows)")
show_cols  = [c for c in fdf.columns if c not in ("cost_pu", "full_date")]
ledger_col = "variance_quantity" if sort_by == "Variance Quantity" else "nlc_value"
st.dataframe(
    fdf.select(show_cols).sort(ledger_col, descending=is_desc).head(5000).to_pandas(),
    use_container_width=True,
)
