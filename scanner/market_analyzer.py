"""Stock Market Screen Analyzer & Chromium Automation Module.

Detects trading and stock market screens, extracts structured stock data from the
central market region (symbol, price, change, change percentage, direction, volume, status),
classifies UP/DOWN/FLAT movements, maintains historical snapshot comparisons, and ranks top movers.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

from config import AppConfig, get_config
from utils.logger import get_logger

logger = get_logger("market_analyzer")


class StockSnapshot(BaseModel):
    """Structured, validated snapshot of an individual stock observation."""
    symbol: str = Field(..., description="Stock scrip / ticker identifier, e.g. RELIANCE")
    name: str = Field(default="", description="Full company / scrip name")
    price: float = Field(..., description="Current observed numerical price")
    previous_price: Optional[float] = Field(default=None, description="Previous observed price from prior snapshot")
    change: float = Field(default=0.0, description="Numerical price change amount")
    change_percent: float = Field(default=0.0, description="Percentage change (+1.50 for +1.5%)")
    direction: str = Field(default="FLAT", description="Direction: UP, DOWN, FLAT, or DATA_UNCERTAIN")
    volume: Optional[str] = Field(default="", description="Trading volume string if available")
    market_status: Optional[str] = Field(default="", description="e.g. Market Open, Market Closed")
    timestamp: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    source: str = Field(default="Chromium DOM", description="Extraction source")
    metadata: Dict[str, Any] = Field(default_factory=dict)


class MarketAnalysisResult(BaseModel):
    """Aggregate result from a complete market screen analysis cycle."""
    timestamp: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    scanner_state: str = Field(default="READY", description="IDLE, SCANNING, ANALYZING, READY, NO_DATA, ERROR, STOPPED")
    screen_detected: bool = Field(default=False)
    target_page_title: str = Field(default="")
    target_page_url: str = Field(default="")
    central_region_found: bool = Field(default=False)
    stocks_detected: int = Field(default=0)
    stocks: List[StockSnapshot] = Field(default_factory=list)
    top_gainers: List[StockSnapshot] = Field(default_factory=list)
    top_decliners: List[StockSnapshot] = Field(default_factory=list)
    flat_count: int = Field(default=0)
    uncertain_count: int = Field(default=0)
    reason: Optional[str] = Field(default=None)


# In-page JavaScript injected to accurately analyze the central market-data region and DOM
IN_PAGE_MARKET_EXTRACTOR_JS = r"""
(customSelectors) => {
    const cleanText = (t) => t ? t.replace(/\s+/g, ' ').trim() : '';
    
    // Parse numeric price: removes currency symbols (₹, $, €, £, INR, USD), commas, and spaces
    const parsePrice = (raw) => {
        if (!raw) return null;
        const cleaned = raw.replace(/[₹$€£INRUSD\s,]/gi, '');
        const match = cleaned.match(/[-+]?\d+(?:\.\d+)?/);
        return match ? parseFloat(match[0]) : null;
    };

    // Parse percentage: extracts number following + or - or %
    const parsePercent = (raw) => {
        if (!raw) return 0.0;
        const match = raw.match(/([+-]?\d+(?:\.\d+)?)\s*%/);
        if (match) return parseFloat(match[1]);
        const simple = raw.replace(/[₹$€£INRUSD\s,%()]/gi, '');
        const num = parseFloat(simple);
        return isNaN(num) ? 0.0 : num;
    };

    const isVisible = (el) => {
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0 && rect.bottom > 0;
    };

    const results = {
        screen_type: 'unknown',
        central_region_found: false,
        stocks: [],
        market_status: '',
        page_title: document.title,
        page_url: window.location.href
    };

    // Check market status badges
    const statusEl = document.querySelector('.tv-market-status__label, [class*="market-status"], [class*="marketStatus"]');
    if (statusEl) {
        results.market_status = cleanText(statusEl.innerText);
    }

    // A. SINGLE-SYMBOL HERO VIEW (TradingView /symbols/..., Yahoo Finance /quote/..., Google Finance, Dhan scrip detail)
    const h1 = document.querySelector('h1');
    const symbolHeaderTicker = document.querySelector('[class*="symbol-header-ticker"], [class*="quotesContainer"], [class*="quote-header"]');
    const lastPriceEl = document.querySelector(
        customSelectors?.price || 
        '.js-symbol-last, [class*="lastContainer"] [class*="last-"], [class*="lastBlock"] [class*="last-"], [data-field*="price"], [class*="currentPrice"]'
    );

    if (lastPriceEl && isVisible(lastPriceEl)) {
        const rawPriceText = cleanText(lastPriceEl.innerText);
        const parsedPrice = parsePrice(rawPriceText);

        if (parsedPrice !== null && !isNaN(parsedPrice)) {
            results.screen_type = 'single_symbol_hero';
            results.central_region_found = true;

            // Resolve Symbol
            let sym = '';
            if (symbolHeaderTicker) {
                const headerLines = cleanText(symbolHeaderTicker.innerText).split(' ');
                if (headerLines.length > 0 && /^[A-Z0-9._-]+$/i.test(headerLines[0])) {
                    sym = headerLines[0].toUpperCase();
                }
            }
            if (!sym) {
                const urlMatch = window.location.pathname.match(/\/symbols\/([A-Z0-9:_-]+)/i) ||
                                 window.location.pathname.match(/\/quote\/([A-Z0-9:_-]+)/i);
                if (urlMatch) {
                    sym = urlMatch[1].replace(/^(NSE|BSE):/i, '').toUpperCase();
                }
            }
            if (!sym && h1) {
                sym = cleanText(h1.innerText).slice(0, 20).toUpperCase();
            }

            // Resolve Company Name
            let name = h1 ? cleanText(h1.innerText) : sym;

            // Find parent quote container (traverse up to find container holding price and changes)
            let parentHero = lastPriceEl.parentElement;
            for (let i = 0; i < 7; i++) {
                if (!parentHero || parentHero === document.body) break;
                if (parentHero.innerText && (parentHero.innerText.includes('%') || parentHero.querySelector('[class*="change"], [class*="percent"]'))) {
                    break;
                }
                parentHero = parentHero.parentElement;
            }
            if (!parentHero) {
                parentHero = lastPriceEl.closest('[class*="quotesContainer"], [class*="quote-header"], [class*="hero"]') || document.body;
            }

            // Resolve Price Change and Percent
            let changeVal = 0.0;
            let percentVal = 0.0;
            
            // Priority 1: Direct specific change selectors
            const directChangeEl = document.querySelector(
                customSelectors?.change || 
                '.js-symbol-change-pt, [class*="change-pt"], [class*="js-symbol-change-direction"], [class*="changeContainer"] [class*="change-"], [class*="priceChange"]'
            );
            const directPercentEl = document.querySelector(
                customSelectors?.change_percent || 
                '.js-symbol-change-pt, [class*="js-symbol-change-direction"], [class*="percentage-"], [class*="percentChange"]'
            );

            if (directChangeEl) {
                const txt = cleanText(directChangeEl.innerText);
                const p = parsePrice(txt);
                if (p !== null) {
                    const isNeg = txt.includes('-') || directChangeEl.className.includes('down') || directChangeEl.className.includes('neg');
                    changeVal = isNeg && p > 0 ? -p : p;
                }
            }

            if (directPercentEl) {
                const txt = cleanText(directPercentEl.innerText);
                const pct = parsePercent(txt);
                if (pct !== 0.0) {
                    const isNeg = txt.includes('-') || directPercentEl.className.includes('down') || directPercentEl.className.includes('neg');
                    percentVal = isNeg && pct > 0 ? -pct : pct;
                }
            }

            // Priority 2: Search within parent hero elements
            if (!changeVal || !percentVal) {
                const changeEls = Array.from(parentHero.querySelectorAll('[class*="change"], [class*="percentage"], [class*="diff"], span, div'));
                for (const el of changeEls) {
                    const text = cleanText(el.innerText);
                    if (text.includes('%') && !percentVal) {
                        percentVal = parsePercent(text);
                        if (text.includes('-') || el.className.includes('down') || el.className.includes('neg')) {
                            if (percentVal > 0) percentVal = -percentVal;
                        }
                    } else if (/^[+-]?\d+(?:\.\d+)?$/.test(text.replace(/[INR₹$€\s,]/gi, '')) && !changeVal) {
                        const parsed = parsePrice(text);
                        if (parsed !== null && parsed !== parsedPrice) {
                            changeVal = text.includes('-') || el.className.includes('down') ? -Math.abs(parsed) : Math.abs(parsed);
                        }
                    }
                }
            }

            // Priority 3: Fallback regex against entire hero container text
            if (parentHero && (!changeVal || !percentVal)) {
                const heroText = parentHero.innerText || '';
                if (!percentVal) {
                    const pctMatch = heroText.match(/([+-]?\d+(?:\.\d+)?)\s*%/);
                    if (pctMatch) percentVal = parseFloat(pctMatch[1]);
                }
                if (!changeVal) {
                    const chgMatch = heroText.match(/([+-]\d+(?:\.\d+)?)\s*(?:INR|USD|EUR|[A-Z]{3})?/);
                    if (chgMatch) changeVal = parseFloat(chgMatch[1]);
                }
            }

            // Infer change from percent or vice versa if one is found
            if (!changeVal && percentVal) {
                changeVal = parseFloat(((parsedPrice * percentVal) / 100).toFixed(2));
            } else if (changeVal && !percentVal && parsedPrice > 0) {
                const prev = parsedPrice - changeVal;
                if (prev > 0) {
                    percentVal = parseFloat(((changeVal / prev) * 100).toFixed(2));
                }
            }

            results.stocks.push({
                symbol: sym || 'TARGET-STOCK',
                name: name,
                price: parsedPrice,
                change: changeVal,
                change_percent: percentVal,
                volume: '',
                market_status: results.market_status
            });
        }
    }

    // B. MULTI-STOCK WATCHLIST / MARKET SCREENER TABLE
    const rowSelector = customSelectors?.row || 'tr[class*="row"], table tbody tr, [role="row"], [class*="watchlist-item"], [class*="screener-row"]';
    const rows = Array.from(document.querySelectorAll(rowSelector));

    if (rows.length > 0) {
        let tableStocks = [];
        for (const row of rows) {
            if (!isVisible(row)) continue;
            
            const cells = Array.from(row.querySelectorAll('td, [role="cell"], [class*="cell"], div')).map(c => cleanText(c.innerText));
            if (cells.length < 2) continue;

            let rowSymbol = '';
            let rowName = '';
            let rowPrice = null;
            let rowChange = 0.0;
            let rowPercent = 0.0;

            const symEl = row.querySelector(customSelectors?.symbol || '[class*="symbol"], [class*="ticker"], a[href*="/symbols/"], strong');
            if (symEl) {
                rowSymbol = cleanText(symEl.innerText).split(' ')[0].toUpperCase();
            }

            for (const text of cells) {
                if (rowPrice === null) {
                    const p = parsePrice(text);
                    if (p !== null && p > 0 && !text.includes('%')) {
                        rowPrice = p;
                        continue;
                    }
                }
                if (text.includes('%') && rowPercent === 0.0) {
                    rowPercent = parsePercent(text);
                } else if (/^[+-]?\d+(?:\.\d+)?$/.test(text.replace(/[INR₹$€\s,]/gi, '')) && rowChange === 0.0) {
                    const ch = parsePrice(text);
                    if (ch !== null && ch !== rowPrice) {
                        rowChange = text.includes('-') ? -Math.abs(ch) : Math.abs(ch);
                    }
                }
            }

            if (rowSymbol && rowPrice !== null && rowPrice > 0) {
                tableStocks.push({
                    symbol: rowSymbol,
                    name: rowName || rowSymbol,
                    price: rowPrice,
                    change: rowChange,
                    change_percent: rowPercent,
                    volume: '',
                    market_status: results.market_status
                });
            }
        }

        if (tableStocks.length > 0) {
            results.central_region_found = true;
            results.screen_type = 'multi_stock_table';
            results.stocks = tableStocks;
        }
    }

    return results;
}
"""


class MarketAnalyzer:
    """Enterprise-grade Stock Market Screen Analyzer."""

    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or get_config()
        self._previous_snapshots: Dict[str, StockSnapshot] = {}
        self._last_analysis: Optional[MarketAnalysisResult] = None
        self._last_alert_time: float = 0.0

    @property
    def last_result(self) -> Optional[MarketAnalysisResult]:
        return self._last_analysis

    def detect_market_page(self, pages: List[Any]) -> Tuple[bool, Optional[Any], str]:
        """Inspects all open browser tabs to locate the stock market interface.
        
        Evaluates URL patterns, titles, and DOM structure.
        """
        if not pages:
            return False, None, "No open browser pages available."

        market_url_keywords = [
            "tradingview.com",
            "web.dhan.co",
            "zerodha.com",
            "moneycontrol.com",
            "nseindia.com",
            "finance.yahoo.com",
            "google.com/finance",
            "/symbols/",
            "/chart",
            "/market",
            "/stocks",
            "/quote",
            "/watchlist",
            "/trade",
        ]
        
        market_title_keywords = [
            "share price",
            "stock",
            "tradingview",
            "dhan",
            "nse:",
            "bse:",
            "quotes",
            "indices",
            "market",
            "screener",
            "reliance",
            "nifty",
        ]

        # 1. Search for explicit match in open pages
        for idx, page in enumerate(pages):
            try:
                if page.is_closed():
                    continue
                url = page.url.lower()
                title = ""
                # Safely inspect without blocking indefinitely
                if any(kw in url for kw in market_url_keywords):
                    logger.info(f"Target market page detected at tab #{idx + 1}: {url}")
                    return True, page, f"Target page detected: {url}"
            except Exception:
                continue

        # 2. Check active page title heuristics
        for idx, page in enumerate(pages):
            try:
                if page.is_closed():
                    continue
                title = (page.title() if hasattr(page, 'title') else "").lower()
                if any(kw in title for kw in market_title_keywords):
                    logger.info(f"Target market page identified by title at tab #{idx + 1}: {title}")
                    return True, page, f"Target page detected by title: {title}"
            except Exception:
                continue

        # 3. Fallback to first non-closed page
        for page in pages:
            if not page.is_closed():
                return True, page, f"Using available active page: {page.url}"

        return False, None, "No suitable market page could be identified."

    async def analyze_page(
        self,
        page: Any,
        custom_selectors: Optional[Dict[str, str]] = None
    ) -> MarketAnalysisResult:
        """Analyzes the stock-market interface displayed on the provided Chromium page."""
        if not page or page.is_closed():
            return MarketAnalysisResult(
                scanner_state="ERROR",
                screen_detected=False,
                reason="Browser page is not accessible or closed."
            )

        selectors = custom_selectors or {
            "region": self.config.market_region_selector,
            "row": self.config.stock_row_selector,
            "symbol": self.config.symbol_selector,
            "price": self.config.price_selector,
            "change": self.config.change_selector,
            "percent": self.config.change_percent_selector,
        }

        try:
            # Execute in-page extraction directly inside Chromium DOM context
            raw_data = await page.evaluate(IN_PAGE_MARKET_EXTRACTOR_JS, selectors)
        except Exception as exc:
            logger.error(f"DOM market extraction script failed: {exc}")
            return MarketAnalysisResult(
                scanner_state="ERROR",
                screen_detected=False,
                reason=f"Extraction failure: {exc}"
            )

        if not raw_data or not raw_data.get("stocks"):
            logger.warning(
                f"No stocks identified on {raw_data.get('page_url', 'current page')}. "
                f"Market region detected: {raw_data.get('central_region_found')}"
            )
            res = MarketAnalysisResult(
                scanner_state="NO_DATA",
                screen_detected=bool(raw_data.get("central_region_found")),
                target_page_title=raw_data.get("page_title", ""),
                target_page_url=raw_data.get("page_url", ""),
                central_region_found=bool(raw_data.get("central_region_found")),
                stocks_detected=0,
                reason="Market-data region or price indicators could not be identified."
            )
            self._last_analysis = res
            return res

        # Process and validate extracted stocks
        validated_snapshots: List[StockSnapshot] = []
        flat_count = 0
        uncertain_count = 0

        for item in raw_data["stocks"]:
            sym = str(item.get("symbol", "")).strip().upper()
            if not sym or len(sym) < 1:
                continue

            raw_price = item.get("price")
            if raw_price is None or not isinstance(raw_price, (int, float)) or raw_price <= 0:
                uncertain_count += 1
                continue

            price = float(raw_price)
            change = float(item.get("change", 0.0) or 0.0)
            change_pct = float(item.get("change_percent", 0.0) or 0.0)

            # Retrieve prior snapshot for price delta calculation
            prior = self._previous_snapshots.get(sym)
            previous_price = prior.price if prior else None

            # Determine direction strictly based on numerical values
            if change > 0.0001 or change_pct > 0.0001:
                direction = "UP"
            elif change < -0.0001 or change_pct < -0.0001:
                direction = "DOWN"
            elif abs(change) <= 0.0001 and abs(change_pct) <= 0.0001:
                direction = "FLAT"
                flat_count += 1
            else:
                direction = "DATA_UNCERTAIN"
                uncertain_count += 1

            snap = StockSnapshot(
                symbol=sym,
                name=item.get("name", sym),
                price=price,
                previous_price=previous_price,
                change=change,
                change_percent=change_pct,
                direction=direction,
                volume=item.get("volume", ""),
                market_status=item.get("market_status", ""),
                source=f"Chromium ({raw_data.get('screen_type', 'dom')})"
            )
            validated_snapshots.append(snap)
            self._previous_snapshots[sym] = snap

        # Compute Top Movers (Gainers) and Top Decliners
        gainers = sorted(
            [s for s in validated_snapshots if s.direction == "UP"],
            key=lambda x: x.change_percent,
            reverse=True
        )[:self.config.top_movers_limit]

        decliners = sorted(
            [s for s in validated_snapshots if s.direction == "DOWN"],
            key=lambda x: x.change_percent
        )[:self.config.top_decliners_limit]

        result = MarketAnalysisResult(
            scanner_state="READY" if validated_snapshots else "NO_DATA",
            screen_detected=True,
            target_page_title=raw_data.get("page_title", ""),
            target_page_url=raw_data.get("page_url", ""),
            central_region_found=True,
            stocks_detected=len(validated_snapshots),
            stocks=validated_snapshots,
            top_gainers=gainers,
            top_decliners=decliners,
            flat_count=flat_count,
            uncertain_count=uncertain_count,
        )

        self._last_analysis = result
        logger.info(
            f"Market Analysis Complete: {len(validated_snapshots)} stocks detected. "
            f"Gainers: {len(gainers)}, Decliners: {len(decliners)}, Flat: {flat_count}."
        )
        return result

    def should_dispatch_telegram_alert(self, result: MarketAnalysisResult) -> bool:
        """Determines if the current market analysis merits a Telegram notification."""
        if not self.config.telegram_market_alerts:
            return False
        candidates = result.stocks or (result.top_gainers + result.top_decliners)
        if not candidates:
            return False

        now = time.time()
        if (now - self._last_alert_time) < self.config.telegram_notification_cooldown:
            return False

        # If threshold is <= 0.0, dispatch any detected stocks on the screen automatically
        if self.config.telegram_min_change_percent <= 0.0:
            self._last_alert_time = now
            return True

        # Check if any detected stock breaches the configured change percentage threshold
        has_significant_mover = any(
            abs(s.change_percent) >= self.config.telegram_min_change_percent
            for s in candidates
        )
        if has_significant_mover:
            self._last_alert_time = now
            return True

        return False


# Global singleton instance
market_analyzer = MarketAnalyzer()
