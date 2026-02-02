# dapp.py
# Streamlit Flight Dashboard (tabs + filters + calendar view + maintenance)
# -----------------------------------------------------------
# Run:
#   streamlit run dapp.py

import os
import io
import re
import math
import numpy as np
import pandas as pd
import streamlit as st
from datetime import datetime, date, timedelta
import calendar
import matplotlib.pyplot as plt
import plotly.express as px
import plotly.graph_objects as go

# ====== USER DEFAULTS ======
# DEFAULT_FILE_PATH = r"C:\Users\LENOVO\Documents\RBGH\flight dashboard\dashboard_1\Flight_Summary_1.xlsx"
# BATTERY_CSV_PATH = r"C:\Users\LENOVO\Documents\RBGH\flight dashboard\battery logs\UAV_BATTERY_LOGS.csv"
#
# # Battery files
# DEFAULT_BATT_CYCLES_PATH = r"C:\Users\LENOVO\Documents\RBGH\flight dashboard\dashboard_1\battery_cycles_long.csv"
# DEFAULT_BATT_VOLT_PATH = r"C:\Users\LENOVO\Documents\RBGH\flight dashboard\dashboard_1\battery_voltage_stats.csv"
#
# # Maintenance history file
# DEFAULT_MAINTENANCE_HISTORY_PATH = r"C:\Users\LENOVO\Documents\RBGH\flight dashboard\dashboard_1\maintenance_history.xlsx"

from pathlib import Path

try:
    BASE_DIR = Path(__file__).resolve().parent
except NameError:
    BASE_DIR = Path.cwd()

# ====== USER DEFAULTS ======
DEFAULT_FILE_PATH = BASE_DIR / "Flight_Summary_1.xlsx"
BATTERY_CSV_PATH = BASE_DIR / "UAV_BATTERY_LOGS.csv"

# Battery files
DEFAULT_BATT_CYCLES_PATH = BASE_DIR / "battery_cycles_long.csv"
DEFAULT_BATT_VOLT_PATH = BASE_DIR / "battery_voltage_stats.csv"

# Maintenance history file
DEFAULT_MAINTENANCE_HISTORY_PATH = BASE_DIR / "maintenance_history.xlsx"



# Thresholds
CYCLE_DECOMMISSION_LIMIT = 200
CELL_DELTA_WARN_V = 0.001
MAINTENANCE_HOURS_LIMIT = 200
MIN_CELL_VOLTAGE_LIMIT = 3.0  # Minimum safe cell voltage


# ----------------------- Robust Loader -----------------------
@st.cache_data(show_spinner=False)
def load_dataframe(file_or_buffer):
    """
    Robust reader:
    - Detects extension (for paths and uploaded files)
    - Uses proper Excel engines
    - Tries multiple CSV encodings if needed
    """

    def _read_excel(src, ext):
        if ext in (".xlsx", ".xlsm", ".xltx", ".xltm"):
            return pd.read_excel(src, engine="openpyxl")
        elif ext == ".xls":
            try:
                return pd.read_excel(src, engine="xlrd")
            except Exception as e:
                raise RuntimeError("Reading legacy .xls requires 'xlrd' (pip install xlrd).") from e
        else:
            return pd.read_excel(src)

    def _read_csv_with_fallbacks(src):
        for enc in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                return pd.read_csv(src, encoding=enc)
            except UnicodeDecodeError:
                continue
        return pd.read_csv(src, encoding="utf-8", errors="replace")

    if isinstance(file_or_buffer, str):
        path = file_or_buffer
        if not os.path.exists(path):
            raise FileNotFoundError(f"File not found: {path}")

        ext = os.path.splitext(path)[1].lower()
        if ext in (".xlsx", ".xlsm", ".xltx", ".xltm", ".xls"):
            try:
                return _read_excel(path, ext)
            except ImportError as e:
                raise RuntimeError(
                    "Excel engine missing. Install:\n"
                    "  pip install openpyxl  (for .xlsx/.xlsm)\n"
                    "  pip install xlrd      (for legacy .xls)"
                ) from e
        elif ext in (".csv", ".txt"):
            return _read_csv_with_fallbacks(path)
        else:
            try:
                return _read_excel(path, ".xlsx")
            except Exception:
                return _read_csv_with_fallbacks(path)

    name = getattr(file_or_buffer, "name", "uploaded")
    ext = os.path.splitext(name)[1].lower()
    buffer = file_or_buffer

    if ext in (".xlsx", ".xlsm", ".xltx", ".xltm", ".xls"):
        try:
            return _read_excel(buffer, ext)
        except ImportError as e:
            raise RuntimeError(
                "Excel engine missing. Install:\n"
                "  pip install openpyxl  (for .xlsx/.xlsm)\n"
                "  pip install xlrd      (for legacy .xls)"
            ) from e
    elif ext in (".csv", ".txt"):
        return _read_csv_with_fallbacks(buffer)
    else:
        try:
            return _read_excel(buffer, ".xlsx")
        except Exception:
            if hasattr(buffer, "seek"):
                buffer.seek(0)
            return _read_csv_with_fallbacks(buffer)


# ----------------------- Helpers & Parsing -----------------------
def guess_datetime_columns(df: pd.DataFrame):
    candidates = []
    for c in df.columns:
        lc = c.lower()
        if any(k in lc for k in ["date", "time", "start", "timestamp", "log", "flight_date"]):
            candidates.append(c)
    scored = []
    for c in candidates:
        try:
            parsed = pd.to_datetime(df[c], errors="coerce")
            score = parsed.notna().sum()
            if score > 0:
                scored.append((c, score))
        except Exception:
            pass
    scored.sort(key=lambda x: x[1], reverse=True)
    return [c for c, _ in scored]


def guess_duration_columns(df: pd.DataFrame):
    candidates = []
    for c in df.columns:
        lc = c.lower()
        if any(k in lc for k in ["duration", "flight_time", "time_mins", "time_min", "mins", "minutes", "elapsed"]):
            candidates.append(c)
    for c in df.columns:
        if df[c].dtype == object:
            sample = df[c].astype(str).head(30).tolist()
            if any(re.match(r"^\s*\d{1,2}:\d{2}(:\d{2})?\s*$", s) for s in sample):
                candidates.append(c)
    return list(dict.fromkeys(candidates))


def to_datetime_series(s):
    return pd.to_datetime(s, errors="coerce")


def parse_duration_to_minutes(series: pd.Series) -> pd.Series:
    name_hint = (series.name or "").lower()
    if pd.api.types.is_numeric_dtype(series):
        vals = series.astype(float)
        if "sec" in name_hint or "second" in name_hint:
            return vals / 60.0
        return vals.astype(float)

    def parse_one(x):
        if pd.isna(x):
            return np.nan
        s = str(x).strip()
        m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", s)
        if m:
            hh = int(m.group(1));
            mm = int(m.group(2));
            ss = int(m.group(3)) if m.group(3) else 0
            return hh * 60 + mm + ss / 60.0
        try:
            val = float(s.replace(",", ""))
            if "sec" in name_hint or "second" in name_hint:
                return val / 60.0
            return val
        except Exception:
            return np.nan

    return series.map(parse_one)


def minutes_to_hms_str(total_minutes: float):
    if pd.isna(total_minutes):
        return "0h 00m"
    total_seconds = int(round(total_minutes * 60))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    return f"{hours}h {minutes:02d}m"


def month_calendar_dataframe(year: int, month: int, flights_by_day: dict):
    cal = calendar.Calendar(firstweekday=0)
    month_days = cal.monthdayscalendar(year, month)
    while len(month_days) < 6:
        month_days.append([0] * 7)
    matrix = []
    for week in month_days:
        row = []
        for d in week:
            cnt = flights_by_day.get(d, 0) if d != 0 else 0
            row.append((d, cnt))
        matrix.append(row)
    return matrix


# ----------------- Battery helpers -----------------
def _guess_batt_id_cols(df: pd.DataFrame):
    hits = [c for c in df.columns if
            any(k in c.lower() for k in ["battery", "batt", "pack", "sn", "serial", "id", "index"])]
    return list(dict.fromkeys(hits)) or list(df.columns)


def _guess_cycle_cols(df: pd.DataFrame):
    hits = [c for c in df.columns if any(k in c.lower() for k in ["cycle", "charge_count", "charges", "count"])]
    return hits or list(df.columns)


def _guess_datetime_cols(df: pd.DataFrame):
    return guess_datetime_columns(df) or list(df.columns)


def _detect_cell_voltage_cols(df: pd.DataFrame):
    patt = re.compile(r"(cell\s*\d+.*v|^v\d+|cell\d+|cell_?\d+|cell.*voltage)", re.IGNORECASE)
    cand = []
    for c in df.columns:
        if pd.api.types.is_numeric_dtype(df[c]) and patt.search(c):
            cand.append(c)
    common = [c for c in df.columns if
              c.strip().lower() in {"cell1v", "cell2v", "cell3v", "cell4v", "cell5v", "cell6v"}]
    return list(dict.fromkeys(cand + common))


def prepare_batt_voltage_stats(df_raw: pd.DataFrame) -> pd.DataFrame:
    if df_raw is None or df_raw.empty:
        return pd.DataFrame()

    df = df_raw.copy()

    batt_col_candidates = _guess_batt_id_cols(df)
    battery_col = batt_col_candidates[0] if batt_col_candidates else None
    if battery_col is None:
        df["battery_index"] = "battery_1"
    else:
        df["battery_index"] = df[battery_col].astype(str)

    dt_candidates = _guess_datetime_cols(df)
    date_col = dt_candidates[0] if dt_candidates else None
    if date_col is not None:
        df["Date"] = to_datetime_series(df[date_col])
    else:
        df["Date"] = pd.NaT

    lower_cols = {c.lower(): c for c in df.columns}
    min_col = lower_cols.get("min_volt")
    max_col = lower_cols.get("max_volt")

    if (min_col is None) or (max_col is None):
        cell_cols = _detect_cell_voltage_cols(df)
        cell_cols = [c for c in cell_cols if pd.api.types.is_numeric_dtype(df[c])]
        if len(cell_cols) >= 2:
            df["min_volt"] = df[cell_cols].min(axis=1)
            df["max_volt"] = df[cell_cols].max(axis=1)
        else:
            guess_min = [c for c in df.columns if "min" in c.lower() and "v" in c.lower()]
            guess_max = [c for c in df.columns if "max" in c.lower() and "v" in c.lower()]
            if guess_min and guess_max:
                df["min_volt"] = pd.to_numeric(df[guess_min[0]], errors="coerce")
                df["max_volt"] = pd.to_numeric(df[guess_max[0]], errors="coerce")
            else:
                return pd.DataFrame()
    else:
        df["min_volt"] = pd.to_numeric(df[min_col], errors="coerce")
        df["max_volt"] = pd.to_numeric(df[max_col], errors="coerce")

    if "delta" not in df.columns:
        df["delta"] = df["max_volt"] - df["min_volt"]
    else:
        df["delta"] = pd.to_numeric(df["delta"], errors="coerce")
        df["delta"] = df["delta"].fillna(df["max_volt"] - df["min_volt"])

    out = df[["Date", "battery_index", "min_volt", "max_volt", "delta"]].copy()
    out = out.dropna(subset=["Date"])
    return out.sort_values("Date")


def prepare_batt_cycles(df_raw: pd.DataFrame) -> pd.DataFrame:
    if df_raw is None or df_raw.empty:
        return pd.DataFrame()

    df = df_raw.copy()

    batt_col_candidates = _guess_batt_id_cols(df)
    battery_col = batt_col_candidates[0] if batt_col_candidates else None
    if battery_col is None:
        df["battery_index"] = "battery_1"
    else:
        df["battery_index"] = df[battery_col].astype(str)

    cyc_candidates = _guess_cycle_cols(df)
    cycles_col = cyc_candidates[0] if cyc_candidates else None
    if cycles_col is None:
        return pd.DataFrame()

    df["cycles"] = pd.to_numeric(df[cycles_col], errors="coerce")

    dt_candidates = _guess_datetime_cols(df)
    if dt_candidates:
        df["Date"] = to_datetime_series(df[dt_candidates[0]])
    else:
        df["Date"] = pd.NaT

    out = df[["Date", "battery_index", "cycles"]].copy()
    return out


# ------------------ Maintenance history helpers ------------------

def prepare_maintenance_history(df_raw: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize a maintenance history dataframe into a Date column (date type) and keep other columns (Notes, Technician, etc.).
    """
    if df_raw is None or df_raw.empty:
        return pd.DataFrame()
    df = df_raw.copy()

    # Try to find a datetime-like column
    dt_candidates = guess_datetime_columns(df)
    if dt_candidates:
        df['Date'] = to_datetime_series(df[dt_candidates[0]])
    else:
        # fallback: pick first column that looks like a date after coercion
        found = False
        for c in df.columns:
            coerced = to_datetime_series(df[c])
            if coerced.notna().sum() > 0:
                df['Date'] = coerced
                found = True
                break
        if not found:
            # last resort: try a column named 'date' or 'maintenance_date'
            for name_try in ['date', 'maintenance_date', 'maint_date', 'service_date']:
                if name_try in [c.lower() for c in df.columns]:
                    original = [c for c in df.columns if c.lower() == name_try][0]
                    df['Date'] = to_datetime_series(df[original])
                    found = True
                    break
        if not found:
            return pd.DataFrame()

    df = df.dropna(subset=['Date']).copy()
    df['Date'] = df['Date'].dt.date
    df = df.sort_values('Date', ascending=False)
    # Keep Date first column
    cols = ['Date'] + [c for c in df.columns if c != 'Date']
    return df[cols]


# ----------------------- UI -----------------------
st.set_page_config(page_title="Flight Dashboard", layout="wide", initial_sidebar_state="expanded")
st.title("🛰️ Flight Dashboard")

# File input
with st.sidebar:
    st.header("Data")
    file_choice = st.radio("Choose data source", ["Use default path", "Upload file"], horizontal=True)
    data = None
    if file_choice == "Use default path":
        st.caption(f"Default: `{DEFAULT_FILE_PATH}`")
        try:
            data = load_dataframe(DEFAULT_FILE_PATH)
            st.success(f"Loaded {len(data):,} rows from default path.")
        except FileNotFoundError as e:
            st.warning(str(e))
            data = None
        except Exception as e:
            st.error(f"Failed to load default path: {e}")
            data = None
    else:
        up = st.file_uploader("Upload Excel/CSV", type=["xlsx", "xls", "xlsm", "csv", "txt"])
        if up is not None:
            try:
                data = load_dataframe(up)
                st.success(f"Loaded {len(data):,} rows from uploaded file.")
            except Exception as e:
                st.error(f"Failed to read uploaded file: {e}")
                data = None

    # Maintenance history upload / default
    st.markdown("---")
    st.subheader("Maintenance history")
    maintenance_history = None
    mh_choice = st.radio("Maintenance history source", ["Use default path", "Upload file", "None"], index=0,
                         horizontal=True)
    if mh_choice == "Use default path":
        st.caption(f"Default: `{DEFAULT_MAINTENANCE_HISTORY_PATH}`")
        try:
            maintenance_history = load_dataframe(DEFAULT_MAINTENANCE_HISTORY_PATH)
            st.success(f"Loaded maintenance history ({len(maintenance_history):,} rows) from default path.")
        except FileNotFoundError as e:
            st.info(str(e))
            maintenance_history = pd.DataFrame()
        except Exception as e:
            st.info(f"Failed to load maintenance history from default path: {e}")
            maintenance_history = pd.DataFrame()
    elif mh_choice == "Upload file":
        upmh = st.file_uploader("Upload maintenance_history (xlsx/csv)", type=["xlsx", "xls", "csv", "txt"],
                                key="mh_uploader")
        if upmh is not None:
            try:
                maintenance_history = load_dataframe(upmh)
                st.success(f"Loaded maintenance history ({len(maintenance_history):,} rows) from uploaded file.")
            except Exception as e:
                st.error(f"Failed to read uploaded maintenance history: {e}")
                maintenance_history = pd.DataFrame()
    else:
        maintenance_history = pd.DataFrame()

    # Column mapping
    if data is not None:
        st.subheader("Column Mapping")
        dt_candidates = guess_datetime_columns(data)
        dur_candidates = guess_duration_columns(data)

        dt_col = st.selectbox(
            "Date/Datetime column",
            options=[None] + dt_candidates + list(data.columns),
            index=1 if dt_candidates else 0
        )
        dur_col = st.selectbox(
            "Duration column (mins or HH:MM:SS)",
            options=[None] + dur_candidates + list(data.columns),
            index=1 if dur_candidates else 0
        )
    else:
        dt_col = None
        dur_col = None

if data is None:
    st.stop()

# Prepare dataframe
df = data.copy()

# Parse datetime
if dt_col is None:
    st.error("Please select a Date/Datetime column in the sidebar.")
    st.stop()

df["__dt__"] = to_datetime_series(df[dt_col])
df = df[df["__dt__"].notna()].copy()
if df.empty:
    st.error("No valid datetime values after parsing the selected column.")
    st.stop()

df["__date__"] = df["__dt__"].dt.date
df["__year__"] = df["__dt__"].dt.year
df["__month__"] = df["__dt__"].dt.month
df["__day__"] = df["__dt__"].dt.day

# Parse duration -> minutes
if dur_col is None:
    st.warning("No duration column selected. Duration metrics will be 0.")
    df["__mins__"] = 0.0
else:
    df["__mins__"] = parse_duration_to_minutes(df[dur_col].copy()).fillna(0.0)

df["__hours__"] = df["__mins__"] / 60.0

# ================= Tabs =================
tab1, tab2, tab3, tab4 = st.tabs(["📊 General Statistics", "⏱️ Flight Duration", "🔋 Battery Health", "🔧 Maintenance"])

# ----------------------- TAB 1: GENERAL -----------------------
with tab1:
    st.subheader("Overview")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Total Flights", f"{len(df):,}")
    with c2:
        total_mins = df["__mins__"].sum()
        st.metric("Total Duration (hours)", f"{total_mins / 60.0:,.2f}")
    with c3:
        avg_min = df["__mins__"].replace(0, np.nan).mean()
        st.metric("Avg Flight (mins)", f"{avg_min:,.1f}" if not np.isnan(avg_min) else "0.0")
    with c4:
        unique_days = df["__date__"].nunique()
        st.metric("Active Flight Days", f"{unique_days:,}")

    st.markdown("---")
    st.subheader("Flights per Year / Month")
    by_year = df.groupby("__year__").size().rename("flights").reset_index()
    by_month = df.groupby(["__year__", "__month__"]).size().rename("flights").reset_index()

    c5, c6 = st.columns(2)
    with c5:
        st.markdown("**Flights per Year**")
        if not by_year.empty:
            st.bar_chart(by_year.set_index("__year__")["flights"])
        else:
            st.info("No yearly data available.")

    with c6:
        st.markdown("**Flights per Month (stacked by year)**")
        if not by_month.empty:
            piv = by_month.pivot(index="__month__", columns="__year__", values="flights").fillna(0).sort_index()
            st.bar_chart(piv)
        else:
            st.info("No monthly data available.")

    st.markdown("---")
    st.subheader("Duration per Year (hours)")
    dur_year = df.groupby("__year__")["__hours__"].sum().rename("hours").reset_index()
    if not dur_year.empty:
        st.bar_chart(dur_year.set_index("__year__")["hours"])
    else:
        st.info("No duration data available.")

# ----------------------- TAB 2: DURATION + FILTERS + CALENDAR -----------------------
with tab2:
    st.subheader("Filters")

    years = sorted(df["__year__"].unique().tolist())
    months = list(range(1, 13))
    days_all = list(range(1, 32))

    colf1, colf2, colf3 = st.columns(3)
    with colf1:
        sel_years = st.multiselect("Year(s)", years, default=years)
    with colf2:
        sel_months = st.multiselect("Month(s)", months, default=months, format_func=lambda m: calendar.month_abbr[m])
    with colf3:
        sel_days = st.multiselect("Day(s) of month", days_all, default=[])

    f = df[df["__year__"].isin(sel_years) & df["__month__"].isin(sel_months)]
    if sel_days:
        f = f[f["__day__"].isin(sel_days)]

    st.markdown("### Duration Summary (Filtered)")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Flights (filtered)", f"{len(f):,}")
    with c2:
        st.metric("Total Minutes", f"{f['__mins__'].sum():,.1f}")
    with c3:
        st.metric("Total Hours", f"{f['__hours__'].sum():,.2f}")

    st.markdown("---")
    st.markdown("### Daily Duration (minutes)")
    daily = f.groupby("__date__")["__mins__"].sum().reset_index().sort_values("__date__")
    if daily.empty:
        st.info("No data for current filter.")
    else:
        st.line_chart(daily.set_index("__date__")["__mins__"])

    st.markdown("---")
    st.markdown("### Calendar View (select a single month)")

    cal_cols = st.columns(2)
    with cal_cols[0]:
        cal_year = st.selectbox("Calendar Year", options=years, index=max(0, len(years) - 1))
    with cal_cols[1]:
        available_months_for_year = sorted(df.loc[df["__year__"] == cal_year, "__month__"].unique().tolist())
        if not available_months_for_year:
            available_months_for_year = months
        cal_month = st.selectbox(
            "Calendar Month", options=available_months_for_year,
            index=len(available_months_for_year) - 1,
            format_func=lambda m: f"{calendar.month_name[m]} ({m:02d})"
        )

    month_mask = (df["__year__"] == cal_year) & (df["__month__"] == cal_month)
    df_month = df.loc[month_mask].copy()

    metric_choice = st.radio("Highlight by", ["Flights Count", "Total Minutes"], horizontal=True)
    if metric_choice == "Flights Count":
        agg = df_month.groupby("__day__").size().to_dict()
        legend_label = "Flights"
    else:
        agg = df_month.groupby("__day__")["__mins__"].sum().to_dict()
        legend_label = "Minutes"

    mat = month_calendar_dataframe(cal_year, cal_month, agg)

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.set_title(f"{calendar.month_name[cal_month]} {cal_year} — {legend_label}")
    ax.axis("off")

    nrows, ncols = len(mat), 7
    cell_w, cell_h = 1.0 / ncols, 1.0 / nrows

    vals = [cnt for row in mat for (_, cnt) in row]
    vmin, vmax = (min(vals), max(vals)) if vals else (0, 0)


    def norm(x):
        if vmax == vmin:
            return 0.2 if x > 0 else 0.0
        return 0.15 + 0.75 * (x - vmin) / (vmax - vmin)


    for r in range(nrows):
        for c in range(ncols):
            day_num, cnt = mat[r][c]
            x0 = c * cell_w
            y0 = 1 - (r + 1) * cell_h
            shade = norm(cnt)
            rect = plt.Rectangle((x0, y0), cell_w, cell_h, fill=True, alpha=shade)
            ax.add_patch(rect)
            if day_num != 0:
                ax.text(x0 + 0.02, y0 + 0.75 * cell_h, str(day_num), fontsize=10, va="top", ha="left")
                if cnt > 0:
                    val_str = f"{int(cnt)}" if isinstance(cnt, (int, np.integer)) else f"{round(cnt, 1)}"
                    ax.text(x0 + 0.5 * cell_w, y0 + 0.35 * cell_h, val_str, fontsize=10, ha="center", va="center")

    for c, wd in enumerate(list(calendar.day_abbr)):
        x0 = c * cell_w
        ax.text(x0 + 0.5 * cell_w, 1.02, wd, ha="center", va="bottom", fontsize=10)

    st.pyplot(fig)

# ----------------------- TAB 3: BATTERY HEALTH -----------------------
with tab3:
    st.subheader("Battery Health")

    try:
        raw_batt_volt = load_dataframe(DEFAULT_BATT_VOLT_PATH)
    except Exception as e:
        raw_batt_volt = pd.DataFrame()
        st.info(f"No battery_voltage_stats file loaded from default path. ({e})")

    try:
        raw_batt_cycles = load_dataframe(DEFAULT_BATT_CYCLES_PATH)
    except Exception as e:
        raw_batt_cycles = pd.DataFrame()
        st.info(f"No battery_cycles file loaded from default path. ({e})")

    batt_stats = prepare_batt_voltage_stats(raw_batt_volt)
    batt_cycles = prepare_batt_cycles(raw_batt_cycles)

    if not batt_stats.empty:
        cs = batt_stats.dropna(subset=["Date"]).copy()
        col1, col2 = st.columns(2)
        with col1:
            if "min_volt" in cs.columns:
                fig1 = px.line(cs, x="Date", y="min_volt", color="battery_index",
                               title="Min Cell Voltage by Battery")

                # Add horizontal red line at 3.0V
                fig1.add_hline(y=MIN_CELL_VOLTAGE_LIMIT,
                               line_dash="dash",
                               line_color="red",
                               annotation_text=f"Min Safe Voltage ({MIN_CELL_VOLTAGE_LIMIT}V)",
                               annotation_position="right")

                st.plotly_chart(fig1, use_container_width=True)
            else:
                st.info("battery_voltage_stats missing 'min_volt' after preparation.")
        with col2:
            if "delta" in cs.columns:
                fig2 = px.line(cs, x="Date", y="delta", color="battery_index",
                               title="Imbalance Δ (max - min) by Battery")
                st.plotly_chart(fig2, use_container_width=True)
            else:
                st.info("battery_voltage_stats missing computed 'delta' after preparation.")

        st.dataframe(batt_stats, use_container_width=True)
    else:
        st.info("No battery_voltage_stats available (after preparation) from the default file.")

    # Battery Cycles with RED LINE at 200
    if not batt_cycles.empty and {"Date", "battery_index", "cycles"}.issubset(batt_cycles.columns):
        st.markdown("**Battery Cycles Over Time**")
        bc = batt_cycles.dropna(subset=["Date"]).sort_values("Date")
        if not bc.empty:
            # Create plotly figure
            figc = px.line(bc, x="Date", y="cycles", color="battery_index",
                           title="Battery Cycles Over Time")

            # Add horizontal red line at 200 cycles
            figc.add_hline(y=CYCLE_DECOMMISSION_LIMIT,
                           line_dash="dash",
                           line_color="red",
                           annotation_text=f"Decommission Limit ({CYCLE_DECOMMISSION_LIMIT} cycles)",
                           annotation_position="right")

            st.plotly_chart(figc, use_container_width=True)
        st.dataframe(batt_cycles, use_container_width=True)

    st.caption(
        f"Notes: "
        f"• Batteries at/over **{CYCLE_DECOMMISSION_LIMIT} cycles** are typically flagged for decommissioning (SOP dependent). "
        f"• Cell ΔV warning often around **{CELL_DELTA_WARN_V:.2f} V**; adjust per your SOP."
    )

    # Battery warnings
    st.markdown("### Battery Warnings")

    if not batt_cycles.empty and {"battery_index", "cycles"}.issubset(batt_cycles.columns):
        latest_cycles = (
            batt_cycles.groupby("battery_index")["cycles"]
            .max()
            .reset_index()
            .sort_values("cycles", ascending=False)
        )
        crossed = latest_cycles[latest_cycles["cycles"] >= CYCLE_DECOMMISSION_LIMIT]
        if not crossed.empty:
            st.warning(
                f"⚠️ {len(crossed)} battery(ies) at or beyond **{CYCLE_DECOMMISSION_LIMIT} cycles**."
            )
            st.dataframe(
                crossed.rename(columns={"battery_index": "Battery", "cycles": "Max Cycles"}),
                use_container_width=True,
            )
        else:
            st.success(f"No batteries have crossed **{CYCLE_DECOMMISSION_LIMIT}** cycles.")

    if not batt_stats.empty and {"battery_index", "Date", "delta"}.issubset(batt_stats.columns):
        high_delta = batt_stats[batt_stats["delta"] > CELL_DELTA_WARN_V].copy()
        if not high_delta.empty:
            flagged_batts = sorted(high_delta["battery_index"].unique().tolist())
            st.warning(
                f"⚠️ {len(flagged_batts)} battery(ies) observed with imbalance ΔV > **{CELL_DELTA_WARN_V:.2f} V**."
            )

            latest_exceed = (
                high_delta.sort_values(["battery_index", "Date"])
                .groupby("battery_index", as_index=False)
                .tail(1)
                .sort_values("delta", ascending=False)
            )
            st.dataframe(
                latest_exceed[["battery_index", "Date", "min_volt", "max_volt", "delta"]]
                .rename(
                    columns={
                        "battery_index": "Battery",
                        "Date": "Last exceedance",
                        "min_volt": "Min (V)",
                        "max_volt": "Max (V)",
                        "delta": "ΔV (V)",
                    }
                ),
                use_container_width=True,
            )
        else:
            st.success(f"No imbalance samples above **{CELL_DELTA_WARN_V:.2f} V**.")

# ----------------------- TAB 4: MAINTENANCE -----------------------
with tab4:
    st.subheader("🔧 Drone Maintenance Tracker")

    st.markdown("""
    Track drone maintenance intervals and monitor flight hours since last maintenance.
    The recommended maintenance interval is **200 flight hours**.
    """)

    # Date selector for last maintenance
    min_date = df["__date__"].min()
    max_date = df["__date__"].max()

    # Prepare maintenance history if provided
    maintenance_history_prepared = prepare_maintenance_history(maintenance_history) if (
                'maintenance_history' in locals() and isinstance(maintenance_history, pd.DataFrame)) else pd.DataFrame()

    latest_maint_date = None
    if not maintenance_history_prepared.empty:
        latest_maint_date = maintenance_history_prepared.iloc[0]['Date']

    col1, col2 = st.columns([2, 1])
    with col1:
        # Allow user to choose between using the latest history date or entering/selecting their own date
        if latest_maint_date is not None:
            use_latest = st.checkbox(f"Use latest maintenance from history ({latest_maint_date})", value=True)
        else:
            use_latest = False

        if use_latest and latest_maint_date is not None:
            last_maintenance = latest_maint_date
            st.write(f"Using maintenance history date: **{last_maintenance}**")
        else:
            last_maintenance = st.date_input(
                "Select Last Maintenance Date",
                value=min_date,
                min_value=min_date,
                max_value=max_date,
                help="Select the date when the drone was last sent for maintenance"
            )

    # Show maintenance history table (if available)
    if not maintenance_history_prepared.empty:
        st.markdown("### Maintenance History")
        with st.expander("View maintenance history (most recent first)"):
            st.dataframe(maintenance_history_prepared, use_container_width=True)

    # Calculate hours since maintenance
    df_since_maintenance = df[df["__date__"] >= last_maintenance].copy()

    total_hours_since = df_since_maintenance["__hours__"].sum()
    total_flights_since = len(df_since_maintenance)
    days_since = (max_date - last_maintenance).days

    # Display metrics
    st.markdown("### Maintenance Status")
    metric_col1, metric_col2, metric_col3, metric_col4 = st.columns(4)

    with metric_col1:
        st.metric("Hours Since Maintenance", f"{total_hours_since:,.2f}")
    with metric_col2:
        hours_remaining = max(0, MAINTENANCE_HOURS_LIMIT - total_hours_since)
        st.metric("Hours Until Maintenance", f"{hours_remaining:,.2f}")
    with metric_col3:
        st.metric("Flights Since Maintenance", f"{total_flights_since:,}")
    with metric_col4:
        st.metric("Days Since Maintenance", f"{days_since:,}")

    # Warning if approaching or over limit
    if total_hours_since >= MAINTENANCE_HOURS_LIMIT:
        st.error(f"⚠️ **MAINTENANCE REQUIRED!** Drone has exceeded {MAINTENANCE_HOURS_LIMIT} flight hours.")
    elif total_hours_since >= MAINTENANCE_HOURS_LIMIT * 0.8:
        st.warning(f"⚠️ **Approaching maintenance interval.** Consider scheduling maintenance soon.")
    else:
        st.success(f"✅ Drone is within maintenance interval.")

    # Predictive Maintenance Section
    # st.markdown("---")
    # st.markdown("### 🔮 Predictive Maintenance")

    # # Calculate flight activity trends
    # if len(df_since_maintenance) >= 2:
    #     # Calculate average hours per day based on recent activity
    #     days_with_flights = df_since_maintenance.groupby("__date__")["__hours__"].sum()

    #     # Get last 30 days of activity (or all available if less than 30)
    #     recent_days = min(30, len(days_with_flights))
    #     recent_activity = days_with_flights.tail(recent_days)

    #     avg_hours_per_day = recent_activity.mean()

    #     # Calculate when maintenance will be due
    #     hours_remaining = max(0, MAINTENANCE_HOURS_LIMIT - total_hours_since)

    #     if avg_hours_per_day > 0:
    #         days_until_maintenance = hours_remaining / avg_hours_per_day
    #         predicted_maintenance_date = max_date + timedelta(days=int(days_until_maintenance))

    #         pred_col1, pred_col2, pred_col3 = st.columns(3)

    #         with pred_col1:
    #             st.metric(
    #                 "Avg Flight Hours/Day",
    #                 f"{avg_hours_per_day:.2f}",
    #                 help=f"Based on last {recent_days} days with flights"
    #             )

    #         with pred_col2:
    #             if days_until_maintenance > 0:
    #                 st.metric(
    #                     "Est. Days Until Maintenance",
    #                     f"{int(days_until_maintenance)}",
    #                     help="Estimated based on recent flight activity"
    #                 )
    #             else:
    #                 st.metric(
    #                     "Est. Days Until Maintenance",
    #                     "Overdue",
    #                     delta=f"{int(abs(days_until_maintenance))} days over"
    #                 )

    #         with pred_col3:
    #             if days_until_maintenance > 0:
    #                 st.metric(
    #                     "Predicted Maintenance Date",
    #                     predicted_maintenance_date.strftime("%Y-%m-%d"),
    #                     help="Estimated date when 200 hours will be reached"
    #                 )
    #             else:
    #                 st.metric(
    #                     "Predicted Maintenance Date",
    #                     "Immediate",
    #                     delta="Maintenance overdue"
    #                 )

    #         # Visual indicator
    #         if days_until_maintenance > 0:
    #             if days_until_maintenance <= 7:
    #                 st.warning(
    #                     f"⏰ **Maintenance due within a week!** Schedule maintenance by {predicted_maintenance_date.strftime('%B %d, %Y')}.")
    #             elif days_until_maintenance <= 14:
    #                 st.info(
    #                     f"📅 Maintenance recommended by {predicted_maintenance_date.strftime('%B %d, %Y')} (approximately {int(days_until_maintenance)} days).")
    #             else:
    #                 st.success(
    #                     f"✅ Next maintenance estimated around {predicted_maintenance_date.strftime('%B %d, %Y')} (approximately {int(days_until_maintenance)} days away).")

    #         # Show prediction details
    #         with st.expander("ℹ️ How is this calculated?"):
    #             st.markdown(f"""
    #             **Prediction Method:**
    #             - Analyzing flight activity from the last **{recent_days} days** with recorded flights
    #             - Average flight hours per day: **{avg_hours_per_day:.2f} hours**
    #             - Hours remaining until maintenance: **{hours_remaining:.2f} hours**
    #             - Estimated days until limit: **{hours_remaining:.2f} ÷ {avg_hours_per_day:.2f} = {days_until_maintenance:.1f} days**

    #             **Note:** This is an estimate based on recent flight patterns. Actual maintenance timing may vary based on:
    #             - Changes in flight frequency
    #             - Seasonal variations in operations
    #             - Unexpected maintenance needs
    #             """)
    #     else:
    #         st.info("No recent flight activity detected. Unable to predict maintenance schedule.")
    # else:
    #     st.info("Insufficient flight data for predictions. Need at least 2 flights since last maintenance.")

    # Graph: Cumulative hours since maintenance
    st.markdown("### Cumulative Flight Hours Since Maintenance")

    if not df_since_maintenance.empty:
        # Calculate cumulative hours
        daily_hours = df_since_maintenance.groupby("__date__")["__hours__"].sum().reset_index()
        daily_hours = daily_hours.sort_values("__date__")
        daily_hours["cumulative_hours"] = daily_hours["__hours__"].cumsum()

        # Create plotly figure
        fig_maint = go.Figure()

        # Add cumulative hours line
        fig_maint.add_trace(go.Scatter(
            x=daily_hours["__date__"],
            y=daily_hours["cumulative_hours"],
            mode='lines+markers',
            name='Cumulative Hours',
            line=dict(width=2),
            marker=dict(size=6)
        ))

        # Add horizontal red line at 200 hours
        fig_maint.add_hline(
            y=MAINTENANCE_HOURS_LIMIT,
            line_dash="dash",
            line_color="red",
            line_width=3,
            annotation_text=f"Maintenance Required ({MAINTENANCE_HOURS_LIMIT} hours)",
            annotation_position="right"
        )

        # Update layout
        fig_maint.update_layout(
            title=f"Cumulative Flight Hours Since {last_maintenance}",
            xaxis_title="Date",
            yaxis_title="Cumulative Hours",
            hovermode='x unified',
            showlegend=True,
            height=500
        )

        st.plotly_chart(fig_maint, use_container_width=True)

        # Show detailed data table
        with st.expander("View Detailed Flight Data Since Maintenance"):
            display_df = df_since_maintenance[["__date__", "__mins__", "__hours__"]].copy()
            display_df = display_df.rename(columns={
                "__date__": "Date",
                "__mins__": "Duration (minutes)",
                "__hours__": "Duration (hours)"
            })
            st.dataframe(display_df, use_container_width=True)
    else:
        st.info("No flights recorded since the selected maintenance date.")

st.caption(
    "Tip: Select your Date/Datetime and Duration columns in the sidebar. HH:MM(:SS) durations are auto-converted to minutes.")

