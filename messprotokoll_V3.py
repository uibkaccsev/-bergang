#!/usr/bin/env python3
"""
messprotokoll_V3.py  –  VESC Testbench Measurement Report  (V3)

Generates a 2-page A4 PDF:
  Page 1:  Header (motor, Mat.Nr., date, PASS/FAIL)
           RPM table  (abs values)
           Current tables  (drive + load)
           Torque graph  (same style as V1)
  Page 2:  BEMF full waveform
           BEMF zoomed (2-3 electrical periods)
           BEMF peaks/valleys table

Also writes  analysis_result.json  (needed by messablauf.py / gui_launcher).
"""

import argparse
import csv
import json
import sys
from datetime import datetime
from math import isnan
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.patches as mpatches
import numpy as np
from matplotlib.backends.backend_pdf import PdfPages
from scipy.signal import find_peaks

# ---------------------------------------------------------------------------
# Paths (relative to script location / exe location)
# ---------------------------------------------------------------------------
if getattr(sys, 'frozen', False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_DIR         = SCRIPT_DIR / "results"
CONFIG_DIR          = SCRIPT_DIR / "config"
REPORTS_DIR         = SCRIPT_DIR.parent / "Messberichte"   # one level up: Motorprüfstand/Messberichte
RESULTS_DIR.mkdir(exist_ok=True)
CONFIG_DIR.mkdir(exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)
VESC_CSV_PATH       = RESULTS_DIR / "measurement.csv"
OSZI_CSV_PATH       = RESULTS_DIR / "OscilloscopeStream.csv"
TORQUE_CSV_PATH     = RESULTS_DIR / "dm_avg.csv"
MOTORAUSWAHL_PATH   = CONFIG_DIR  / "Motorauswahl.json"
ANALYSIS_JSON_PATH  = RESULTS_DIR / "analysis_result.json"

SAMPLE_RATE = 100_000          # Hz  (oscilloscope)
PASS_THRESHOLD_PCT = 10.0      # % deviation for pass/fail vs. baseline
ZOOM_PERIODS = 3               # number of electrical periods in zoomed BEMF view

# Thresholds for new diagnostics
PHASE_TIMING_SYMMETRY_TOL_PCT = 5.0   # max allowed deviation between deltas
THD_WARN_PCT                  = 10.0  # THD above this is flagged
PHASE_BALANCE_WARN_PCT        = 5.0   # amplitude CV above this is flagged

COLORS = ["#2980b9", "#e67e22", "#27ae60", "#8e44ad", "#c0392b"]


# ═══════════════════════════════════════════════════════════════════════════
#  Data loading
# ═══════════════════════════════════════════════════════════════════════════

def _pf(s):
    """Parse a string to float, return NaN on failure."""
    try:
        return float(s)
    except (ValueError, TypeError):
        return float("nan")


def load_vesc(path: Path) -> dict | None:
    """Load measurement.csv into a dict of lists."""
    if not path.exists():
        print(f"  [WARN] VESC data not found: {path}")
        return None
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        data: dict[str, list] = {}
        for row in reader:
            for k, v in row.items():
                data.setdefault(k, []).append(_pf(v))
    return data if data else None


def load_oszi(path: Path) -> dict | None:
    """Load OscilloscopeStream.csv  (semicolon-delimited)."""
    if not path.exists():
        print(f"  [WARN] Oscilloscope data not found: {path}")
        return None
    channels: dict[str, list[float]] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip().rstrip(";")
            if not line or line.startswith("Sample"):
                parts = line.rstrip(";").split(";")
                if len(parts) > 1:
                    ch_names = parts[1:]
                continue
            parts = line.split(";")
            if len(parts) < 2:
                continue
            for i, name in enumerate(ch_names, start=1):
                if i < len(parts):
                    channels.setdefault(name, []).append(_pf(parts[i]))
    return channels if channels else None


def load_torque(path: Path) -> dict | None:
    """Load dm_avg.csv (torque sensor via DM voltage)."""
    if not path.exists():
        print(f"  [WARN] Torque data not found: {path}")
        return None
    times, vals = [], []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            times.append(_pf(row.get("timestamp_s", "")))
            vals.append(_pf(row.get("Ch4", "")))
    if not vals:
        return None
    return {"time": times, "torque": vals}


def load_json(path: Path):
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ═══════════════════════════════════════════════════════════════════════════
#  Statistics helpers
# ═══════════════════════════════════════════════════════════════════════════

def _clean(vals):
    """Return numpy array without NaN."""
    a = np.array(vals, dtype=float)
    return a[~np.isnan(a)]


def stat_block(vals):
    """Return dict with min, max, mean, std, n for a list of numbers."""
    c = _clean(vals)
    if len(c) == 0:
        return None
    return {
        "min": float(np.min(c)),
        "max": float(np.max(c)),
        "mean": float(np.mean(c)),
        "std": float(np.std(c)),
        "n": len(c),
    }


def moving_avg(vals, radius=5):
    """Simple centred moving average."""
    out = []
    for i in range(len(vals)):
        lo = max(0, i - radius)
        hi = min(len(vals), i + radius + 1)
        sl = [v for v in vals[lo:hi] if not isnan(v)]
        out.append(sum(sl) / len(sl) if sl else float("nan"))
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  BEMF peak / valley detection  (same algorithm as V1 with outlier filter)
# ═══════════════════════════════════════════════════════════════════════════

def _estimate_period_samples(arr: np.ndarray) -> float:
    """Robust period estimation in samples via FFT of the mean-removed signal."""
    N = len(arr)
    if N < 8:
        return float(N)

    # Apply Hanning window to reduce spectral leakage, consistent with
    # analyze_magnet_symmetry.
    window   = np.hanning(N)
    spectrum = np.abs(np.fft.rfft((arr - arr.mean()) * window))
    freqs    = np.fft.rfftfreq(N)

    min_freq_idx = 1
    max_freq_idx = max(2, N // 10)
    search = spectrum[min_freq_idx:max_freq_idx]

    if len(search) == 0:
        return float(N // 4)

    dom_idx = np.argmax(search) + min_freq_idx
    period  = 1.0 / freqs[dom_idx] if freqs[dom_idx] > 0 else float(N // 4)
    return float(np.clip(period, 4, N // 2))


def analyze_magnet_symmetry(arr: np.ndarray) -> float:
    """Returns the maximum sub-harmonic magnitude (relative to fundamental) as a percentage (0-100)."""
    N = len(arr)
    if N < 8:
        return 0.0

    arr_centered = arr - arr.mean()
    window = np.hanning(N)

    spectrum = np.abs(np.fft.rfft(arr_centered * window))

    min_freq_idx = 1
    max_freq_idx = max(2, N // 10)
    search = spectrum[min_freq_idx:max_freq_idx]

    if len(search) == 0:
        return 0.0

    dom_idx = np.argmax(search) + min_freq_idx

    sub_start_idx = max(2, int(dom_idx * 0.05))
    sub_end_idx   = max(sub_start_idx + 1, int(dom_idx * 0.6))

    if sub_end_idx > sub_start_idx:
        sub_search = spectrum[sub_start_idx:sub_end_idx]
        if len(sub_search) > 0:
            return float(np.max(sub_search) / spectrum[dom_idx]) * 100.0

    return 0.0


def compute_thd(arr: np.ndarray) -> float:
    """
    Compute BEMF Total Harmonic Distortion (2nd–10th harmonic) in percent.
    """
    N = len(arr)
    if N < 8:
        return 0.0

    arr_centered = arr - arr.mean()
    window       = np.hanning(N)
    spectrum     = np.abs(np.fft.rfft(arr_centered * window))

    min_freq_idx = 1
    max_freq_idx = max(2, N // 10)
    search       = spectrum[min_freq_idx:max_freq_idx]
    if len(search) == 0:
        return 0.0

    dom_idx  = int(np.argmax(search)) + min_freq_idx
    fund_mag = spectrum[dom_idx]
    if fund_mag < 1e-9:
        return 0.0

    harmonic_mags = np.array([
        spectrum[min(dom_idx * k, len(spectrum) - 1)]
        for k in range(2, 11)
    ])
    return float(np.sqrt(np.sum(harmonic_mags ** 2)) / fund_mag * 100.0)


def phase_balance(per_ch: dict) -> float:
    """
    Return amplitude imbalance across BEMF channels as CV in percent.
    """
    amps = [float(np.ptp(info["arr"]))
            for info in per_ch.values()
            if "arr" in info and len(info["arr"]) > 0]
    if len(amps) < 2:
        return 0.0
    mean_amp = float(np.mean(amps))
    if mean_amp < 1e-9:
        return 0.0
    return float(np.std(amps) / mean_amp * 100.0)


# ← CHANGED: now returns structured data (ok, deltas_ms, max_dev_pct)
#   instead of a pre-formatted string, so build_page2 can place each
#   value into its own table cell without overflow.
def evaluate_phase_timing(analysis_stats: dict) -> tuple[bool, list[float], float]:
    """
    Compare delta_p1_p2, delta_p2_p3, delta_p3_p1 for symmetry.

    Returns:
        ok          – True when all deltas are within PHASE_TIMING_SYMMETRY_TOL_PCT
        deltas_ms   – [d12, d23, d31] in milliseconds  (0.0 when unavailable)
        max_dev_pct – largest deviation from the mean in percent
    """
    keys   = ("delta_p1_p2", "delta_p2_p3", "delta_p3_p1")
    deltas = [analysis_stats.get(k, 0.0) for k in keys]

    if all(d == 0.0 for d in deltas):
        return True, [0.0, 0.0, 0.0], 0.0

    mean_d = float(np.mean(deltas))
    if mean_d < 1e-9:
        return True, [d * 1000.0 for d in deltas], 0.0

    deviations  = [abs(d - mean_d) / mean_d * 100.0 for d in deltas]
    max_dev_pct = max(deviations)
    ok          = max_dev_pct <= PHASE_TIMING_SYMMETRY_TOL_PCT
    deltas_ms   = [d * 1000.0 for d in deltas]
    return ok, deltas_ms, max_dev_pct


def _enforce_alternation(arr: np.ndarray,
                         peaks: np.ndarray,
                         valleys: np.ndarray):
    """Enforce strict peak-valley alternation; keep the strongest when duplicates exist."""
    if len(peaks) == 0 or len(valleys) == 0:
        return peaks, valleys

    items = [(int(i), 'p') for i in peaks] + [(int(i), 'v') for i in valleys]
    items.sort(key=lambda x: x[0])

    result: list[tuple[int, str]] = []
    for pos, typ in items:
        if result and result[-1][1] == typ:
            prev_pos, _ = result[-1]
            if typ == 'p':
                if arr[pos] > arr[prev_pos]:
                    result[-1] = (pos, typ)
            else:
                if arr[pos] < arr[prev_pos]:
                    result[-1] = (pos, typ)
        else:
            result.append((pos, typ))

    peaks_out   = np.array([p for p, t in result if t == 'p'], dtype=int)
    valleys_out = np.array([p for p, t in result if t == 'v'], dtype=int)
    return peaks_out, valleys_out


def detect_peaks_valleys(values, _unused_smooth=None, _unused_check=None, _unused_dist=None):
    arr = np.array(values, dtype=float)
    if len(arr) < 8:
        return np.array([], dtype=int), np.array([], dtype=int)

    period_samples = _estimate_period_samples(arr)
    min_distance   = max(4, int(period_samples * 0.05))
    amplitude      = np.nanmax(arr) - np.nanmin(arr)
    min_prominence = max(0.2, amplitude * 0.03)

    peaks,   _  = find_peaks( arr, distance=min_distance, prominence=min_prominence)
    valleys, _  = find_peaks(-arr, distance=min_distance, prominence=min_prominence)

    return peaks, valleys


def filter_outliers(indices, values, tol=0.08):
    """Drop indices whose value deviates >tol from the median."""
    if len(indices) < 3:
        return indices
    v = values[indices]
    med = np.median(v)
    if abs(med) < 1e-9:
        return indices
    keep = np.abs(v - med) <= tol * np.abs(med)
    return indices[keep]


# ═══════════════════════════════════════════════════════════════════════════
#  analysis_result.json  (identical schema to V1 – required by GUI)
# ═══════════════════════════════════════════════════════════════════════════

def build_analysis_result(vesc_data, oszi_data):
    """Compute and write analysis_result.json."""
    stats: dict = {}

    if vesc_data and "i_motor_drive_A" in vesc_data:
        c = _clean(vesc_data["i_motor_drive_A"])
        stats["current_under_load"] = float(np.mean(c)) if len(c) else 0.0
    else:
        stats["current_under_load"] = 0.0

    ch_map = {"Ch1": "1", "Ch2": "2", "Ch3": "3"}
    full_peak_times: dict[str, np.ndarray] = {}
    full_valley_times: dict[str, np.ndarray] = {}

    if oszi_data:
        n_samples = max(len(v) for v in oszi_data.values())
        times = np.arange(n_samples) / SAMPLE_RATE

        t_ref = 0.0

        processed: dict = {}
        for ch in ("Ch1", "Ch2", "Ch3"):
            vals = oszi_data.get(ch)
            if not vals:
                continue
            arr = np.array(vals, dtype=float)
            pk, vl = detect_peaks_valleys(arr)
            processed[ch] = {"values": arr, "peaks": pk, "valleys": vl}

        if "Ch1" in processed and len(processed["Ch1"]["peaks"]) > 0:
            t_ref = times[processed["Ch1"]["peaks"][0]]

        for ch, info in processed.items():
            if ch not in ch_map:
                continue
            pk  = info["peaks"]
            vl  = info["valleys"]
            arr = info["values"]

            if len(pk):
                pk = pk[times[pk] >= (t_ref - 1e-7)]
            if len(vl):
                vl = vl[times[vl] >= (t_ref - 1e-7)]

            pk = filter_outliers(pk, arr)
            vl = filter_outliers(vl, arr)

            if len(pk):
                full_peak_times[ch] = times[pk]
            if len(vl):
                full_valley_times[ch] = times[vl]

            num    = ch_map[ch]
            p_vals = arr[pk]
            if len(p_vals) > 2:
                p_vals = np.sort(p_vals)[1:-1]
            stats[f"BEMF_{num}_P"] = float(np.mean(p_vals)) if len(p_vals) else 0.0

            v_vals = arr[vl]
            if len(v_vals) > 2:
                v_vals = np.sort(v_vals)[1:-1]
            stats[f"BEMF_{num}_V"] = float(np.mean(v_vals)) if len(v_vals) else 0.0

    else:
        for num in ("1", "2", "3"):
            stats[f"BEMF_{num}_P"] = 0.0
            stats[f"BEMF_{num}_V"] = 0.0

    def diff_next(d, k1, k2):
        if k1 in d and k2 in d:
            ta, tb = d[k1], d[k2]
            if len(ta) == 0 or len(tb) == 0:
                return 0.0
            cand = tb[tb > ta[0]]
            return float(cand[0] - ta[0]) if len(cand) else 0.0
        return 0.0

    stats["delta_p1_p2"] = diff_next(full_peak_times, "Ch1", "Ch2")
    stats["delta_p2_p3"] = diff_next(full_peak_times, "Ch2", "Ch3")
    stats["delta_p3_p1"] = diff_next(full_peak_times, "Ch3", "Ch1")
    stats["delta_v1_v2"] = diff_next(full_valley_times, "Ch1", "Ch2")
    stats["delta_v2_v3"] = diff_next(full_valley_times, "Ch2", "Ch3")
    stats["delta_v3_v1"] = diff_next(full_valley_times, "Ch3", "Ch1")

    try:
        with open(ANALYSIS_JSON_PATH, "w") as fh:
            json.dump(stats, fh, indent=4)
        print(f"  analysis_result.json written.")
    except Exception as exc:
        print(f"  [WARN] Failed to write analysis_result.json: {exc}")

    return stats


# ═══════════════════════════════════════════════════════════════════════════
#  PASS / FAIL determination  (10 % deviation from Motorauswahl baseline)
# ═══════════════════════════════════════════════════════════════════════════

BASELINE_KEYS = [
    "BEMF_1_V", "BEMF_2_V", "BEMF_3_V",
    "BEMF_1_P", "BEMF_2_P", "BEMF_3_P",
    "current_under_load",
]


def determine_verdict(analysis, motor_entry):
    """Return ("PASS"|"FAIL"|"NO BASELINE", details_list)."""
    if motor_entry is None:
        return "NO BASELINE", []

    has_baseline = all(motor_entry.get(k) is not None for k in BASELINE_KEYS)
    if not has_baseline:
        return "NO BASELINE", []

    details = []
    fail = False
    for k in BASELINE_KEYS:
        base = motor_entry[k]
        meas = analysis.get(k, 0.0)
        if abs(base) < 0.001:
            pct = 0.0
        else:
            pct = abs((meas - base) / base) * 100.0
        ok = pct < PASS_THRESHOLD_PCT
        if not ok:
            fail = True
        details.append({
            "key": k, "baseline": base, "measured": meas,
            "pct": pct, "ok": ok,
        })
    return ("FAIL" if fail else "PASS"), details


# ═══════════════════════════════════════════════════════════════════════════
#  Page 1  –  Header · RPM · Currents · Torque
# ═══════════════════════════════════════════════════════════════════════════

def _fmt(v, decimals=2):
    if v is None:
        return "---"
    return f"{v:.{decimals}f}"


def build_page1(fig, motor_name, mat_nr, date_str, verdict, verdict_details,
                vesc_data, torque_data, user_comment=""):
    """Compose the first page of the report."""

    gs = gridspec.GridSpec(
        5, 2, figure=fig,
        height_ratios=[0.22, 0.45, 0.55, 0.55, 1.3],
        hspace=0.55, wspace=0.35,
        left=0.07, right=0.97, top=0.97, bottom=0.04,
    )

    # ── 0  HEADER ────────────────────────────────────────────────────────
    ax_hdr = fig.add_subplot(gs[0, :])
    ax_hdr.axis("off")

    ax_hdr.add_patch(mpatches.FancyBboxPatch(
        (0, 0), 1, 1, boxstyle="round,pad=0.02",
        transform=ax_hdr.transAxes, facecolor="#2c3e50",
        edgecolor="none", clip_on=False))

    ax_hdr.text(
        0.5, 0.78,
        "Vesc Prüfstand  –  Messbericht",
        transform=ax_hdr.transAxes, ha="center", va="center",
        fontsize=13, weight="bold", color="white")

    sub = f"Motor: {motor_name}   |   Mat.Nr.: {mat_nr}   |   {date_str}"
    ax_hdr.text(
        0.5, 0.45, sub,
        transform=ax_hdr.transAxes, ha="center", va="center",
        fontsize=9, color="#ecf0f1")
        
    comment_text = f"Kommentar: {user_comment}" if user_comment else ""
    ax_hdr.text(
        0.5, 0.15, comment_text,
        transform=ax_hdr.transAxes, ha="center", va="center",
        fontsize=9, color="#f39c12", weight="bold")

    v_col = {"PASS": "#27ae60", "FAIL": "#c0392b"}.get(verdict, "#7f8c8d")
    ax_hdr.text(
        0.97, 0.50, verdict,
        transform=ax_hdr.transAxes, ha="right", va="center",
        fontsize=12, weight="bold", color=v_col,
        bbox=dict(boxstyle="round,pad=0.25", facecolor="white",
                  edgecolor=v_col, linewidth=1.5))

    # ── 1  RPM TABLE ─────────────────────────────────────────────────────
    ax_rpm = fig.add_subplot(gs[1, :])
    ax_rpm.axis("off")
    ax_rpm.set_title("RPM  (Erpm-Werte)", fontsize=10,
                     weight="bold", loc="left", pad=6)

    rpm_rows = []
    rpm_colors = []
    if vesc_data:
        for label, key in [("Drive RPM", "rpm_drive"),
                           ("Load RPM", "rpm_load")]:
            raw  = vesc_data.get(key, [])
            absv = [abs(v) for v in raw if not isnan(v)]
            s    = stat_block(absv)
            if s:
                rpm_rows.append([label,
                                 _fmt(s["min"], 0),
                                 _fmt(s["max"], 0),
                                 _fmt(s["mean"], 1),
                                 _fmt(s["std"], 2)])
            else:
                rpm_rows.append([label, "---", "---", "---", "---"])
            rpm_colors.append(["#eaf0fb"] * 5)

        cmd = [v for v in vesc_data.get("rpm_command", []) if not isnan(v)]
        if cmd:
            c_mean = np.mean(cmd)
            rpm_rows.append(["Solldrehzahl", "---", "---",
                             _fmt(c_mean, 0), "---"])
            rpm_colors.append(["#dde0e3"] * 5)

    if rpm_rows:
        col_h = ["", "Min", "Max", "Mittel", "Abw."]
        tbl = ax_rpm.table(cellText=rpm_rows, colLabels=col_h,
                           cellColours=rpm_colors,
                           colWidths=[0.25, 0.15, 0.15, 0.20, 0.15],
                           loc="upper center", cellLoc="center")
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(9)
        tbl.scale(1, 1.85)
        for c in range(5):
            tbl[0, c].set_facecolor("#2c3e50")
            tbl[0, c].set_text_props(color="white", weight="bold")
    else:
        ax_rpm.text(0.5, 0.5, "No RPM data", ha="center", va="center")

    # ── 2  CURRENT TABLES  (Drive left, Load right) ──────────────────────
    for col_idx, side, keys in [
        (0, "Prüf", [("i_in", "i_in_drive_A"), ("i_motor", "i_motor_drive_A")]),
        (1, "Last",  [("i_in", "i_in_load_A"),  ("i_motor", "i_motor_load_A")]),
    ]:
        ax = fig.add_subplot(gs[2, col_idx])
        ax.axis("off")
        ax.set_title(f"{side} Motor – Ströme (A)",
                     fontsize=10, weight="bold", loc="left", pad=6)

        rows, colors = [], []
        if vesc_data:
            for label, key in keys:
                s = stat_block(vesc_data.get(key, []))
                if s:
                    rows.append([label, _fmt(s["min"], 2),
                                 _fmt(s["max"], 2), _fmt(s["mean"], 2)])
                else:
                    rows.append([label, "---", "---", "---"])
                colors.append(["#eaf0fb"] * 4)

        if rows:
            t = ax.table(cellText=rows,
                         colLabels=["", "Min", "Max", "Mittel"],
                         cellColours=colors,
                         colWidths=[0.28, 0.22, 0.22, 0.22],
                         loc="upper center", cellLoc="center")
            t.auto_set_font_size(False)
            t.set_fontsize(9)
            t.scale(1, 1.85)
            for c in range(4):
                t[0, c].set_facecolor("#2c3e50")
                t[0, c].set_text_props(color="white", weight="bold")

    # ── 3  iq / id TABLES  (Drive left, Load right) ──────────────────────
    for col_idx, side, keys in [
        (0, "Prüf", [("iq", "iq_drive_A"), ("id", "id_drive_A")]),
        (1, "Last",  [("iq", "iq_load_A"),  ("id", "id_load_A")]),
    ]:
        ax = fig.add_subplot(gs[3, col_idx])
        ax.axis("off")
        ax.set_title(f"{side} Motor – FOC Ströme (A)",
                     fontsize=10, weight="bold", loc="left", pad=6)

        rows, colors = [], []
        if vesc_data:
            for label, key in keys:
                s = stat_block(vesc_data.get(key, []))
                if s:
                    rows.append([label, _fmt(s["min"], 3),
                                 _fmt(s["max"], 3), _fmt(s["mean"], 3)])
                else:
                    rows.append([label, "---", "---", "---"])
                colors.append(["#eaf0fb"] * 4)

        if rows:
            t = ax.table(cellText=rows,
                         colLabels=["", "Min", "Max", "Mittel"],
                         cellColours=colors,
                         colWidths=[0.22, 0.24, 0.24, 0.24],
                         loc="upper center", cellLoc="center")
            t.auto_set_font_size(False)
            t.set_fontsize(9)
            t.scale(1, 1.85)
            for c in range(4):
                t[0, c].set_facecolor("#2c3e50")
                t[0, c].set_text_props(color="white", weight="bold")

    # ── 4  TORQUE GRAPH  (V1-style) ──────────────────────────────────────
    ax_tq = fig.add_subplot(gs[4, :])
    if torque_data and torque_data.get("torque"):
        t_time   = torque_data["time"]
        t_torque = [abs(v) for v in torque_data["torque"]]

        time_range = None
        if vesc_data and "timestamp_drive_s" in vesc_data:
            ts = vesc_data["timestamp_drive_s"]
            v  = [x for x in ts if not isnan(x)]
            if v:
                time_range = (v[0], v[-1])

        ax_tq.plot(t_time, t_torque, color=COLORS[3], linewidth=1.5,
                   marker="o", markersize=4, alpha=0.85, label="Torque")

        ts = stat_block(t_torque)
        if ts:
            ax_tq.axhline(ts["mean"], color="grey", lw=1.1, ls="--",
                          label=f"avg {ts['mean']:.4f} Nm")
            ax_tq.text(
                0.01, 0.05,
                f"avg = {ts['mean']:.4f} Nm  |  "
                f"min = {ts['min']:.4f}  |  max = {ts['max']:.4f}  |  "
                f"std = {ts['std']:.5f}",
                transform=ax_tq.transAxes, fontsize=8,
                bbox=dict(boxstyle="round", facecolor="white", alpha=0.88))

        if time_range:
            ax_tq.set_xlim(time_range)

        ax_tq.legend(fontsize=8, loc="upper right")

    else:
        ax_tq.text(0.5, 0.5, "keine Drehmoment-Daten verfügbar", ha="center", va="center")

    ax_tq.set_title("Drehmoment", fontsize=10, weight="bold", loc="left")
    ax_tq.set_ylabel("Drehmoment (Nm)", fontsize=9)
    ax_tq.set_xlabel("Zeit (s)", fontsize=9)
    ax_tq.grid(True, alpha=0.3)


# ═══════════════════════════════════════════════════════════════════════════
#  Page 2  –  BEMF waveform + zoomed + table
# ═══════════════════════════════════════════════════════════════════════════

def build_page2(fig, oszi_data, analysis_stats, bemf_rpm=0):
    """Compose the second page: BEMF analysis."""

    # ← CHANGED: graphs reduced ~9 % (1.4 → 1.27) to free vertical space
    #            for the expanded diagnostic table rows.
    gs = gridspec.GridSpec(
        3, 1, figure=fig,
        height_ratios=[1.27, 1.27, 1.2],
        hspace=0.52,
        left=0.08, right=0.97, top=0.94, bottom=0.04,
    )

    rpm_note = f"   (BEMF RPM: {bemf_rpm})" if bemf_rpm else ""
    fig.suptitle(f"BEMF Phasen analyse{rpm_note}", fontsize=13, weight="bold", y=0.975)

    ax_full  = fig.add_subplot(gs[0])
    ax_zoom  = fig.add_subplot(gs[1])
    ax_table = fig.add_subplot(gs[2])

    ch_names  = ["Ch1", "Ch2", "Ch3"]
    ch_labels = {"Ch1": "Phase 1", "Ch2": "Phase 2", "Ch3": "Phase 3"}
    ch_colors = {"Ch1": COLORS[0], "Ch2": COLORS[1], "Ch3": COLORS[2]}

    if not oszi_data:
        for ax in (ax_full, ax_zoom, ax_table):
            ax.text(0.5, 0.5, "Keine BEMF-Daten verfügbar",
                    ha="center", va="center", fontsize=11)
        return

    n_samples = max(len(oszi_data.get(ch, [])) for ch in ch_names)
    times = np.arange(n_samples) / SAMPLE_RATE   # seconds

    # Detect peaks/valleys per channel
    per_ch: dict = {}
    for ch in ch_names:
        vals = oszi_data.get(ch)
        if not vals:
            continue
        arr = np.array(vals, dtype=float)
        pk, vl = detect_peaks_valleys(arr)
        per_ch[ch] = {
            "arr": arr,
            "peaks_raw": pk,
            "valleys_raw": vl,
        }

    # Compute t_ref from Ch1
    t_ref = 0.0
    if "Ch1" in per_ch and len(per_ch["Ch1"]["peaks_raw"]):
        t_ref = times[per_ch["Ch1"]["peaks_raw"][0]]

    # Apply time + outlier filters; compute stats
    table_rows   = []
    table_colors = []
    for ch in ch_names:
        info = per_ch.get(ch)
        if info is None:
            table_rows.append([ch_labels[ch]] + ["---"] * 7)
            table_colors.append(["#ffffff"] * 8)
            continue

        arr = info["arr"]
        pk  = info["peaks_raw"]
        vl  = info["valleys_raw"]

        if len(pk):
            pk = pk[times[pk] >= (t_ref - 1e-7)]
        if len(vl):
            vl = vl[times[vl] >= (t_ref - 1e-7)]

        pk = filter_outliers(pk, arr)
        vl = filter_outliers(vl, arr)

        info["peaks"]   = pk
        info["valleys"] = vl

        p_vals = arr[pk]
        if len(p_vals) > 2:
            p_vals = np.sort(p_vals)[1:-1]
        v_vals = arr[vl]
        if len(v_vals) > 2:
            v_vals = np.sort(v_vals)[1:-1]

        p_avg = float(np.mean(p_vals)) if len(p_vals) else None
        v_avg = float(np.mean(v_vals)) if len(v_vals) else None
        p_min = float(np.min(p_vals))  if len(p_vals) else None
        p_max = float(np.max(p_vals))  if len(p_vals) else None
        v_min = float(np.min(v_vals))  if len(v_vals) else None
        v_max = float(np.max(v_vals))  if len(v_vals) else None
        amp   = (p_avg - v_avg) if (p_avg is not None and v_avg is not None) else None

        table_rows.append([
            ch_labels[ch],
            _fmt(p_avg, 3), _fmt(p_min, 3), _fmt(p_max, 3),
            _fmt(v_avg, 3), _fmt(v_min, 3), _fmt(v_max, 3),
            _fmt(amp, 3),
        ])
        table_colors.append(["#eaf0fb"] * 8)

    # ── FULL WAVEFORM ────────────────────────────────────────────────────
    for ch in ch_names:
        info = per_ch.get(ch)
        if info is None:
            continue
        col = ch_colors[ch]
        arr = info["arr"]
        ax_full.plot(times[:len(arr)] * 1000, arr,
                     color=col, lw=0.8, alpha=0.6,
                     label=ch_labels[ch])
        pk = info.get("peaks", np.array([]))
        vl = info.get("valleys", np.array([]))
        if len(pk):
            ax_full.scatter(times[pk] * 1000, arr[pk], color=col,
                            marker="^", s=40, zorder=5,
                            edgecolors="black", linewidths=0.4)
        if len(vl):
            ax_full.scatter(times[vl] * 1000, arr[vl], color=col,
                            marker="v", s=40, zorder=5,
                            edgecolors="black", linewidths=0.4)

    ax_full.set_title("BEMF Phasenbild  (ganze Messperiode)", fontsize=10, weight="bold")
    ax_full.set_xlabel("Zeit (ms)", fontsize=9)
    ax_full.set_ylabel("Spannung (V)", fontsize=9)
    ax_full.legend(fontsize=8, loc="upper right")
    ax_full.grid(True, alpha=0.3)

    # ── ZOOMED  ───────────────────────────────────────────────────────────
    zoom_start_ms      = 0.0
    zoom_end_ms        = times[-1] * 1000 if len(times) else 1.0
    zoom_period_est_ms = None
    ch1_info = per_ch.get("Ch1")
    if ch1_info is not None:
        period_samples     = _estimate_period_samples(ch1_info["arr"])
        period             = period_samples / SAMPLE_RATE
        zoom_period_est_ms = period * 1000

        pk1 = ch1_info.get("peaks", np.array([]))
        if len(pk1) > 0:
            start_idx     = pk1[1] if len(pk1) > 1 else pk1[0]
            zoom_start_ms = max(0.0, times[start_idx] * 1000 - period * 0.15 * 1000)
            zoom_end_ms   = zoom_start_ms + period * ZOOM_PERIODS * 1000
            zoom_end_ms   = min(times[-1] * 1000, zoom_end_ms)

    for ch in ch_names:
        info = per_ch.get(ch)
        if info is None:
            continue
        col  = ch_colors[ch]
        arr  = info["arr"]
        t_ms = times[:len(arr)] * 1000
        mask = (t_ms >= zoom_start_ms) & (t_ms <= zoom_end_ms)
        ax_zoom.plot(t_ms[mask], arr[mask],
                     color=col, lw=1.2, alpha=0.8,
                     label=ch_labels[ch])
        pk = info.get("peaks", np.array([]))
        vl = info.get("valleys", np.array([]))
        if len(pk):
            pk_mask = (times[pk] * 1000 >= zoom_start_ms) & (times[pk] * 1000 <= zoom_end_ms)
            pk_vis  = pk[pk_mask]
            if len(pk_vis):
                ax_zoom.scatter(times[pk_vis] * 1000, arr[pk_vis], color=col,
                                marker="^", s=60, zorder=5,
                                edgecolors="black", linewidths=0.5)
        if len(vl):
            vl_mask = (times[vl] * 1000 >= zoom_start_ms) & (times[vl] * 1000 <= zoom_end_ms)
            vl_vis  = vl[vl_mask]
            if len(vl_vis):
                ax_zoom.scatter(times[vl_vis] * 1000, arr[vl_vis], color=col,
                                marker="v", s=60, zorder=5,
                                edgecolors="black", linewidths=0.5)

    period_str = (f"  –  T≈{zoom_period_est_ms:.2f} ms/el." if zoom_period_est_ms else "")
    ax_zoom.set_title(
        f"BEMF Phasenbild  (Ausschnitt – {ZOOM_PERIODS} elektrische Perioden{period_str})",
        fontsize=10, weight="bold")
    ax_zoom.set_xlabel("Zeit (ms)", fontsize=9)
    ax_zoom.set_ylabel("Spannung (V)", fontsize=9)
    ax_zoom.legend(fontsize=8, loc="upper right")
    ax_zoom.grid(True, alpha=0.3)

    # ── BEMF TABLE ───────────────────────────────────────────────────────
    ax_table.axis("off")
    ax_table.set_title(
        "Hoch / Tief Statistik  "
        "(niedrigster Peak & höchstes Tal ausgeschlossen)",
        fontsize=9, weight="bold", loc="left", pad=4)

    col_h = ["", "Hoch Avg", "Hoch Min", "Hoch Max",
             "Tief Avg", "Tief Max", "Tief Min", "Amplitude"]
    col_w = [0.13, 0.12, 0.11, 0.11, 0.12, 0.11, 0.11, 0.12]

    # ── Magnet-Asymmetrie row (unchanged logic, tightened label) ─────────
    sym_ch1 = analyze_magnet_symmetry(per_ch["Ch1"]["arr"]) if "Ch1" in per_ch else 0.0
    sym_ch2 = analyze_magnet_symmetry(per_ch["Ch2"]["arr"]) if "Ch2" in per_ch else 0.0
    sym_ch3 = analyze_magnet_symmetry(per_ch["Ch3"]["arr"]) if "Ch3" in per_ch else 0.0

    # ← CHANGED: shortened cell texts so they fit without overflow
    table_rows.append([
        "Magnet-Asymm.",
        f"Ch1: {sym_ch1:.1f}%", "",
        f"Ch2: {sym_ch2:.1f}%", "",
        f"Ch3: {sym_ch3:.1f}%", "",
        "(Subharm.)",
    ])
    table_colors.append(["#dde0e3"] * 8)

    # ← CHANGED: Phase-Balance row – CV value in its own cell, limit in last cell
    pb      = phase_balance(per_ch)
    pb_warn = pb > PHASE_BALANCE_WARN_PCT
    table_rows.append([
        "Phasen-Balance",
        f"CV: {pb:.2f}%{'⚠' if pb_warn else ''}", "", "", "",
        "", "",
        f"Warn>{PHASE_BALANCE_WARN_PCT:.0f}%",
    ])
    table_colors.append(["#f5b7b1"] * 8 if pb_warn else ["#dde0e3"] * 8)

    # ← CHANGED: Phase-Timing row – each delta in its own cell,
    #            max deviation and verdict in the last two cells.
    #            Uses the new structured return value of evaluate_phase_timing.
    timing_ok, deltas_ms, max_dev_pct = evaluate_phase_timing(analysis_stats)
    no_data = all(d == 0.0 for d in deltas_ms)
    if no_data:
        table_rows.append([
            "Phasen-Timing",
            "n/v", "", "", "", "", "",
            "---",
        ])
    else:
        timing_verdict = "OK" if timing_ok else "WARN ⚠"
        table_rows.append([
            "Phasen-Timing",
            f"Δ12: {deltas_ms[0]:.3f}ms",
            "",
            f"Δ23: {deltas_ms[1]:.3f}ms",
            "",
            f"Δ31: {deltas_ms[2]:.3f}ms",
            f"Abw: {max_dev_pct:.1f}%",
            timing_verdict,
        ])
    table_colors.append(["#dde0e3"] * 8 if timing_ok else ["#f5b7b1"] * 8)

    if table_rows:
        t = ax_table.table(
            cellText=table_rows,
            colLabels=col_h,
            cellColours=table_colors,
            colWidths=col_w,
            loc="upper center",
            cellLoc="center",
        )
        t.auto_set_font_size(False)
        # ← CHANGED: font 8→7.5 and scale 2.0→1.65 to keep all rows on-page
        t.set_fontsize(7.5)
        t.scale(1, 1.65)
        for c in range(len(col_h)):
            t[0, c].set_facecolor("#2c3e50")
            t[0, c].set_text_props(color="white", weight="bold")


# ═══════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    now_str      = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    now_filename = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    parser = argparse.ArgumentParser(
        description="VESC Testbench Measurement Report V3")
    parser.add_argument("--motor_name", type=str,
                        default="motor_unbekannt",
                        help="Motor name (matches Motorauswahl.json entry)")
    parser.add_argument("--mat_nr", type=str, default="",
                        help="Materialnummer (aus Motorauswahl.json)")
    parser.add_argument("--bemf_rpm", type=int, default=0,
                        help="BEMF Messung RPM (benutzerseitig, nicht eRPM)")
    parser.add_argument("--user_comment", type=str, default="",
                        help="Optionale Anmerkung des Benutzers für den Bericht")
    args       = parser.parse_args()
    motor_name = args.motor_name
    cli_mat_nr = args.mat_nr
    bemf_rpm   = args.bemf_rpm
    user_comment = args.user_comment

    print(f"[messprotokoll_V3] Generating report for: {motor_name}")

    # ── Load data ────────────────────────────────────────────────────────
    vesc_data    = load_vesc(VESC_CSV_PATH)
    oszi_data    = load_oszi(OSZI_CSV_PATH)
    torque_data  = load_torque(TORQUE_CSV_PATH)
    motorauswahl = load_json(MOTORAUSWAHL_PATH)

    # Motor metadata
    motor_entry = None
    mat_nr = cli_mat_nr if cli_mat_nr else "---"
    if motorauswahl and isinstance(motorauswahl, list):
        motor_entry = next(
            (m for m in motorauswahl if m.get("Name") == motor_name), None)
        if motor_entry and not cli_mat_nr:
            mat_nr = motor_entry.get("Mat.Nr.", "---")

    # ── Build analysis_result.json  (required by GUI / messablauf) ───────
    analysis_stats = build_analysis_result(vesc_data, oszi_data)

    # Sanitize comment for filename
    safe_comment = ""
    if user_comment:
        safe_comment = "".join(c if c.isalnum() or c in " _-" else "_" for c in user_comment)
        safe_comment = "_" + safe_comment.strip()[:30] # Limit length

    # ── Construct output PDF filename ────────────────────────────────────
    output_pdf_path = REPORTS_DIR / f"Messprotokoll_{motor_name}_{mat_nr}_{now_filename}{safe_comment}.pdf"

    # ── Verdict ──────────────────────────────────────────────────────────
    verdict, verdict_details = determine_verdict(analysis_stats, motor_entry)
    print(f"  Verdict: {verdict}")

    # ── Generate PDF ─────────────────────────────────────────────────────
    with PdfPages(output_pdf_path) as pdf:
        # Page 1
        fig1 = plt.figure(figsize=(8.27, 11.69))
        build_page1(fig1, motor_name, mat_nr, now_str,
                    verdict, verdict_details,
                    vesc_data, torque_data, user_comment)
        pdf.savefig(fig1)
        plt.close(fig1)
        print("  Page 1 (Overview) done.")

        # Page 2
        fig2 = plt.figure(figsize=(8.27, 11.69))
        build_page2(fig2, oszi_data, analysis_stats, bemf_rpm=bemf_rpm)
        pdf.savefig(fig2)
        plt.close(fig2)
        print("  Page 2 (BEMF) done.")

    print(f"  Report saved -> {output_pdf_path.resolve()}")


if __name__ == "__main__":
    main()