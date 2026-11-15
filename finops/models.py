"""Time-series models for the bottom-up forecast lines (NO Streamlit; shared by the page, the report and the tests).

Eight models, simplest first. Every one takes a monthly Series (index = month starts) and returns the next `h` months:

  Flat                       last 3 months held flat
  Damped trend               last 3 months + Theil-Sen slope whose momentum fades 15% a month
  Linear                     last 3 months + the slope continuing forever
  Holt (ETS)                 damped additive exponential smoothing (statsmodels)
  Ensemble                   plain average of Flat, Damped trend and Holt (ETS)
  Seasonal naive x YoY       the same month last year x the median year-over-year ratio of the last 6 months
  Seasonal profile           Damped trend on the de-seasonalised line + a calendar-month profile (halved: with two years it is noisy)
  Holt-Winters (seasonal)    damped additive trend + additive seasonality (statsmodels)

Two years of data cannot prove seasonality, so the seasonal models are only SCORED when enough months exist to test them
(see MIN_TRAIN) and every model is judged on the SAME backtest months: a rolling origin, up to 3 months ahead.
"""
from __future__ import annotations
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=RuntimeWarning, module=r"statsmodels\.")
warnings.filterwarnings("ignore", category=UserWarning, module=r"statsmodels\.")

DAMPING = 0.85
MODELS = ["Flat", "Damped trend", "Linear", "Holt (ETS)", "Ensemble", "Seasonal naive × YoY", "Seasonal profile", "Holt-Winters (seasonal)"]
BASE = MODELS[:4]                                  # the original four: the Auto pool always contains them
AUTO_POOL = [m for m in MODELS if m != "Ensemble"]  # Auto picks the simplest within tolerance of the best (Ensemble is opt-in)
SEASONAL = {"Seasonal naive × YoY", "Seasonal profile", "Holt-Winters (seasonal)"}
ENSEMBLE_OF = ["Flat", "Damped trend", "Holt (ETS)"]
# months of history a model needs before it can forecast (and before a backtest origin can use it); seasonal models count from the first non-zero month
MIN_TRAIN = {"Flat": 5, "Damped trend": 5, "Linear": 5, "Holt (ETS)": 5, "Ensemble": 5,
             "Seasonal naive × YoY": 18, "Seasonal profile": 18, "Holt-Winters (seasonal)": 24}
MIN_ORIGINS = 4                                    # a model is only scored when at least this many backtest months exist
RATIO_CLIP = (0.6, 1.6)

HELP = {
    "Auto": "Backtest picks, per line, the simplest model within 5% of the most accurate one.",
    "Flat": "Last 3 months held flat.",
    "Damped trend": "Last 3 months plus the trend, fading 15% a month.",
    "Linear": "Last 3 months plus the trend continuing at the same slope (aggressive).",
    "Holt (ETS)": "Damped exponential smoothing.",
    "Ensemble": "Average of Flat, Damped trend and Holt: usually steadier than any single model on noisy lines.",
    "Seasonal naive × YoY": "Same month last year × recent year-over-year growth. Needs 18+ active months.",
    "Seasonal profile": "Damped trend plus a calendar-month profile (halved to limit noise). Needs 18+ active months.",
    "Holt-Winters (seasonal)": "Damped trend plus additive seasonality. Needs 24+ active months.",
}


def theil_sen(y) -> float:
    v = np.asarray(y, dtype=float)
    slopes = [(v[j] - v[i]) / (j - i) for i in range(len(v)) for j in range(i + 1, len(v))]
    return float(np.median(slopes)) if slopes else 0.0


# ------------------------------------------------------------------------------------------ the models
def _lead(y: np.ndarray) -> int:
    """Months before the first non-zero month (a line that did not exist yet)."""
    nz = np.flatnonzero(np.asarray(y) != 0)
    return int(nz[0]) if len(nz) else len(y)


def _classic(model: str, y: np.ndarray, h: int) -> np.ndarray:
    steps = np.arange(1, h + 1)
    anchor = y[-3:].mean() if len(y) >= 3 else y.mean()
    if model == "Flat":
        return np.full(h, anchor)
    slope = theil_sen(y)
    if model == "Damped trend":
        return anchor + slope * np.cumsum(DAMPING ** steps)
    if model == "Linear":
        return anchor + slope * steps
    if model == "Holt (ETS)":
        from statsmodels.tsa.holtwinters import ExponentialSmoothing
        with warnings.catch_warnings(), np.errstate(all="ignore"):
            warnings.simplefilter("ignore")
            fit = ExponentialSmoothing(y.astype(float), trend="add", damped_trend=True, initialization_method="estimated").fit()
        out = np.asarray(fit.forecast(h), dtype=float)
        if not np.isfinite(out).all():
            raise ValueError("non-finite ETS forecast")
        return out
    raise ValueError(model)


def _yoy_ratio(y: np.ndarray) -> float:
    cur, prev = y[-6:], y[-18:-12]
    ok = prev > 0
    return float(np.clip(np.median(cur[ok] / prev[ok]), *RATIO_CLIP)) if ok.any() else 1.0


def _seasonal_naive(y: np.ndarray, h: int) -> np.ndarray:
    n, ratio = len(y), _yoy_ratio(y)
    k = np.arange(1, h + 1)
    return y[n - 12 + (k - 1) % 12] * ratio ** ((k - 1) // 12 + 1)     # beyond 12 months out the base is two years back: two years of growth


def _profile(s: pd.Series) -> pd.Series:
    """Calendar-month effect (month number 1..12 -> $): residual around a Theil-Sen line, centred on zero, halved."""
    y = s.to_numpy(float)
    t = np.arange(len(y))
    slope = theil_sen(y)
    resid = y - (np.median(y - slope * t) + slope * t)
    prof = pd.Series(resid, index=s.index.month).groupby(level=0).mean()
    prof = prof - prof.mean()
    return 0.5 * prof.reindex(range(1, 13)).fillna(0.0)


def _seasonal_profile(s: pd.Series, h: int) -> np.ndarray:
    prof = _profile(s)
    d = s.to_numpy(float) - prof.reindex(s.index.month).to_numpy()
    fut = pd.date_range(s.index[-1] + pd.DateOffset(months=1), periods=h, freq="MS")
    return _classic("Damped trend", d, h) + prof.reindex(fut.month).to_numpy()


def _holt_winters(y: np.ndarray, h: int) -> np.ndarray:
    from statsmodels.tsa.holtwinters import ExponentialSmoothing
    with warnings.catch_warnings(), np.errstate(all="ignore"):
        warnings.simplefilter("ignore")
        fit = ExponentialSmoothing(y.astype(float), trend="add", damped_trend=True, seasonal="add", seasonal_periods=12,
                                   initialization_method="estimated").fit()
    out = np.asarray(fit.forecast(h), dtype=float)
    if not np.isfinite(out).all():
        raise ValueError("non-finite Holt-Winters forecast")
    return out


def active_months(s: pd.Series) -> int:
    return len(s) - _lead(s.to_numpy())


def can_fit(model: str, s: pd.Series) -> bool:
    n = active_months(s) if model in SEASONAL else len(s)
    return model in MIN_TRAIN and n >= MIN_TRAIN[model]


def predict(model: str, s: pd.Series, h: int, cache: dict | None = None) -> np.ndarray:
    """Next `h` months. Raises ValueError when the model cannot be fitted (too little history, or the fit failed)."""
    s = s.astype(float)
    if model in SEASONAL:
        s = s.iloc[_lead(s.to_numpy()):]
    if not can_fit(model, s):
        raise ValueError(f"{model} needs {MIN_TRAIN.get(model, '?')}+ months")
    key = (model, len(s), s.index[-1])
    if cache is not None and key in cache:
        return cache[key][:h] if len(cache[key]) >= h else _store(cache, key, model, s, h)
    return _store(cache, key, model, s, h)


def _store(cache, key, model, s, h):
    y = s.to_numpy(float)
    if model == "Ensemble":
        parts = []
        for c in ENSEMBLE_OF:
            try:
                parts.append(predict(c, s, h, cache))
            except Exception:
                continue
        if not parts:
            raise ValueError("no ensemble member could be fitted")
        out = np.mean(parts, axis=0)
    elif model == "Seasonal naive × YoY":
        out = _seasonal_naive(y, h)
    elif model == "Seasonal profile":
        out = _seasonal_profile(s, h)
    elif model == "Holt-Winters (seasonal)":
        out = _holt_winters(y, h)
    else:
        out = _classic(model, y, h)
    if cache is not None:
        cache[key] = out
    return out


# ------------------------------------------------------------------------------------------ backtest
def start_of(model: str, s: pd.Series) -> int:
    """First backtest origin (index into s) for this model."""
    return (_lead(s.to_numpy()) if model in SEASONAL else 0) + MIN_TRAIN[model]


def score(s: pd.Series, models: list[str] | None = None, max_h: int = 3, min_origins: int = MIN_ORIGINS) -> dict:
    """Rolling-origin backtest on ONE shared set of months, so the numbers are comparable.
    Returns {"table": {model: {mae, rmse}}, "origins": int, "first": int}; a model that cannot be tested (too little history) or that fails
    to fit at any origin is left out of the table."""
    s = s.astype(float)
    n = len(s)
    ok = [m for m in (models or MODELS) if n - start_of(m, s) >= min_origins]
    if not ok:
        return dict(table={}, origins=0, first=n)
    first = max(start_of(m, s) for m in ok)
    cache, table = {}, {}
    for m in ok:
        errs = []
        try:
            for t in range(first, n):
                h = min(max_h, n - t)
                errs.extend(predict(m, s.iloc[:t], h, cache) - s.to_numpy()[t:t + h])
        except Exception:
            continue
        e = np.asarray(errs)
        table[m] = dict(mae=float(np.mean(np.abs(e))), rmse=float(np.sqrt(np.mean(e ** 2))))
    return dict(table=table, origins=n - first, first=first)
