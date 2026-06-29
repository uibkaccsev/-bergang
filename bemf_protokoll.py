#!/usr/bin/env python3
"""
bemf_protokoll.py  -  BEMF Update Report

Generates a 1-page A4 PDF  "BEMF-Protokoll.pdf"  after a BEMF-aktualisierung
run.  Layout mirrors page 2 of messprotokoll_V3:
  - Header (motor name, Mat.Nr., date, BEMF RPM)
  - Full BEMF waveform  (all 3 phases)
  - Zoomed BEMF waveform  (ZOOM_PERIODS electrical periods)
  - Diagnostic table: peaks/valleys stats, Magnet-Asymm., THD,
    Phasen-Balance, Phasen-Timing

Called from gui_launcher.py after BEMF-aktualisierung.py completes.
"""

import argparse
import csv
import json
import sys
from datetime import datetime
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
# Paths
# ---------------------------------------------------------------------------
if getattr(sys, 'frozen', False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_DIR       = SCRIPT_DIR / "results"
CONFIG_DIR        = SCRIPT_DIR / "config"
REPORTS_DIR       = SCRIPT_DIR.parent / "Messberichte"   # one level up: Motorprüfstand/Messberichte
RESULTS_DIR.mkdir(exist_ok=True)
CONFIG_DIR.mkdir(exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)
OSZI_CSV_PATH     = RESULTS_DIR / "OscilloscopeStream.csv"
BEMF_RESULT_PATH  = RESULTS_DIR / "bemf_result.json"
MOTORAUSWAHL_PATH = CONFIG_DIR  / "Motorauswahl.json"

SAMPLE_RATE = 100_000   # Hz

ZOOM_PERIODS = 3               # electrical periods shown in zoomed view

# Diagnostic thresholds (identical to messprotokoll_V3)
PHASE_TIMING_SYMMETRY_TOL_PCT = 5.0
THD_WARN_PCT                  = 10.0
PHASE_BALANCE_WARN_PCT        = 5.0

COLORS = ["#2980b9", "#e67e22", "#27ae60", "#8e44ad", "#c0392b"]


# ==========================================================================
#  Data loading
# ==========================================================================

def _pf(s):
    try:
        return float(s)
    except (ValueError, TypeError):
        return float("nan")


def load_oszi(path):
    if not path.exists():
        print(f"  [WARN] Oscilloscope data not found: {path}")
        return None
    channels = {}
    ch_names = []
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


def load_json(path):
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _fmt(v, decimals=2):
    if v is None:
        return "---"
    return f"{v:.{decimals}f}"


# ==========================================================================
#  BEMF analysis helpers  (identical algorithms to messprotokoll_V3)
# ==========================================================================

def _estimate_period_samples(arr):
    N = len(arr)
    if N < 8:
        return float(N)
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


def analyze_magnet_symmetry(arr):
    N = len(arr)
    if N < 8:
        return 0.0
    arr_centered = arr - arr.mean()
    window   = np.hanning(N)
    spectrum = np.abs(np.fft.rfft(arr_centered * window))
    min_freq_idx = 1
    max_freq_idx = max(2, N // 10)
    search = spectrum[min_freq_idx:max_freq_idx]
    if len(search) == 0:
        return 0.0
    dom_idx       = np.argmax(search) + min_freq_idx
    sub_start_idx = max(2, int(dom_idx * 0.05))
    sub_end_idx   = max(sub_start_idx + 1, int(dom_idx * 0.6))
    if sub_end_idx > sub_start_idx:
        sub_search = spectrum[sub_start_idx:sub_end_idx]
        if len(sub_search) > 0:
            return float(np.max(sub_search) / spectrum[dom_idx]) * 100.0
    return 0.0


def compute_thd(arr):
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


def phase_balance(per_ch):
    amps = [float(np.ptp(info["arr"]))
            for info in per_ch.values()
            if "arr" in info and len(info["arr"]) > 0]
    if len(amps) < 2:
        return 0.0
    mean_amp = float(np.mean(amps))
    if mean_amp < 1e-9:
        return 0.0
    return float(np.std(amps) / mean_amp * 100.0)


def evaluate_phase_timing(analysis_stats):
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


def detect_peaks_valleys(values, _unused_smooth=None, _unused_check=None, _unused_dist=None):
    arr = np.array(values, dtype=float)
    if len(arr) < 8:
        return np.array([], dtype=int), np.array([], dtype=int)
    period_samples = _estimate_period_samples(arr)
    min_distance   = max(4, int(period_samples * 0.05))
    amplitude      = np.nanmax(arr) - np.nanmin(arr)
    min_prominence = max(0.2, amplitude * 0.03)
    peaks,   _ = find_peaks( arr, distance=min_distance, prominence=min_prominence)
    valleys, _ = find_peaks(-arr, distance=min_distance, prominence=min_prominence)
    return peaks, valleys


def filter_outliers(indices, values, tol=0.08):
    if len(indices) < 3:
        return indices
    v = values[indices]
    med = np.median(v)
    if abs(med) < 1e-9:
        return indices
    keep = np.abs(v - med) <= tol * np.abs(med)
    return indices[keep]


# ==========================================================================
#  Build analysis_stats dict  (peak/valley timing deltas)
# ==========================================================================

def build_analysis_stats(oszi_data):
    stats = {}
    full_peak_times = {}
    full_valley_times = {}

    if oszi_data:
        n_samples = max(len(v) for v in oszi_data.values())
        times = np.arange(n_samples) / SAMPLE_RATE
        t_ref = 0.0
        processed = {}
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
    return stats


# ==========================================================================
#  Build the single-page report  (mirrors build_page2 of messprotokoll_V3)
# ==========================================================================

def build_page(fig, motor_name, mat_nr, date_str, bemf_rpm, oszi_data, user_comment=""):

    gs = gridspec.GridSpec(
        4, 1, figure=fig,
        height_ratios=[0.25, 1.27, 1.27, 1.2],
        hspace=0.52,
        left=0.08, right=0.97, top=0.97, bottom=0.04,
    )

    # -- HEADER -------------------------------------------------------------
    ax_hdr = fig.add_subplot(gs[0])
    ax_hdr.axis("off")
    ax_hdr.add_patch(mpatches.FancyBboxPatch(
        (0, 0), 1, 1, boxstyle="round,pad=0.02",
        transform=ax_hdr.transAxes, facecolor="#2c3e50",
        edgecolor="none", clip_on=False))
    ax_hdr.text(0.5, 0.78,
                "BEMF Update Report",
                transform=ax_hdr.transAxes, ha="center", va="center",
                fontsize=13, weight="bold", color="white")
    rpm_note = f"   |   BEMF RPM: {bemf_rpm}" if bemf_rpm else ""
    sub = f"Motor: {motor_name}   |   Mat.Nr.: {mat_nr}   |   {date_str}{rpm_note}"
    ax_hdr.text(0.5, 0.45, sub,
                transform=ax_hdr.transAxes, ha="center", va="center",
                fontsize=9, color="#ecf0f1")
                
    comment_text = f"Kommentar: {user_comment}" if user_comment else ""
    ax_hdr.text(0.5, 0.15, comment_text,
                transform=ax_hdr.transAxes, ha="center", va="center",
                fontsize=9, color="#f39c12", weight="bold")

    ax_full  = fig.add_subplot(gs[1])
    ax_zoom  = fig.add_subplot(gs[2])
    ax_table = fig.add_subplot(gs[3])

    ch_names  = ["Ch1", "Ch2", "Ch3"]
    ch_labels = {"Ch1": "Phase 1", "Ch2": "Phase 2", "Ch3": "Phase 3"}
    ch_colors = {"Ch1": COLORS[0], "Ch2": COLORS[1], "Ch3": COLORS[2]}

    if not oszi_data:
        for ax in (ax_full, ax_zoom, ax_table):
            ax.text(0.5, 0.5, "Keine BEMF-Daten verfuegbar",
                    ha="center", va="center", fontsize=11)
        return

    # -- Peak / valley detection --------------------------------------------
    n_samples = max(len(oszi_data.get(ch, [])) for ch in ch_names)
    times = np.arange(n_samples) / SAMPLE_RATE   # seconds

    per_ch = {}
    for ch in ch_names:
        vals = oszi_data.get(ch)
        if not vals:
            continue
        arr = np.array(vals, dtype=float)
        pk, vl = detect_peaks_valleys(arr)
        per_ch[ch] = {"arr": arr, "peaks_raw": pk, "valleys_raw": vl}

    t_ref = 0.0
    if "Ch1" in per_ch and len(per_ch["Ch1"]["peaks_raw"]):
        t_ref = times[per_ch["Ch1"]["peaks_raw"][0]]

    # -- Stats table rows ---------------------------------------------------
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

    # -- FULL WAVEFORM ------------------------------------------------------
    for ch in ch_names:
        info = per_ch.get(ch)
        if info is None:
            continue
        col = ch_colors[ch]
        arr = info["arr"]
        ax_full.plot(times[:len(arr)] * 1000, arr,
                     color=col, lw=0.8, alpha=0.6, label=ch_labels[ch])
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

    # -- ZOOMED -------------------------------------------------------------
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
                     color=col, lw=1.2, alpha=0.8, label=ch_labels[ch])
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

    period_str = (f"  -  T={zoom_period_est_ms:.2f} ms/el." if zoom_period_est_ms else "")
    ax_zoom.set_title(
        f"BEMF Phasenbild  (Ausschnitt - {ZOOM_PERIODS} elektrische Perioden{period_str})",
        fontsize=10, weight="bold")
    ax_zoom.set_xlabel("Zeit (ms)", fontsize=9)
    ax_zoom.set_ylabel("Spannung (V)", fontsize=9)
    ax_zoom.legend(fontsize=8, loc="upper right")
    ax_zoom.grid(True, alpha=0.3)

    # -- BEMF TABLE ---------------------------------------------------------
    ax_table.axis("off")
    ax_table.set_title(
        "Hoch / Tief Statistik  "
        "(niedrigster Peak & hoechstes Tal ausgeschlossen)",
        fontsize=9, weight="bold", loc="left", pad=4)

    col_h = ["", "Hoch Avg", "Hoch Min", "Hoch Max",
             "Tief Avg", "Tief Max", "Tief Min", "Amplitude"]
    col_w = [0.13, 0.12, 0.11, 0.11, 0.12, 0.11, 0.11, 0.12]

    # Magnet-Asymmetrie row
    sym_ch1 = analyze_magnet_symmetry(per_ch["Ch1"]["arr"]) if "Ch1" in per_ch else 0.0
    sym_ch2 = analyze_magnet_symmetry(per_ch["Ch2"]["arr"]) if "Ch2" in per_ch else 0.0
    sym_ch3 = analyze_magnet_symmetry(per_ch["Ch3"]["arr"]) if "Ch3" in per_ch else 0.0
    table_rows.append([
        "Magnet-Asymm.",
        f"Ch1: {sym_ch1:.1f}%", "",
        f"Ch2: {sym_ch2:.1f}%", "",
        f"Ch3: {sym_ch3:.1f}%", "",
        "(Subharm.)",
    ])
    table_colors.append(["#dde0e3"] * 8)

    # Phase-Balance row
    pb      = phase_balance(per_ch)
    pb_warn = pb > PHASE_BALANCE_WARN_PCT
    warn_sym = chr(9888) if pb_warn else ""
    table_rows.append([
        "Phasen-Balance",
        f"CV: {pb:.2f}%{warn_sym}", "", "", "",
        "", "",
        f"Warn>{PHASE_BALANCE_WARN_PCT:.0f}%",
    ])
    table_colors.append(["#f5b7b1"] * 8 if pb_warn else ["#dde0e3"] * 8)

    # Phase-Timing row
    analysis_stats = build_analysis_stats(oszi_data)
    timing_ok, deltas_ms, max_dev_pct = evaluate_phase_timing(analysis_stats)
    no_data = all(d == 0.0 for d in deltas_ms)
    if no_data:
        table_rows.append([
            "Phasen-Timing",
            "n/v", "", "", "", "", "",
            "---",
        ])
    else:
        timing_verdict = "OK" if timing_ok else f"WARN {chr(9888)}"
        table_rows.append([
            "Phasen-Timing",
            f"D12: {deltas_ms[0]:.3f}ms",
            "",
            f"D23: {deltas_ms[1]:.3f}ms",
            "",
            f"D31: {deltas_ms[2]:.3f}ms",
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
        t.set_fontsize(7.5)
        t.scale(1, 1.65)
        for c in range(len(col_h)):
            t[0, c].set_facecolor("#2c3e50")
            t[0, c].set_text_props(color="white", weight="bold")


# ==========================================================================
#  Main
# ==========================================================================

def main():
    now_str = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    ts_file = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    parser = argparse.ArgumentParser(description="BEMF Bericht")
    parser.add_argument("--motor_name", type=str, default="motor_unbekannt")
    parser.add_argument("--mat_nr", type=str, default="---")
    parser.add_argument("--bemf_rpm", type=int, default=0,
                        help="BEMF Messung RPM (benutzerseitig)")
    parser.add_argument("--user_comment", type=str, default="",
                        help="Optionale Anmerkung des Benutzers für den Bericht")
    args = parser.parse_args()

    safe_name = args.motor_name.replace(" ", "_").replace("/", "-").replace("\\", "-")
    safe_mat  = args.mat_nr.replace(" ", "_").replace("/", "-").replace("\\", "-")
    
    # Sanitize comment for filename
    safe_comment = ""
    if args.user_comment:
        safe_comment = "".join(c if c.isalnum() or c in " _-" else "_" for c in args.user_comment)
        safe_comment = "_" + safe_comment.strip()[:30]

    output_pdf = REPORTS_DIR / f"BEMF_Protokoll_{safe_name}_{safe_mat}_{ts_file}{safe_comment}.pdf"

    print(f"[bemf_protokoll] Generating BEMF report for: {args.motor_name}")

    oszi_data = load_oszi(OSZI_CSV_PATH)

    fig = plt.figure(figsize=(8.27, 11.69))
    build_page(fig, args.motor_name, args.mat_nr, now_str, args.bemf_rpm, oszi_data, args.user_comment)

    with PdfPages(output_pdf) as pdf:
        pdf.savefig(fig)
    plt.close(fig)

    print(f"  BEMF-Protokoll saved -> {output_pdf.resolve()}")


if __name__ == "__main__":
    main()
