# -*- coding: utf-8 -*-
"""
YunaiFetcher - YunAI Quant market data source.

The API token is read as a complete Authorization header value so a copied
curl command can be imported without exposing the token in logs.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Optional

import pandas as pd
import requests

from .base import BaseFetcher, DataFetchError, STANDARD_COLUMNS, normalize_stock_code
from .realtime_types import RealtimeSource, UnifiedRealtimeQuote, safe_float, safe_int

logger = logging.getLogger(__name__)

DEFAULT_YUNAI_BASE_URL = "https://quant.yunai.com.cn/quant-market"


def _parse_priority(value: str | None, default: int = 1) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        logger.warning("Invalid YUNAI_PRIORITY=%r; falling back to %s", value, default)
        return default


def _clean_base_url(value: str | None) -> str:
    base = (value or DEFAULT_YUNAI_BASE_URL).strip().rstrip("/")
    marker = "/api/v1/"
    if marker in base:
        base = base.split(marker, 1)[0].rstrip("/")
    return base or DEFAULT_YUNAI_BASE_URL


def _ratio_to_percent(value: Any) -> Optional[float]:
    number = safe_float(value)
    if number is None:
        return None
    if abs(number) <= 1:
        number *= 100
    return round(number, 2)


def _timestamp_ms_to_iso(value: Any) -> Optional[str]:
    timestamp = safe_float(value)
    if timestamp is None or timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp / 1000).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


class YunaiFetcher(BaseFetcher):
    name = "YunaiFetcher"
    priority = _parse_priority(os.getenv("YUNAI_PRIORITY"), 1)

    def __init__(self):
        from src.config import get_config

        config = get_config()
        self._authorization = (
            getattr(config, "yunai_authorization", None)
            or os.getenv("YUNAI_AUTHORIZATION")
            or os.getenv("YUNAI_API_KEY")
            or ""
        ).strip()
        self._base_url = _clean_base_url(
            getattr(config, "yunai_base_url", None) or os.getenv("YUNAI_BASE_URL")
        )
        self._right_option = (os.getenv("YUNAI_RIGHT_OPTION") or "br").strip() or "br"
        if self._authorization and not self._authorization.lower().startswith("bearer "):
            self._authorization = f"Bearer {self._authorization}"
        if not self._authorization:
            logger.debug("[YunAI] Authorization not configured, fetcher disabled")

    @staticmethod
    def has_configured_credentials(config: Any | None = None) -> bool:
        value = (
            getattr(config, "yunai_authorization", None)
            if config is not None
            else None
        ) or os.getenv("YUNAI_AUTHORIZATION") or os.getenv("YUNAI_API_KEY")
        return bool((value or "").strip())

    @staticmethod
    def _symbol(stock_code: str) -> str:
        normalized = normalize_stock_code(stock_code).strip().upper()
        if normalized.startswith("HK") and normalized[2:].isdigit():
            return normalized[2:].zfill(5)
        if normalized.endswith(".HK"):
            return normalized[:-3].zfill(5)
        if normalized.endswith((".SH", ".SZ", ".BJ")):
            return normalized.split(".", 1)[0]
        return normalized

    def _headers(self) -> dict[str, str]:
        if not self._authorization:
            raise DataFetchError("[YunAI] Authorization not configured")
        return {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Authorization": self._authorization,
        }

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        url = f"{self._base_url}{path}"
        try:
            self.random_sleep(0.1, 0.3)
            response = requests.post(url, json=payload, headers=self._headers(), timeout=20)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise DataFetchError(f"[YunAI] HTTP request failed: {exc}") from exc

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        symbol = self._symbol(stock_code)
        payload = {
            "symbols": [symbol],
            "barType": "day",
            "rightOption": self._right_option,
            "startDate": start_date,
            "endDate": end_date,
        }
        data = self._post("/api/v1/quantitative/quotes/bars-range", payload)
        rows = data.get(symbol) if isinstance(data, dict) else None
        if not rows:
            raise DataFetchError(f"[YunAI] No daily bars returned for {symbol}")
        return pd.DataFrame(rows)

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        if df.empty:
            return df

        out = df.copy()
        if "date" in out.columns:
            out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.date
        elif "time" in out.columns:
            out["date"] = pd.to_datetime(out["time"], unit="ms", errors="coerce").dt.date
        else:
            raise DataFetchError("[YunAI] Daily bars missing date/time field")

        for col in ["open", "high", "low", "close", "volume", "amount"]:
            if col not in out.columns:
                out[col] = None
            out[col] = pd.to_numeric(out[col], errors="coerce")
        out = out.sort_values("date", ascending=True).reset_index(drop=True)
        out["pct_chg"] = out["close"].pct_change().fillna(0) * 100
        out["pct_chg"] = out["pct_chg"].round(2)
        out["code"] = normalize_stock_code(stock_code)
        keep = ["code"] + STANDARD_COLUMNS
        return out[[col for col in keep if col in out.columns]]

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        if not self._authorization:
            return None

        symbol = self._symbol(stock_code)
        try:
            data = self._post(
                "/api/v1/quantitative/quotes/real-time-quotes",
                {"symbols": [symbol]},
            )
        except Exception as exc:
            logger.warning("[YunAI] Realtime quote failed for %s: %s", symbol, exc)
            return None

        quote = data.get(symbol) if isinstance(data, dict) else None
        if not isinstance(quote, dict):
            return None

        price = safe_float(quote.get("latestPrice") or quote.get("close"))
        if price is None or price <= 0:
            return None

        high = safe_float(quote.get("high"))
        low = safe_float(quote.get("low"))
        pre_close = safe_float(quote.get("preClose"))
        amplitude = None
        if high is not None and low is not None and pre_close and pre_close > 0:
            amplitude = round((high - low) / pre_close * 100, 2)

        total_mv = safe_float(quote.get("marketCap"))
        if total_mv is not None and total_mv > 0:
            total_mv = round(total_mv / 10000, 2)

        return UnifiedRealtimeQuote(
            code=normalize_stock_code(stock_code),
            name=str(quote.get("name") or ""),
            source=RealtimeSource.YUNAI,
            price=price,
            change_pct=_ratio_to_percent(quote.get("changeRate")),
            change_amount=safe_float(quote.get("change")),
            volume=safe_int(quote.get("volume")),
            amount=safe_float(quote.get("amount")),
            volume_ratio=safe_float(quote.get("volumeRatio")),
            turnover_rate=_ratio_to_percent(quote.get("turnoverRate")),
            amplitude=amplitude,
            open_price=safe_float(quote.get("open")),
            high=high,
            low=low,
            pre_close=pre_close,
            pe_ratio=safe_float(quote.get("peRatio")),
            pb_ratio=safe_float(quote.get("pbRatio")),
            total_mv=total_mv,
            provider_timestamp=_timestamp_ms_to_iso(quote.get("latestTime")),
        )

    def get_market_status(self, market: str = "ALL", lang: str = "zh_CN") -> Optional[list[dict[str, Any]]]:
        try:
            response = requests.get(
                f"{self._base_url}/api/v1/quantitative/quotes/market-status",
                params={"market": market, "lang": lang},
                headers=self._headers(),
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.debug("[YunAI] Market status failed: %s", exc)
            return None
        return data if isinstance(data, list) else None
