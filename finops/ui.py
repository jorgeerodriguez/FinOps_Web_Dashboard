from __future__ import annotations
import pandas as pd
import streamlit as st
from .config import load_config
from .data import load_all
from .schema import CLOUD, MARKETPLACE, TAX

COLORS = {"AWS": "#E07B00", "GCP": "#2563EB"}
TEMPLATE = "plotly_white"


def money(v: float) -> str:
    if pd.isna(v):
        return "–"
    a = abs(v)
    s = f"${a/1e6:,.2f}M" if a >= 1e6 else f"${a/1e3:,.0f}K" if a >= 1e4 else f"${a:,.0f}"
    return "-" + s if v < 0 else s


def pct(v: float) -> str:
    return "–" if pd.isna(v) else f"{v:+.1%}"


VIEWS = {
    "Cloud Services (budget basis)": [CLOUD],
    "Marketplace": [MARKETPLACE],
    "Tax": [TAX],
    "All (reference only)": [CLOUD, MARKETPLACE, TAX],
}


import contextvars
from contextlib import contextmanager

_VIEW_OVERRIDE: contextvars.ContextVar = contextvars.ContextVar("view_override", default=None)


@contextmanager
def force_view(name: str):
    """Pin the spend category regardless of the sidebar (used by report generation)."""
    tok = _VIEW_OVERRIDE.set(name)
    try:
        yield
    finally:
        _VIEW_OVERRIDE.reset(tok)


def current_view() -> str:
    return _VIEW_OVERRIDE.get() or st.session_state.get("cat_view", "Cloud Services (budget basis)")


def view_picker() -> None:
    """Global sidebar selector (called once from app.py). Marketplace is NEVER mixed into the default view."""
    st.sidebar.radio("Spend category", list(VIEWS), key="cat_view",
                     help="Budgets apply to Cloud Services only. Marketplace and Tax are always reported separately.")


def filter_view(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["category"].isin(VIEWS[current_view()])]


@st.cache_data(show_spinner="Loading billing data…")
def _load(include_partial: bool) -> pd.DataFrame:
    return load_all(include_partial)


def get_data(include_partial: bool = False, apply_view: bool = True) -> pd.DataFrame:
    df = _load(include_partial)
    return filter_view(df) if apply_view else df


def sidebar_filters(df: pd.DataFrame, providers: bool = True) -> pd.DataFrame:
    """Shared sidebar. Returns the filtered frame."""
    st.sidebar.markdown("### Filters")
    if st.sidebar.button("Reload data"):
        st.cache_data.clear()
        st.rerun()
    if providers:
        sel = st.sidebar.multiselect("Provider", ["AWS", "GCP"], default=["AWS", "GCP"])
        df = df[df["provider"].isin(sel)]
    return df


def require_data(df: pd.DataFrame, what: str = "billing data") -> None:
    """Friendly first-run screen instead of a traceback when the store is empty (or a filter leaves nothing)."""
    if df is None or df.empty:
        st.info(f"No {what} yet. Open **Data Health** and click **Refresh AWS** / **Refresh GCP** "
                "(your own credentials), then come back.")
        st.stop()


def cfg() -> dict:
    return load_config()


def annual_budget(year: int, providers: list[str] | None = None) -> float:
    """12 x monthly budget for the selected providers (0 if not set)."""
    if current_view() != "Cloud Services (budget basis)":
        return 0.0        # budgets are set on Cloud Services only
    b = load_config().get("budget", {}).get(str(year), {})
    return 12 * sum(b.get(f"{p.lower()}_monthly", 0) for p in (providers or ["AWS", "GCP"]))


def ytd_budget(year: int, through_month: int, providers: list[str] | None = None) -> float:
    return annual_budget(year, providers) / 12 * through_month


# ----------------------------------------------------------------------------- theme (follows the system / Settings menu)
# Charts are authored in the light palette; style_fig() adapts them. Pure function (no Streamlit) so a PDF/HTML
# export can call style_fig(fig, "light") to force the light variant regardless of the viewer's theme.
_LIGHT_TO_DARK = {          # only the colors that lose contrast on a dark background
    "#0f172a": "#e2e8f0",   # ink / text
    "#334155": "#cbd5e1",   # "actual" bars, secondary ink
    "#475569": "#94a3b8",
    "#64748b": "#94a3b8",   # muted
    "#e2e8f0": "#334155",   # grid lines
    "#7c3aed": "#a78bfa",   # purple
    "#b45309": "#d97706",   # marketplace (AWS)
    "#15803d": "#4ade80",   # good / under budget
    "#b91c1c": "#f87171",   # bad / over budget
}
DARK_BG = "rgba(0,0,0,0)"


def theme_type() -> str:
    """'dark' or 'light' from the viewer's active Streamlit theme (system setting by default). Dark if unknown."""
    try:
        return st.context.theme.type or "dark"
    except Exception:
        return "dark"


def is_dark() -> bool:
    return theme_type() == "dark"


def good_color() -> str:
    return "#4ade80" if is_dark() else "#15803d"


def bad_color() -> str:
    return "#f87171" if is_dark() else "#b91c1c"


def style_fig(fig, mode: str | None = None):
    """Return the figure adapted to `mode` ('light'|'dark'; default = the active theme)."""
    import plotly.io as pio
    mode = mode or theme_type()
    if mode != "dark":
        fig.update_layout(template="plotly_white")
        return fig
    import re
    # one pass, so a swapped color is never swapped again (e.g. ink -> light -> dark)
    s = re.sub(r'"(#[0-9a-fA-F]{6})"', lambda m: f'"{_LIGHT_TO_DARK.get(m.group(1).lower(), m.group(1))}"', fig.to_json())
    out = pio.from_json(s)
    out.update_layout(template="plotly_dark", paper_bgcolor=DARK_BG, plot_bgcolor=DARK_BG)
    return out


def show(fig, container=None, **kw) -> None:
    """st.plotly_chart with the theme applied. Use instead of st.plotly_chart everywhere.
    container: a column / tab / expander to draw into (defaults to the current container)."""
    (container or st).plotly_chart(style_fig(fig), theme=None, **kw)


def heat_scale(kind: str):
    """Heatmap color scale readable on the active theme. kind: 'blue' | 'purple'."""
    if is_dark():
        return {"blue": [[0, "#172554"], [1, "#60a5fa"]], "purple": [[0, "#2e1065"], [1, "#a78bfa"]]}[kind]
    return {"blue": "Blues", "purple": "Purples"}[kind]
