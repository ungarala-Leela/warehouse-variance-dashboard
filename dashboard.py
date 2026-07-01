import streamlit as st
import polars as pl
import os
import glob
import requests

st.set_page_config(page_title="Warehouse Variance Dashboard", layout="wide")
st.title("📊 Warehouse Variance & FSN Performance Dashboard")
st.markdown("---")

PREFIX_LEN = 7  # number of leading characters used to match warehouse/site IDs

# --- 1. DATA SOURCES ---
# Small files (Hub Map, FSN Mapping) stay bundled in the repo's "data/" folder.
# The big variance file (too large for a normal repo commit) is hosted as a
# GitHub Release asset and downloaded at runtime from a stable "latest" URL.
DATA_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# >>> EDIT THESE TWO LINES to match your repo + release asset filename <<<
GITHUB_REPO = "ungarala-Leela/warehouse-variance-dashboard"
MAIN_FILE_ASSET_NAME = "variance.csv"  # exact filename you attached to the Release

MAIN_FILE_URL = f"https://github.com/{GITHUB_REPO}/releases/latest/download/{MAIN_FILE_ASSET_NAME}"
LOCAL_CACHE_PATH = os.path.join("/tmp", MAIN_FILE_ASSET_NAME)


@st.cache_data(show_spinner="Downloading latest data file from GitHub Release...")
def download_main_file(url: str, cache_buster: str = "") -> str:
    """Downloads the file once per cache_buster value and returns the local path."""
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    with open(LOCAL_CACHE_PATH, "wb") as f:
        f.write(response.content)
    return LOCAL_CACHE_PATH


def list_data_files(folder):
    if not os.path.exists(folder):
        return []
    patterns = ["*.csv", "*.xlsx", "*.xls"]
    files = []
    for p in patterns:
        files.extend(glob.glob(os.path.join(folder, p)))
    return [f for f in files if os.path.isfile(f) and not os.path.basename(f).startswith("~$")]


def find_by_keyword(files, keywords):
    for f in files:
        name = os.path.basename(f).lower()
        if any(kw in name for kw in keywords):
            return f
    return None


all_files = list_data_files(DATA_FOLDER)
HUB_PATH = find_by_keyword(all_files, ["hub"])
FSN_PATH = find_by_keyword(all_files, ["fsn"])

with st.sidebar:
    st.header("📁 Data Source")
    cache_buster = st.text_input(
        "Data version tag (change this after publishing a new Release to force a re-download)",
        value="v1",
    )
    try:
        MAIN_PATH = download_main_file(MAIN_FILE_URL, cache_buster)
        st.success(f"Main file: downloaded ({MAIN_FILE_ASSET_NAME})")
    except Exception as download_err:
        MAIN_PATH = None
        st.error(f"❌ Could not download main file: {download_err}")

    if HUB_PATH:
        st.success(f"Hub file: {os.path.basename(HUB_PATH)}")
    if FSN_PATH:
        st.success(f"FSN file: {os.path.basename(FSN_PATH)}")
    if st.button("🔄 Refresh data (clear cache)"):
        st.cache_data.clear()
        st.rerun()
    st.markdown("---")


def strip_col_names(df: pl.DataFrame) -> pl.DataFrame:
    """Remove leading/trailing whitespace from every column name."""
    return df.rename({c: c.strip() for c in df.columns if c != c.strip()})


def read_any_file(path):
    if path.lower().endswith((".xlsx", ".xls")):
        return pl.read_excel(path)
    return pl.read_csv(path)


def add_prefix(df: pl.DataFrame, col: str, alias: str) -> pl.DataFrame:
    """Lowercase + first PREFIX_LEN chars of a column, used as the join key."""
    return df.with_columns(
        pl.col(col).cast(pl.String).str.to_lowercase().str.slice(0, PREFIX_LEN).alias(alias)
    )


# --- 2. DATA ENGINE ---
@st.cache_data(show_spinner=False)
def load_and_merge_data(main_path, hub_path, fsn_path):
    # ---- Load main variance file ----
    main_df = read_any_file(main_path)
    main_df = strip_col_names(main_df)

    expected_cols = {"variance_warehouse_id", "variance_quantity"}
    if not expected_cols.issubset(set(main_df.columns)):
        raise ValueError(
            f"'{os.path.basename(main_path)}' does not look like the variance file "
            f"(missing {expected_cols - set(main_df.columns)}). "
            f"Found columns: {main_df.columns}."
        )

    if "variance_id" in main_df.columns:
        main_df = main_df.drop("variance_id")

    main_df = main_df.with_columns(
        pl.col("variance_quantity").cast(pl.Float64, strict=False),
        pl.col("VALUE").cast(pl.Float64, strict=False),
    )

    # Build the matching key: first 7 chars of variance_warehouse_id, lowercased
    main_df = add_prefix(main_df, "variance_warehouse_id", "match_key")

    # ---- A. Map Zone / City / Town / Store Name from hub file ----
    metadata_cols = ["Zone", "City", "Town", "Store Name"]

    if hub_path:
        try:
            df_hub = read_any_file(hub_path)
            df_hub = strip_col_names(df_hub)

            # normalize hub column names so "Site_ID" / "Site ID" etc. both work
            rename_map = {}
            for c in df_hub.columns:
                key = c.strip().replace("_", " ").lower()
                if key == "warehouse id":
                    rename_map[c] = "Warehouse ID"
                elif key == "site id":
                    rename_map[c] = "Site_ID"
                elif key == "zone":
                    rename_map[c] = "Zone"
                elif key == "city":
                    rename_map[c] = "City"
                elif key == "town":
                    rename_map[c] = "Town"
                elif key == "store name":
                    rename_map[c] = "Store Name"
            df_hub = df_hub.rename(rename_map)

            keep_cols = [c for c in ["Warehouse ID", "Site_ID"] + metadata_cols if c in df_hub.columns]
            df_hub = df_hub.select(keep_cols)

            # Lookup #1: match key built from Warehouse ID
            lookup_wh = None
            if "Warehouse ID" in df_hub.columns:
                lookup_wh = add_prefix(
                    df_hub.drop_nulls(subset=["Warehouse ID"]), "Warehouse ID", "match_key"
                ).unique(subset=["match_key"])
                lookup_wh = lookup_wh.select(["match_key"] + [c for c in metadata_cols if c in lookup_wh.columns])

            # Lookup #2: match key built from Site_ID (fallback)
            lookup_site = None
            if "Site_ID" in df_hub.columns:
                lookup_site = add_prefix(
                    df_hub.drop_nulls(subset=["Site_ID"]), "Site_ID", "match_key"
                ).unique(subset=["match_key"])
                lookup_site = lookup_site.select(["match_key"] + [c for c in metadata_cols if c in lookup_site.columns])

            # Try Warehouse ID match first
            if lookup_wh is not None:
                main_df = main_df.join(lookup_wh, on="match_key", how="left")
            else:
                for c in metadata_cols:
                    main_df = main_df.with_columns(pl.lit(None).cast(pl.String).alias(c))

            # Fallback to Site_ID match for rows still missing Zone
            if lookup_site is not None and "Zone" in main_df.columns:
                still_missing = main_df.filter(pl.col("Zone").is_null())
                already_matched = main_df.filter(pl.col("Zone").is_not_null())

                if still_missing.height > 0:
                    fallback_meta_cols = [c for c in metadata_cols if c in still_missing.columns]
                    still_missing = still_missing.drop(fallback_meta_cols).join(
                        lookup_site, on="match_key", how="left"
                    )
                    main_df = pl.concat([already_matched, still_missing], how="diagonal_relaxed")

        except Exception as hub_error:
            st.warning(f"⚠️ Hub mapping failed: {hub_error}. Zone/City/Town/Store Name left blank.")

    if "match_key" in main_df.columns:
        main_df = main_df.drop("match_key")

    # Guarantee metadata columns exist and blank-fill unmatched rows
    for col in metadata_cols:
        if col not in main_df.columns:
            main_df = main_df.with_columns(pl.lit("").alias(col))
        else:
            main_df = main_df.with_columns(pl.col(col).fill_null(""))

    # ---- B. Map cost_pu from the FSN Mapping file ----
    if fsn_path:
        try:
            df_fsn = read_any_file(fsn_path)
            df_fsn = strip_col_names(df_fsn)
            fsn_col = [c for c in df_fsn.columns if "fsn" in c.lower()]
            cost_col = [c for c in df_fsn.columns if "cost" in c.lower()]

            if fsn_col and cost_col:
                df_fsn = df_fsn.select(
                    [
                        pl.col(fsn_col[0]).alias("product_detail_fsn"),
                        pl.col(cost_col[0])
                        .cast(pl.String)
                        .str.replace_all(",", "")
                        .str.strip_chars()
                        .cast(pl.Float64, strict=False)
                        .alias("cost_pu"),
                    ]
                ).unique(subset=["product_detail_fsn"])

                main_df = main_df.join(df_fsn, on="product_detail_fsn", how="left")
        except Exception as fsn_error:
            st.warning(f"⚠️ FSN cost mapping failed: {fsn_error}.")

    if "cost_pu" not in main_df.columns:
        main_df = main_df.with_columns(pl.lit(None).cast(pl.Float64).alias("cost_pu"))

    # ---- C. Calculate NLC value (SIGNED): cost_pu * qty if cost known (sign follows qty),
    #         else |VALUE| * 0.8, sign-matched to qty ----
    qty_sign = pl.when(pl.col("variance_quantity") < 0).then(-1).otherwise(1)
    main_df = main_df.with_columns(
        pl.when(pl.col("cost_pu").is_not_null())
        .then(pl.col("cost_pu") * pl.col("variance_quantity"))  # naturally signed (cost_pu is always >= 0)
        .otherwise(pl.col("VALUE").abs() * 0.8 * qty_sign)
        .round(0)
        .alias("nlc_value")
    )

    return main_df


# --- 3. INGESTION ---
if not MAIN_PATH:
    st.error(
        "❌ Could not download the main variance file.\n\n"
        f"Check that `{MAIN_FILE_URL}` points to a real, published GitHub Release asset."
    )
    st.stop()

with st.spinner("Processing files and structural joins..."):
    try:
        df = load_and_merge_data(MAIN_PATH, HUB_PATH, FSN_PATH)
    except Exception as runtime_err:
        st.error(f"🚨 Core engine load crash: {runtime_err}")
        st.stop()

if df is None:
    st.error("❌ Dataset failed to load. Check the data files and try again.")
    st.stop()

# --- 4. SIDEBAR FILTERS ---
st.sidebar.header("📊 Global Controls")

required_cols = ["Zone", "City", "variance_warehouse_id", "full_date", "variance_reason_type"]
missing = [c for c in required_cols if c not in df.columns]
if missing:
    st.error(f"❌ Required column(s) missing from data: {missing}")
    st.stop()

unique_zones = sorted({z for z in df["Zone"].to_list() if z})
unique_cities = sorted({c for c in df["City"].to_list() if c})
unique_warehouses = sorted({w for w in df["variance_warehouse_id"].unique().to_list() if w})
unique_dates = sorted({d for d in df["full_date"].unique().to_list() if d})
unique_reasons = sorted({r for r in df["variance_reason_type"].unique().to_list() if r})

selected_zones = st.sidebar.multiselect("Select Zone(s):", options=unique_zones, default=[])
selected_cities = st.sidebar.multiselect("Select City/Cities:", options=unique_cities, default=[])
selected_warehouses = st.sidebar.multiselect("Select Warehouse ID(s):", options=unique_warehouses, default=[])
selected_dates = st.sidebar.multiselect("Select Date(s):", options=unique_dates, default=[])
selected_reasons = st.sidebar.multiselect("Select Variance Reason Type(s):", options=unique_reasons, default=[])

st.sidebar.markdown("---")
st.sidebar.header("🎯 Target FSN Filters")
qty_filter_type = st.sidebar.radio(
    "Variance Quantity Target Type:",
    options=["All Quantities", "Negative (-VE) Only", "Positive (+VE) Only"],
)
sort_by = st.sidebar.radio("Sort By:", options=["Variance Quantity", "NLC Value"])
sort_order = st.sidebar.radio("Sort Order:", options=["Descending (Highest)", "Ascending (Lowest)"])
top_n = st.sidebar.slider("Show Top FSNs Count:", min_value=5, max_value=100, value=10)
st.sidebar.markdown("---")
net_filter = st.sidebar.radio("Net NLC Filter (for Net NLC tab):", options=["All", "Net Positive Only", "Net Negative Only"])

# --- 5. APPLY FILTERS ---
filtered_df = df
if selected_zones:
    filtered_df = filtered_df.filter(pl.col("Zone").is_in(selected_zones))
if selected_cities:
    filtered_df = filtered_df.filter(pl.col("City").is_in(selected_cities))
if selected_warehouses:
    filtered_df = filtered_df.filter(pl.col("variance_warehouse_id").is_in(selected_warehouses))
if selected_dates:
    filtered_df = filtered_df.filter(pl.col("full_date").is_in(selected_dates))
if selected_reasons:
    filtered_df = filtered_df.filter(pl.col("variance_reason_type").is_in(selected_reasons))
if qty_filter_type == "Negative (-VE) Only":
    filtered_df = filtered_df.filter(pl.col("variance_quantity") < 0)
elif qty_filter_type == "Positive (+VE) Only":
    filtered_df = filtered_df.filter(pl.col("variance_quantity") > 0)

# --- 6. KPIs ---
total_rows = filtered_df.height
total_qty = filtered_df["variance_quantity"].sum() if total_rows > 0 else 0
total_nlc = filtered_df["nlc_value"].sum() if total_rows > 0 else 0

col1, col2 = st.columns(2)
with col1:
    st.metric(label="Total Variance Qty", value=f"{total_qty:,.0f}")
with col2:
    st.metric(label="Total Calculated NLC Value", value=f"{total_nlc:,.0f}")

st.markdown("---")

# --- 7. TOP FSN ANALYSIS ---
st.subheader(f"🔝 Top {top_n} FSNs by {sort_by}")
if total_rows > 0 and "product_detail_fsn" in filtered_df.columns:
    fsn_summary = filtered_df.group_by(
        ["Zone", "City", "variance_warehouse_id", "product_detail_fsn"]
    ).agg(
        [
            pl.col("variance_quantity").sum().round(0).cast(pl.Int64).alias("Total_Variance_Qty"),
            pl.col("nlc_value").sum().round(0).cast(pl.Int64).alias("Total_NLC_Value"),
            pl.when(pl.col("variance_quantity") > 0)
            .then(pl.col("nlc_value"))
            .otherwise(0)
            .sum()
            .round(0)
            .cast(pl.Int64)
            .alias("Positive_NLC_Value"),
            pl.when(pl.col("variance_quantity") < 0)
            .then(pl.col("nlc_value"))
            .otherwise(0)
            .sum()
            .round(0)
            .cast(pl.Int64)
            .alias("Negative_NLC_Value"),
        ]
    ).with_columns(
        (pl.col("Positive_NLC_Value") + pl.col("Negative_NLC_Value")).alias("Net_NLC_Value")
    )

    sort_col = "Total_Variance_Qty" if sort_by == "Variance Quantity" else "Total_NLC_Value"
    is_descending = sort_order == "Descending (Highest)"
    fsn_summary = fsn_summary.sort(sort_col, descending=is_descending).head(top_n)
    st.dataframe(fsn_summary.to_pandas(), use_container_width=True)
else:
    st.warning("⚠️ No data matches the active filter selection configuration.")
    is_descending = sort_order == "Descending (Highest)"

st.markdown("---")

# --- 7b. CITY-WISE SUMMARY ---
st.subheader(f"🏙️ City-wise Summary (sorted by {sort_by})")
if total_rows > 0:
    city_summary = filtered_df.group_by(["Zone", "City"]).agg(
        [
            pl.col("variance_quantity").sum().round(0).cast(pl.Int64).alias("Total_Variance_Qty"),
            pl.col("nlc_value").sum().round(0).cast(pl.Int64).alias("Total_NLC_Value"),
            pl.when(pl.col("variance_quantity") > 0)
            .then(pl.col("nlc_value"))
            .otherwise(0)
            .sum()
            .round(0)
            .cast(pl.Int64)
            .alias("Positive_NLC_Value"),
            pl.when(pl.col("variance_quantity") < 0)
            .then(pl.col("nlc_value"))
            .otherwise(0)
            .sum()
            .round(0)
            .cast(pl.Int64)
            .alias("Negative_NLC_Value"),
        ]
    ).with_columns(
        (pl.col("Positive_NLC_Value") + pl.col("Negative_NLC_Value")).alias("Net_NLC_Value")
    )

    city_sort_col = "Total_Variance_Qty" if sort_by == "Variance Quantity" else "Total_NLC_Value"
    city_summary = city_summary.sort(city_sort_col, descending=is_descending)
    st.dataframe(city_summary.to_pandas(), use_container_width=True)
else:
    st.warning("⚠️ No data matches the active filter selection configuration.")

st.markdown("---")

# --- 7c. NET NLC FSN ANALYSIS (only FSNs with BOTH +ve and -ve NLC) ---
st.subheader("⚖️ Net NLC FSN Analysis (FSNs with both Excess and Shortage)")
if total_rows > 0 and "product_detail_fsn" in filtered_df.columns:
    net_fsn = filtered_df.group_by(["Zone", "City", "variance_warehouse_id", "product_detail_fsn"]).agg(
        [
            # NLC kept signed: +ve for excess-found rows, -ve for shortage rows
            pl.when(pl.col("variance_quantity") > 0)
            .then(pl.col("nlc_value"))
            .otherwise(0)
            .sum()
            .round(0)
            .cast(pl.Int64)
            .alias("Positive_NLC_Qty"),
            pl.when(pl.col("variance_quantity") < 0)
            .then(pl.col("nlc_value"))
            .otherwise(0)
            .sum()
            .round(0)
            .cast(pl.Int64)
            .alias("Negative_NLC_Qty"),
        ]
    ).with_columns(
        (pl.col("Positive_NLC_Qty") + pl.col("Negative_NLC_Qty")).alias("Net_NLC_Value")
    )

    # Only keep FSNs that have BOTH a positive and a negative NLC component
    net_fsn = net_fsn.filter((pl.col("Positive_NLC_Qty") != 0) & (pl.col("Negative_NLC_Qty") != 0))

    if net_filter == "Net Positive Only":
        net_fsn = net_fsn.filter(pl.col("Net_NLC_Value") > 0)
    elif net_filter == "Net Negative Only":
        net_fsn = net_fsn.filter(pl.col("Net_NLC_Value") < 0)

    net_sort_col = "Net_NLC_Value"
    net_fsn = net_fsn.sort(net_sort_col, descending=is_descending)

    if net_fsn.height > 0:
        st.dataframe(net_fsn.to_pandas(), use_container_width=True)
    else:
        st.info("ℹ️ No FSNs found with both positive and negative NLC under the current filters.")
else:
    st.warning("⚠️ No data matches the active filter selection configuration.")

st.markdown("---")
st.subheader("📋 Raw Filtered Data Ledger (Top 5000 rows)")
cols_to_show = [c for c in filtered_df.columns if c not in ("cost_pu", "full_date")]
ledger_sort_col = "variance_quantity" if sort_by == "Variance Quantity" else "nlc_value"
ledger_df = filtered_df.select(cols_to_show).sort(ledger_sort_col, descending=is_descending).head(5000)
st.dataframe(ledger_df.to_pandas(), use_container_width=True)
