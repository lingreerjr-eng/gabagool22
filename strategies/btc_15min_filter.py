"""
Bitcoin 15-Minute Market Filter

Specifically targets Bitcoin 15-minute window markets on Polymarket.
These markets have the format "BTC up or down from HH:MM to HH:MM UTC"
and resolve based on Chainlink oracle price data.
"""
import asyncio
import aiohttp
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import logging
import re


@dataclass
class BTC15MinMarket:
    """Information about a Bitcoin 15-minute market."""
    condition_id: str
    question: str
    slug: str
    yes_token_id: str
    no_token_id: str
    window_start: datetime
    window_end: datetime
    yes_price: float = 0.5
    no_price: float = 0.5
    liquidity: float = 0.0
    volume: float = 0.0
    
    @property
    def minutes_until_close(self) -> float:
        """Minutes until market closes."""
        delta = self.window_end - datetime.now(timezone.utc)
        return delta.total_seconds() / 60.0
    
    @property
    def sum_price(self) -> float:
        """Sum of YES + NO prices."""
        return self.yes_price + self.no_price
    
    def __repr__(self) -> str:
        return f"BTC15MinMarket(window={self.window_start.strftime('%H:%M')}-{self.window_end.strftime('%H:%M')} UTC, sum={self.sum_price:.3f})"


class BTC15MinFilter:
    """
    Filters for Bitcoin 15-minute markets only.
    
    These markets follow a pattern like:
    - "Will Bitcoin go up or down from 12:00 to 12:15 UTC?"
    - Markets are available for the CURRENT 15-min window and upcoming windows
    
    The filter calculates the correct window times and finds the appropriate market.
    """
    
    GAMMA_API_BASE = "https://gamma-api.polymarket.com"
    CLOB_API_BASE = "https://clob.polymarket.com"
    
    # Known series slugs for BTC 15-min markets
    BTC_SERIES_PATTERNS = [
        r'bitcoin.*15.*min',
        r'btc.*15.*min',
        r'bitcoin.*up.*down',
        r'btc.*up.*down.*minute',
    ]
    
    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        min_liquidity: float = 0.0
    ):
        self.logger = logger or logging.getLogger(__name__)
        self.min_liquidity = min_liquidity
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, BTC15MinMarket] = {}
        self._last_fetch: Optional[datetime] = None
        self._cache_ttl_seconds = 10  # Short TTL for 15-min markets
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session
    
    async def close(self):
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def _fetch_json(self, url: str, params: Optional[Dict] = None) -> Optional[Any]:
        """Fetch JSON from URL with error handling."""
        session = await self._get_session()
        try:
            async with session.get(url, params=params) as response:
                if response.status == 429:
                    self.logger.warning(f"Rate limited on {url}, backing off...")
                    await asyncio.sleep(2)
                    return None
                if response.status != 200:
                    self.logger.debug(f"HTTP {response.status} from {url}")
                    return None
                return await response.json()
        except asyncio.TimeoutError:
            self.logger.error(f"Timeout fetching {url}")
            return None
        except aiohttp.ClientError as e:
            self.logger.error(f"Client error fetching {url}: {e}")
            return None
    
    def get_current_15min_window(self) -> tuple[datetime, datetime]:
        """
        Calculate the current 15-minute window in UTC.
        
        Returns (window_start, window_end) where:
        - window_start is the start of the current 15-min interval
        - window_end is window_start + 15 minutes
        """
        now = datetime.now(timezone.utc)
        # Round down to the nearest 15 minutes
        minute_of_interval = now.minute // 15 * 15
        window_start = now.replace(minute=minute_of_interval, second=0, microsecond=0)
        window_end = window_start + timedelta(minutes=15)
        return window_start, window_end
    
    def get_next_15min_window(self) -> tuple[datetime, datetime]:
        """Get the next 15-minute window (after current)."""
        current_start, current_end = self.get_current_15min_window()
        next_start = current_end
        next_end = next_start + timedelta(minutes=15)
        return next_start, next_end
    
    async def get_btc_15min_markets(self) -> List[BTC15MinMarket]:
        """
        Fetch Bitcoin 15-minute markets for the current and next windows.
        
        Returns list of BTC15MinMarket objects.
        """
        # Check cache freshness
        now = datetime.now(timezone.utc)
        if (self._last_fetch and 
            (now - self._last_fetch).total_seconds() < self._cache_ttl_seconds and
            self._cache):
            return list(self._cache.values())
        
        markets = []
        
        # Try multiple approaches to find BTC 15-min markets
        
        # Approach 1: Search for series with "bitcoin" and time-related keywords
        events = await self._search_btc_events()
        
        for event in events:
            market = await self._parse_btc_15min_event(event)
            if market:
                markets.append(market)
                self._cache[market.condition_id] = market
        
        self._last_fetch = now
        
        if markets:
            self.logger.info(f"🪙 Found {len(markets)} Bitcoin 15-min markets")
        else:
            self.logger.debug("No Bitcoin 15-min markets found in current window")
        
        return markets
    
    async def _search_btc_events(self) -> List[Dict]:
        """Search for Bitcoin 15-minute market events."""
        all_events = []
        
        # Try searching with different keywords
        search_terms = [
            "bitcoin 15 min",
            "bitcoin up down",
            "BTC minute",
        ]
        
        for term in search_terms:
            url = f"{self.GAMMA_API_BASE}/events"
            params = {
                'closed': 'false',
                'active': 'true',
                'limit': 100,
                '_q': term,  # Search query
            }
            
            events = await self._fetch_json(url, params)
            if events and isinstance(events, list):
                all_events.extend(events)
            
            await asyncio.sleep(0.1)  # Be nice to the API
        
        # Also try series endpoint
        series_url = f"{self.GAMMA_API_BASE}/series"
        series = await self._fetch_json(series_url)
        if series and isinstance(series, list):
            for s in series:
                title = s.get('title', '').lower()
                slug = s.get('slug', '').lower()
                # Check if this is a BTC 15-min series
                if any(re.search(pattern, title) or re.search(pattern, slug) 
                       for pattern in self.BTC_SERIES_PATTERNS):
                    # Fetch events for this series
                    series_slug = s.get('slug')
                    if series_slug:
                        events_url = f"{self.GAMMA_API_BASE}/events"
                        events = await self._fetch_json(events_url, {'series_slug': series_slug})
                        if events and isinstance(events, list):
                            all_events.extend(events)
        
        # Deduplicate by event ID
        seen_ids = set()
        unique_events = []
        for event in all_events:
            event_id = event.get('id')
            if event_id and event_id not in seen_ids:
                seen_ids.add(event_id)
                unique_events.append(event)
        
        return unique_events
    
    async def _parse_btc_15min_event(self, event: Dict) -> Optional[BTC15MinMarket]:
        """Parse event data to check if it's a valid BTC 15-min market."""
        title = event.get('title', '').lower()
        slug = event.get('slug', '').lower()
        
        # Check if this is a Bitcoin 15-minute market
        is_btc = 'bitcoin' in title or 'btc' in title
        is_15min = '15' in title and ('min' in title or 'minute' in title)
        is_up_down = 'up' in title and 'down' in title
        
        if not (is_btc and (is_15min or is_up_down)):
            return None
        
        markets = event.get('markets', [])
        if not markets:
            return None
        
        # For binary markets, we expect 1 market
        if len(markets) != 1:
            return None
        
        market = markets[0]
        
        # Extract token IDs
        tokens_str = market.get('clobTokenIds', '[]')
        if isinstance(tokens_str, str):
            try:
                import json
                tokens = json.loads(tokens_str)
            except:
                tokens = []
        else:
            tokens = tokens_str
            
        if len(tokens) != 2:
            return None
        
        # Parse close time
        end_date_str = market.get('endDate') or event.get('endDate')
        if not end_date_str:
            return None
        
        try:
            close_time = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
        except ValueError:
            return None
        
        # Check if this market is for current or next 15-min window
        current_start, current_end = self.get_current_15min_window()
        next_start, next_end = self.get_next_15min_window()
        
        # The market end date should match a 15-min boundary
        is_current_window = abs((close_time - current_end).total_seconds()) < 60  # Within 1 min
        is_next_window = abs((close_time - next_end).total_seconds()) < 60
        
        if not (is_current_window or is_next_window):
            return None  # Not a market for our target windows
        
        window_start = current_start if is_current_window else next_start
        window_end = current_end if is_current_window else next_end
        
        # Get prices
        outcome_prices_str = market.get('outcomePrices', '[0.5, 0.5]')
        if isinstance(outcome_prices_str, str):
            try:
                import json
                outcome_prices = json.loads(outcome_prices_str)
            except:
                outcome_prices = [0.5, 0.5]
        else:
            outcome_prices = outcome_prices_str or [0.5, 0.5]
        
        yes_price = float(outcome_prices[0]) if len(outcome_prices) > 0 else 0.5
        no_price = float(outcome_prices[1]) if len(outcome_prices) > 1 else 0.5
        
        # Check liquidity
        liquidity = float(market.get('liquidityNum', 0) or 0)
        if liquidity < self.min_liquidity:
            return None
        
        return BTC15MinMarket(
            condition_id=market.get('conditionId', ''),
            question=market.get('question', ''),
            slug=market.get('slug', ''),
            yes_token_id=str(tokens[0]),
            no_token_id=str(tokens[1]),
            window_start=window_start,
            window_end=window_end,
            yes_price=yes_price,
            no_price=no_price,
            liquidity=liquidity,
            volume=float(market.get('volumeNum', 0) or 0),
        )
    
    async def get_current_window_market(self) -> Optional[BTC15MinMarket]:
        """Get the market for the current 15-minute window only."""
        markets = await self.get_btc_15min_markets()
        current_start, current_end = self.get_current_15min_window()
        
        for market in markets:
            # Check if this market's window matches the current window
            start_match = abs((market.window_start - current_start).total_seconds()) < 60
            end_match = abs((market.window_end - current_end).total_seconds()) < 60
            if start_match and end_match:
                return market
        
        return None
    
    def clear_cache(self):
        """Clear the market cache."""
        self._cache.clear()
        self._last_fetch = None
