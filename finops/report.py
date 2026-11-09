"""Self-contained HTML report framework: light theme, fixed 960px width, print CSS (landscape Letter).
Charts are embedded Plotly (the library is inlined once), so the file opens offline and prints to PDF from any browser."""
from __future__ import annotations
import html as _html
import re
from datetime import datetime

import pandas as pd

from .ui import style_fig

PAGE_W = 960          # px; fits a landscape Letter page with 0.4in margins

CSS = """
@page { size: Letter landscape; margin: 0.4in; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: #0f172a;
       background: #f1f5f9; margin: 0; line-height: 1.45; }
.page { width: %(w)spx; margin: 0 auto; padding: 24px 0 48px; }
header.top { border-bottom: 3px solid #2563eb; padding-bottom: 10px; margin-bottom: 16px; }
header.top h1 { margin: 0; font-size: 26px; letter-spacing: -.01em; }
header.top .sub { color: #475569; font-size: 13px; margin-top: 4px; }
section { background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 16px 18px; margin: 0 0 14px;
          break-inside: avoid; page-break-inside: avoid; }
section.flow { break-inside: auto; page-break-inside: auto; }
section.newpage { break-before: page; page-break-before: always; }
h2 { font-size: 17px; margin: 0 0 8px; } h3 { font-size: 14px; margin: 12px 0 6px; color: #334155; }
.kpis { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; }
.kpi { border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px 12px; background: #f8fafc; }
.kpi .l { font-size: 11px; color: #64748b; text-transform: uppercase; letter-spacing: .04em; }
.kpi .v { font-size: 22px; font-weight: 700; margin: 2px 0; } .kpi .d { font-size: 12px; color: #475569; }
.kpi .d.bad { color: #b91c1c; } .kpi .d.good { color: #15803d; }
.banner { border-radius: 8px; padding: 10px 14px; margin: 8px 0; font-size: 14px; }
.banner.success { background: #dcfce7; color: #14532d; border: 1px solid #86efac; }
.banner.error { background: #fee2e2; color: #7f1d1d; border: 1px solid #fca5a5; }
.banner.info { background: #e0f2fe; color: #0c4a6e; border: 1px solid #7dd3fc; }
.cap { color: #64748b; font-size: 12px; margin: 4px 0 0; }
.row { display: flex; gap: 12px; align-items: flex-start; } .row > div { min-width: 0; }
table { border-collapse: collapse; width: 100%%; font-size: 12px; margin: 6px 0; }
th { text-align: left; background: #f1f5f9; color: #334155; padding: 6px 8px; border-bottom: 1px solid #cbd5e1; }
td { padding: 5px 8px; border-bottom: 1px solid #e2e8f0; } td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tr:nth-child(even) td { background: #f8fafc; }
td.good { color: #15803d; font-weight: 600; } td.bad { color: #b91c1c; font-weight: 600; }
tr.strong td { font-weight: 700; border-top: 2px solid #cbd5e1; }
ul { margin: 6px 0 6px 20px; padding: 0; font-size: 13px; }
footer { color: #64748b; font-size: 11px; border-top: 1px solid #e2e8f0; padding-top: 10px; margin-top: 18px; }
.js-plotly-plot, .plotly-graph-div { max-width: 100%%; }
@media print { body { background: #fff; } .page { padding: 0; } section { border-color: #cbd5e1; } }
""" % {"w": PAGE_W}


def md(s: str) -> str:
    """Tiny markdown subset (**bold**, `code`) -> safe HTML."""
    s = _html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?![\*\w])", r"<i>\1</i>", s)
    return re.sub(r"`(.+?)`", r"<code>\1</code>", s)


class Report:
    def __init__(self, title: str, subtitle: str):
        self.title, self.subtitle = title, subtitle
        self.parts: list[str] = []
        self._plotly_inlined = False
        self._open = False

    # ---- structure --------------------------------------------------------------------
    def section(self, heading: str | None = None, flow: bool = False, new_page: bool = False):
        self.end()
        self.parts.append(f'<section class="{"flow" if flow else ""} {"newpage" if new_page else ""}">' + (f"<h2>{md(heading)}</h2>" if heading else ""))
        self._open = True

    def end(self):
        if self._open:
            self.parts.append("</section>")
            self._open = False

    # ---- blocks -----------------------------------------------------------------------
    def kpis(self, items: list[dict]):
        cells = []
        for it in items:
            d = it.get("delta") or ""
            cls = ""
            if d and d not in ("–",) and not it.get("neutral"):
                neg = d.strip().startswith(("-", "−"))
                bad = (not neg) if it.get("inverse") else neg
                cls = "bad" if bad else "good"
            cells.append(f'<div class="kpi"><div class="l">{md(it["label"])}</div><div class="v">{md(str(it["value"]))}</div>'
                         f'<div class="d {cls}">{md(d) if d else "&nbsp;"}</div></div>')
        self.parts.append('<div class="kpis">' + "".join(cells) + "</div>")

    def banner(self, kind: str, text: str):
        self.parts.append(f'<div class="banner {kind}">{md(text)}</div>')

    def text(self, s: str):
        self.parts.append(f"<p>{md(s)}</p>")

    def small(self, s: str):
        self.parts.append(f'<p style="font-size:12px;margin:4px 0">{md(s)}</p>')

    def caption(self, s: str):
        self.parts.append(f'<p class="cap">{md(s)}</p>')

    def bullets(self, items: list[str]):
        self.parts.append("<ul>" + "".join(f"<li>{md(i)}</li>" for i in items) + "</ul>")

    def figure(self, fig, width: int = PAGE_W - 40, height: int | None = None) -> str:
        fig = style_fig(fig, "light")
        fig.update_layout(width=width, height=height or fig.layout.height or 380, autosize=False)
        inc = False if self._plotly_inlined else True            # inline plotly.js once (self-contained file)
        self._plotly_inlined = True
        return fig.to_html(full_html=False, include_plotlyjs=inc, config={"displayModeBar": False, "responsive": False})

    def chart(self, fig, width: int = PAGE_W - 40, height: int | None = None):
        self.parts.append(self.figure(fig, width, height))

    def charts_row(self, figs: list, widths: list[int]):
        self.parts.append('<div class="row">' + "".join(f"<div>{self.figure(f, w)}</div>" for f, w in zip(figs, widths) if f is not None) + "</div>")

    def columns(self, cells: list[str], widths: list[int]):
        """Side-by-side blocks of ready-made HTML (use figure()/table_html() to build them)."""
        self.parts.append('<div class="row">' + "".join(f'<div style="width:{w}px">{c}</div>' for c, w in zip(cells, widths)) + "</div>")

    def table(self, df: pd.DataFrame, index: bool = False):
        self.parts.append(self.table_html(df, index))

    def table_money(self, df: pd.DataFrame, fmt, var_rows=(), var_cols=(), strong_rows=(), index_name: str = "") -> str:
        """Numeric DataFrame -> HTML. fmt(value)->str. Variance rows/cols are green when >= 0 (under budget), red when < 0."""
        head = "".join(f'<th class="{"num" if i else ""}">{_html.escape(str(c))}</th>' for i, c in enumerate([index_name] + list(df.columns)))
        body = []
        for idx, row in df.iterrows():
            label = idx.strftime("%b %Y") if isinstance(idx, pd.Timestamp) else str(idx)
            isvar_row = label in var_rows
            cells = [f"<td>{_html.escape(label)}</td>"]
            for c, v in row.items():
                if v is None or (isinstance(v, float) and pd.isna(v)):
                    cells.append('<td class="num">–</td>'); continue
                var = (isvar_row or c in var_cols) and abs(v) >= 0.5
                cls = ("good" if v >= 0 else "bad") if var else ""
                cells.append(f'<td class="num {cls}">{("+" if var and v > 0 else "")}{_html.escape(fmt(v))}</td>')
            body.append(f'<tr class="{"strong" if label in strong_rows else ""}">' + "".join(cells) + "</tr>")
        return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"

    def table_html(self, df: pd.DataFrame, index: bool = False) -> str:
        d = df.reset_index() if index else df
        isnum = lambda s: bool(re.fullmatch(r"[\-+−]?\$?[\d,\.]+[KM]?%?", s.strip())) if s else False
        numcol = [all(isnum("" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)) for v in d[c] if str(v) not in ("", "nan", "None"))
                  and len(d) > 0 for c in d.columns]
        head = "".join(f'<th class="{"num" if n else ""}">{_html.escape(str(c))}</th>' for c, n in zip(d.columns, numcol))
        rows = []
        for r in d.itertuples(index=False):
            cells = []
            for v in r:
                s = "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)
                cells.append(f'<td class="{"num" if (numcol[len(cells)] and s) else ""}">{_html.escape(s)}</td>')
            rows.append("<tr>" + "".join(cells) + "</tr>")
        return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"

    # ---- output -----------------------------------------------------------------------
    def render(self, footer: str) -> str:
        self.end()
        return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                f"<title>{_html.escape(self.title)}</title><style>{CSS}</style></head><body><div class=\"page\">"
                f'<header class="top"><h1>{_html.escape(self.title)}</h1><div class="sub">{md(self.subtitle)}</div></header>'
                + "".join(self.parts) + f"<footer>{md(footer)}</footer></div></body></html>")


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
