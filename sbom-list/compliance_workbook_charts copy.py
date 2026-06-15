#!/usr/bin/env python3
"""
Leadership 2×2 charts from CS-ENGG-Security-Compliance workbook export (columns A–K per project tabs).

Produces a single merged PNG of four cumulative portfolio panels and optionally uploads it to Slack.

All paths, dates, titles, chart layout, and Slack settings are configured via constants below (no CLI).

Depends: pandas, openpyxl, matplotlib, numpy; slack_sdk for Slack chart upload + Block Kit summary (see requirements-compliance-charts.txt).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

# Matches JiraComplianceDaily.gs CDL_DISPLAY_HEADERS / report.csv
DISPLAY_COLUMNS: Sequence[str] = (
    "Project",
    "Date",
    "Fixed-Total",
    "Fixed-Breached",
    "Fixed-Within_SLA",
    "Daily-Compliance",
    "Open-Breached",
    "Cumulative-Fixed-Total",
    "Cumulative-Fixed-Breached",
    "Cumulative-Fixed-Within_SLA",
    "Cumulative-Compliance",
)

DEFAULT_EXCLUDE = frozenset({"Summary"})

# =============================================================================
# Configuration — edit here only (no command-line arguments).
# =============================================================================

WORKBOOK_PATH = Path("CS-ENGG-Security-Compliance.xlsx")
OUTPUT_PNG_PATH = Path("compliance_leadership_charts.png")

# First date to include (YYYY-MM-DD), aligned with workbook reporting.
DATE_START = "2026-02-03"

# Extra sheet names to exclude beyond Summary (always skipped).
EXTRA_EXCLUDE_SHEETS: Tuple[str, ...] = ()

# If non-empty, only load these sheet / project names; portfolio aggregates that subset only.
ONLY_PROJECT_SHEETS: Tuple[str, ...] = ()

CHART_TITLE = "CS Engineering - Security SLA compliance"

# Merged PNG layout (matplotlib).
FIGURE_SIZE_INCHES: Tuple[float, float] = (15.0, 10.0)
FIGURE_FACE_COLOR = "#f8f9fa"
SAVE_DPI = 200
LAST_POINT_LABEL_FONTSIZE = 10

# Slack file upload (Bot User OAuth token, starts with xoxb-). Leave blank to skip upload.
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL_ID = "C07G6R3FUDU"
# SLACK_UPLOAD_COMMENT = (
#     "Cumulative portfolio leadership charts (four panels merged)."
# )
# Block Kit header (shown above the metrics section).
SLACK_MESSAGE_HEADER = "CS Engineering - Security SLA compliance"
# Notification preview / accessibility fallback when blocks cannot render.
SLACK_MESSAGE_FALLBACK_PREFIX = "CS Engineering - Security SLA compliance"

# Optional Slack mrkdwn links (<url|label>) for each metric; leave "" for plain values.
SLACK_LINK_SLA_COMPLIANCE = ""
SLACK_LINK_CUMULATIVE_FIXES = ""
SLACK_LINK_OPEN_PAST_SLA = ""
SLACK_LINK_CUM_WITHIN_SLA = ""
SLACK_LINK_CUM_BREACHED = ""

# Destination for project-by-project detail (shared Excel, Jira board/filter, dashboard, etc.).
SLACK_LINK_PER_PROJECT_DETAILS = "https://docs.google.com/spreadsheets/d/16pqLCcnWoLPvAUq6sNJCsXyc75mWHsBRcKcHH-d_oOc/edit?gid=1126363028#gid=1126363028"


class ChartSeries(NamedTuple):
    dates: pd.Series
    compliance_pct: pd.Series
    cum_total: pd.Series
    open_breached: pd.Series
    cum_within: pd.Series
    cum_breached: pd.Series


def _parse_pct_cell(val: object) -> Optional[float]:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    s = s.replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def load_project_frames(
    path: Path,
    *,
    start: str,
    exclude_sheets: Iterable[str],
    only_projects: Optional[Sequence[str]],
) -> Tuple[pd.DataFrame, List[str]]:
    """Load all project tabs into one frame with column Project from sheet name fallback."""
    xl = pd.ExcelFile(path, engine="openpyxl")
    exclude = set(exclude_sheets) | DEFAULT_EXCLUDE
    only_set = set(only_projects) if only_projects else None

    frames: List[pd.DataFrame] = []

    for sheet_name in xl.sheet_names:
        if sheet_name in exclude:
            continue
        if only_set is not None and sheet_name not in only_set:
            continue

        df = pd.read_excel(path, sheet_name=sheet_name, header=0, engine="openpyxl")

        if df.shape[1] < 11:
            continue

        df = df.iloc[:, :11].copy()
        df.columns = list(DISPLAY_COLUMNS)

        pk_series = df["Project"].astype(str).str.strip()
        mask_bad = pk_series.isna() | (pk_series == "") | (pk_series == "nan")
        df.loc[mask_bad, "Project"] = sheet_name

        df["_sheet"] = sheet_name
        frames.append(df)

    if not frames:
        raise SystemExit("No project sheets loaded after filters.")

    out = pd.concat(frames, ignore_index=True)
    out["Date"] = pd.to_datetime(out["Date"], errors="coerce")

    for col in (
        "Fixed-Total",
        "Fixed-Breached",
        "Fixed-Within_SLA",
        "Open-Breached",
        "Cumulative-Fixed-Total",
        "Cumulative-Fixed-Breached",
        "Cumulative-Fixed-Within_SLA",
    ):
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out["Cumulative-Compliance"] = out["Cumulative-Compliance"].map(_parse_pct_cell)

    start_dt = pd.to_datetime(start)
    out = out[out["Date"].notna() & (out["Date"] >= start_dt)].copy()

    proj_keys = sorted(out["Project"].drop_duplicates().tolist(), key=str)
    return out, proj_keys


def dedupe_dates(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["Project", "Date"])
    return df.drop_duplicates(subset=["Project", "Date"], keep="last")


def compliance_pct_from_totals(cum_f: pd.Series, cum_b: pd.Series) -> pd.Series:
    """Align with JiraComplianceDaily.gs cdlCompliancePct_."""
    result = pd.Series(np.nan, index=cum_f.index, dtype="float64")
    mask = cum_f.notna() & (cum_f > 0)
    result.loc[mask] = np.round(10000 * (1 - cum_b.loc[mask] / cum_f.loc[mask])) / 100
    return result


def build_portfolio_series(df: pd.DataFrame) -> pd.DataFrame:
    """Sum daily C,D,E,G by Date; cumulative H,I,J,K from running totals."""
    d = dedupe_dates(df)
    daily = (
        d.groupby("Date", as_index=False)[
            ["Fixed-Total", "Fixed-Breached", "Fixed-Within_SLA", "Open-Breached"]
        ]
        .sum()
        .sort_values("Date")
    )
    cum_f = daily["Fixed-Total"].cumsum()
    cum_b = daily["Fixed-Breached"].cumsum()
    daily = daily.assign(
        **{
            "Cumulative-Fixed-Total": cum_f,
            "Cumulative-Fixed-Breached": cum_b,
            "Cumulative-Fixed-Within_SLA": cum_f - cum_b,
        }
    )
    daily["Cumulative-Compliance"] = compliance_pct_from_totals(cum_f, cum_b)
    return daily


def chart_series_from_portfolio_df(pf: pd.DataFrame) -> ChartSeries:
    return ChartSeries(
        dates=pf["Date"],
        compliance_pct=pf["Cumulative-Compliance"],
        cum_total=pf["Cumulative-Fixed-Total"],
        open_breached=pf["Open-Breached"],
        cum_within=pf["Cumulative-Fixed-Within_SLA"],
        cum_breached=pf["Cumulative-Fixed-Breached"],
    )


def latest_portfolio_metrics(portfolio_df: pd.DataFrame) -> Dict[str, str]:
    """Latest portfolio row (same endpoints as chart annotations)."""
    if portfolio_df.empty:
        raise SystemExit("Portfolio aggregate has no rows.")
    last = portfolio_df.sort_values("Date").iloc[-1]
    date_str = pd.Timestamp(last["Date"]).strftime("%b %d, %Y")

    comp = last["Cumulative-Compliance"]
    compliance_str = f"{float(comp):.1f}%" if pd.notna(comp) else "—"

    def _count(col: str) -> str:
        v = last[col]
        return f"{int(round(float(v))):,}" if pd.notna(v) else "—"

    return {
        "date": date_str,
        "sla_compliance_pct": compliance_str,
        "cumulative_fixes": _count("Cumulative-Fixed-Total"),
        "open_past_sla": _count("Open-Breached"),
        "cum_within_sla": _count("Cumulative-Fixed-Within_SLA"),
        "cum_breached": _count("Cumulative-Fixed-Breached"),
    }


def _slack_link_or_plain(link_url: str, display: str) -> str:
    u = link_url.strip()
    if u:
        return f"<{u}|{display}>"
    return display


def build_slack_blocks(metrics: Dict[str, str]) -> List[dict]:
    """Header + section mrkdwn mirroring the annotated chart values."""
    lines: List[str] = []
    # intro = SLACK_UPLOAD_COMMENT.strip()
    # if intro:
    #     lines.append(intro + "\n\n")

    lines.append(f"*Date:* {metrics['date']}\n")
    lines.append(
        f"*SLA compliance rate:* {_slack_link_or_plain(SLACK_LINK_SLA_COMPLIANCE, metrics['sla_compliance_pct'])}\n"
    )
    lines.append(
        f"*Cumulative fixes completed:* {_slack_link_or_plain(SLACK_LINK_CUMULATIVE_FIXES, metrics['cumulative_fixes'])}\n"
    )
    lines.append(
        f"*Open issues past SLA deadline:* {_slack_link_or_plain(SLACK_LINK_OPEN_PAST_SLA, metrics['open_past_sla'])}\n"
    )
    lines.append(
        f"*Cumulative fixed — within SLA:* {_slack_link_or_plain(SLACK_LINK_CUM_WITHIN_SLA, metrics['cum_within_sla'])}\n"
    )
    lines.append(
        f"*Cumulative fixed — breached SLA:* {_slack_link_or_plain(SLACK_LINK_CUM_BREACHED, metrics['cum_breached'])}\n"
    )

    detail_url = SLACK_LINK_PER_PROJECT_DETAILS.strip()
    if detail_url:
        lines.append(
            "\n"
            "*Per-project detail:* "
            f"<{detail_url}|Click here to view details for each project>.\n"
        )
    else:
        lines.append(
            "\n"
            "_Tip:_ Set `SLACK_LINK_PER_PROJECT_DETAILS` in this script to add a clickable link "
            "(workbook, Jira, or dashboard) for project-level numbers.\n"
        )

    text = "".join(lines)
    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": SLACK_MESSAGE_HEADER,
                "emoji": True,
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]


_PANEL_COLORS = {
    "compliance": "#0077b6",
    "fixed_total": "#e85d04",
    "open_breach": "#c1121f",
    "within_sla": "#2d6a4f",
    "breached": "#e63946",
}


def _last_finite_point(dates: pd.Series, vals: pd.Series) -> Tuple[Optional[pd.Timestamp], Optional[float]]:
    dt = pd.to_datetime(dates)
    y = pd.to_numeric(vals, errors="coerce").astype(float)
    ok = y.notna() & np.isfinite(y.to_numpy())
    if not ok.any():
        return None, None
    pos = np.flatnonzero(ok.to_numpy())[-1]
    return dt.iloc[pos], float(y.iloc[pos])


def _annotate_latest_on_line(
    ax,
    dates: pd.Series,
    vals: pd.Series,
    *,
    label_fmt: Callable[[float], str],
    color: str,
    xytext: Tuple[float, float] = (8.0, 8.0),
) -> None:
    """Label the latest finite point on a line (most recent date in the series)."""
    x, y = _last_finite_point(dates, vals)
    if x is None or y is None:
        return
    ax.annotate(
        label_fmt(y),
        xy=(x, y),
        xytext=xytext,
        textcoords="offset points",
        fontsize=LAST_POINT_LABEL_FONTSIZE,
        fontweight="bold",
        color=color,
        bbox=dict(
            boxstyle="round,pad=0.35",
            facecolor="white",
            edgecolor=color,
            linewidth=1.2,
            alpha=0.95,
        ),
        arrowprops=dict(arrowstyle="-", color=color, lw=0.75, alpha=0.65),
    )


def plot_portfolio_merged_png(portfolio: ChartSeries, output: Path, title: str) -> None:
    """Draw four cumulative portfolio panels in a 2×2 grid and save as one PNG."""
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker

    c = _PANEL_COLORS
    fig, axes = plt.subplots(
        2,
        2,
        figsize=FIGURE_SIZE_INCHES,
        facecolor=FIGURE_FACE_COLOR,
    )
    ax_k, ax_h, ax_g, ax_ij = axes[0, 0], axes[0, 1], axes[1, 0], axes[1, 1]
    d = portfolio.dates

    for ax in axes.flat:
        ax.set_facecolor("#ffffff")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.grid(True, alpha=0.3, color="#e9ecef", linewidth=0.8)
        ax.tick_params(axis="x", rotation=30, labelsize=9)
        ax.tick_params(axis="y", labelsize=9)

    k = portfolio.compliance_pct
    mask_k = k.notna()
    if mask_k.any():
        ax_k.plot(d[mask_k], k[mask_k], color=c["compliance"], linewidth=2.2)
    _annotate_latest_on_line(
        ax_k,
        d,
        portfolio.compliance_pct,
        label_fmt=lambda v: f"{v:.1f}%",
        color=c["compliance"],
        xytext=(8.0, 10.0),
    )
    ax_k.set_title("SLA Compliance Rate (%)", fontsize=12, fontweight="bold", pad=6)
    ax_k.set_ylabel("Compliance (%)", fontsize=10, color="#495057")
    ax_k.text(
        0.01,
        -0.18,
        "% of fixes resolved within SLA",
        transform=ax_k.transAxes,
        fontsize=8,
        color="#6c757d",
    )

    ax_h.plot(d, portfolio.cum_total, color=c["fixed_total"], linewidth=2.2)
    _annotate_latest_on_line(
        ax_h,
        d,
        portfolio.cum_total,
        label_fmt=lambda v: f"{int(round(v)):,.0f}",
        color=c["fixed_total"],
        xytext=(8.0, 8.0),
    )
    ax_h.set_title("Cumulative Fixes Completed", fontsize=12, fontweight="bold", pad=6)
    ax_h.set_ylabel("Issues (count)", fontsize=10, color="#495057")
    ax_h.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{int(x):,}"))
    ax_h.text(
        0.01,
        -0.18,
        "Running total of security issues resolved since Feb 2026",
        transform=ax_h.transAxes,
        fontsize=8,
        color="#6c757d",
    )

    g = portfolio.open_breached
    mask_g = g.notna()
    if mask_g.any():
        ax_g.plot(d[mask_g], g[mask_g], color=c["open_breach"], linewidth=2.2)
    _annotate_latest_on_line(
        ax_g,
        d,
        portfolio.open_breached,
        label_fmt=lambda v: f"{int(round(v)):,.0f}",
        color=c["open_breach"],
        xytext=(8.0, 8.0),
    )
    ax_g.set_title("Open Issues Past SLA Deadline", fontsize=12, fontweight="bold", pad=6)
    ax_g.set_ylabel("Issues (count)", fontsize=10, color="#495057")
    ax_g.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{int(x):,}"))
    ax_g.text(
        0.01,
        -0.18,
        "Active issues already past their SLA deadline",
        transform=ax_g.transAxes,
        fontsize=8,
        color="#6c757d",
    )

    ax_ij.plot(d, portfolio.cum_within, label="Within SLA", color=c["within_sla"], linewidth=2.2)
    ax_ij.plot(
        d,
        portfolio.cum_breached,
        label="SLA Breached",
        color=c["breached"],
        linewidth=2.2,
        linestyle="--",
    )
    _annotate_latest_on_line(
        ax_ij,
        d,
        portfolio.cum_within,
        label_fmt=lambda v: f"Within {int(round(v)):,.0f}",
        color=c["within_sla"],
        xytext=(8.0, 12.0),
    )
    _annotate_latest_on_line(
        ax_ij,
        d,
        portfolio.cum_breached,
        label_fmt=lambda v: f"Breached {int(round(v)):,.0f}",
        color=c["breached"],
        xytext=(8.0, -14.0),
    )
    ax_ij.set_title("Fixed Issues: Within SLA vs Breached", fontsize=12, fontweight="bold", pad=6)
    ax_ij.set_ylabel("Issues (count)", fontsize=10, color="#495057")
    ax_ij.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{int(x):,}"))
    ax_ij.legend(loc="upper left", fontsize=9, framealpha=0.8)
    ax_ij.text(
        0.01,
        -0.18,
        "Cumulative split by outcome (solid = within, dashed = breached)",
        transform=ax_ij.transAxes,
        fontsize=8,
        color="#6c757d",
    )

    fig.suptitle(title, fontsize=14, fontweight="bold", color="#1a1a2e", y=1.02)
    fig.tight_layout(rect=[0, 0, 1, 1])
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=SAVE_DPI, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def post_compliance_to_slack(
    image_path: Path,
    portfolio_df: pd.DataFrame,
    *,
    token: str,
    channel_id: str,
) -> None:
    """Upload PNG with ``files_upload_v2``, then send Block Kit summary (same metrics as chart labels)."""
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError as e:
        raise SystemExit(
            "Slack integration requires slack-sdk. pip install slack-sdk"
        ) from e

    metrics = latest_portfolio_metrics(portfolio_df)
    blocks = build_slack_blocks(metrics)
    fallback = f"{SLACK_MESSAGE_FALLBACK_PREFIX} — {metrics['date']}"

    client = WebClient(token=token.strip())
    channel_b = channel_id.strip()

    try:
        client.files_upload_v2(
            channel=channel_b,
            file=str(image_path.resolve()),
            title=image_path.name,
        )
    except SlackApiError as e:
        err = e.response.get("error") if e.response else str(e)
        raise SystemExit(f"Slack files_upload_v2 failed: {err}") from e

    try:
        client.chat_postMessage(
            channel=channel_b,
            text=fallback,
            blocks=blocks,
        )
    except SlackApiError as e:
        err = e.response.get("error") if e.response else str(e)
        raise SystemExit(f"Slack chat_postMessage failed: {err}") from e

    print(f"Posted chart image + summary message to Slack channel {channel_b}")


def main() -> None:
    workbook = WORKBOOK_PATH.resolve()
    exclude_extra = frozenset(EXTRA_EXCLUDE_SHEETS)
    only = list(ONLY_PROJECT_SHEETS) if ONLY_PROJECT_SHEETS else None

    df, _keys = load_project_frames(
        workbook,
        start=DATE_START,
        exclude_sheets=exclude_extra,
        only_projects=only,
    )

    portfolio_df = build_portfolio_series(df)
    portfolio = chart_series_from_portfolio_df(portfolio_df)

    out = OUTPUT_PNG_PATH.resolve()
    plot_portfolio_merged_png(portfolio, out, CHART_TITLE)
    print(f"Wrote {out}")

    slack_token = SLACK_BOT_TOKEN.strip()
    slack_channel = SLACK_CHANNEL_ID.strip()
    if slack_token and slack_channel:
        post_compliance_to_slack(
            out,
            portfolio_df,
            token=slack_token,
            channel_id=slack_channel,
        )
    elif slack_token or slack_channel:
        raise SystemExit(
            "Set both SLACK_BOT_TOKEN and SLACK_CHANNEL_ID in this file to upload, "
            "or leave both empty to skip Slack."
        )


if __name__ == "__main__":
    main()
