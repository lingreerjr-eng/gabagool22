"""
Market Filter for YES/NO Arbitrage Strategy

Discovers and filters Polymarket markets suitable for 15-min crypto arbitrage.
Uses the Gamma API for market discovery and filtering.
"""
import asyncio
import aiohttp
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Set
import logging


@dataclass
class MarketInfo:
    """Information about a tradeable market."""
    condition_id: str
    slug: str
    question: str
    yes_token_id: str
    no_token_id: str
    close_time: datetime
    tags: List[str] = field(default_factory=list)
    volume: float = 0.0
    liquidity: float = 0.0
    
    @property
    def time_to_close_minutes(self) -> float:
        """Minutes until market closes."""
        delta = self.close_time - datetime.now(timezone.utc)
        return delta.total_seconds() / 60.0
    
    def __repr__(self) -> str:
        return f"MarketInfo(slug={self.slug}, close_in={self.time_to_close_minutes:.1f}min)"


@dataclass
class MarketFilterConfig:
    """Configuration for market filtering."""
    target_tags: List[str] = field(default_factory=lambda: ['Crypto', 'Bitcoin', 'Ethereum', 'Solana'])
    max_time_to_close_min: int = 0  # 0 = no limit (all markets)
    min_volume: float = 0.0
    min_liquidity: float = 0.0
    exclude_tags: List[str] = field(default_factory=list)
    require_active: bool = True


class MarketFilter:
    """
    Filters Polymarket markets for arbitrage opportunities.
    
    Focuses on:
    - Crypto-related markets (Bitcoin, Ethereum, etc.)
    - Short-expiry markets (within MAX_TIME_TO_CLOSE_MIN)
    - Active binary markets with YES/NO tokens
    """
    
    GAMMA_API_BASE = "https://gamma-api.polymarket.com"
    CLOB_API_BASE = "https://clob.polymarket.com"
    
    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        logger: Optional[logging.Logger] = None
    ):
        """
        Initialize market filter.
        
        Args:
            config: Configuration dictionary with filter parameters
            logger: Logger instance for output
        """
        config = config or {}
        # Default values for configuration
        default_tags = ['Crypto', 'Bitcoin', 'Ethereum', 'Solana']
        
        self.config = MarketFilterConfig(
            target_tags=config.get('TARGET_TAGS', default_tags),
            max_time_to_close_min=config.get('MAX_TIME_TO_CLOSE_MIN', 20),
            min_volume=config.get('MIN_VOLUME', 0.0),
            min_liquidity=config.get('MIN_LIQUIDITY', 0.0),
            exclude_tags=config.get('EXCLUDE_TAGS', []),
        )
        self.logger = logger or logging.getLogger(__name__)
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, MarketInfo] = {}
        self._last_fetch: Optional[datetime] = None
        self._cache_ttl_seconds = 30
    
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
                    self.logger.error(f"HTTP {response.status} from {url}")
                    return None
                return await response.json()
        except asyncio.TimeoutError:
            self.logger.error(f"Timeout fetching {url}")
            return None
        except aiohttp.ClientError as e:
            self.logger.error(f"Client error fetching {url}: {e}")
            return None
    
    async def get_target_markets(self) -> List[MarketInfo]:
        """
        Fetch and filter markets suitable for arbitrage.
        
        Returns:
            List of MarketInfo objects meeting filter criteria
        """
        # Check cache freshness
        now = datetime.now(timezone.utc)
        if (self._last_fetch and 
            (now - self._last_fetch).total_seconds() < self._cache_ttl_seconds and
            self._cache):
            return list(self._cache.values())
        
        markets = []
        
        # Fetch events from Gamma API
        events = await self._fetch_events()
        if not events:
            self.logger.warning("No events fetched from Gamma API")
            return markets
        
        # CLEAR CACHE: We rebuild it from scratch to evict expired/missing markets
        self._cache.clear()
        
        for event in events:
            try:
                market = await self._parse_event(event)
                if market and self._matches_filters(market):
                    markets.append(market)
                    self._cache[market.condition_id] = market
            except Exception as e:
                self.logger.debug(f"Error parsing event: {e}")
                continue
        
        self._last_fetch = now
        self.logger.info(f"📊 Found {len(markets)} target markets for arbitrage")
        return markets
    
    async def _fetch_events(self) -> List[Dict]:
        """Fetch active events from Gamma API."""
        all_events = []
        
        # Fetch 15-minute recurring crypto markets using tag_slug
        # This is the key query that returns BTC, ETH, SOL, XRP 15-min markets
        url = f"{self.GAMMA_API_BASE}/events"
        params = {
            'tag_slug': '15M',  # This tag identifies 15-minute recurring markets
            'closed': 'false',
            'active': 'true',
            'limit': 100,
        }
        
        events = await self._fetch_json(url, params)
        if events and isinstance(events, list):
            all_events.extend(events)
            self.logger.info(f"Fetched {len(events)} 15M-tagged events from Gamma API")
        else:
            self.logger.warning("No 15M-tagged events found, falling back to general crypto search")
            # Fallback: try general crypto search
            for tag in self.config.target_tags:
                params = {
                    'tag_slug': tag.lower(),
                    'closed': 'false',
                    'active': 'true',
                    'limit': 100,
                }
                events = await self._fetch_json(url, params)
                if events and isinstance(events, list):
                    all_events.extend(events)
                await asyncio.sleep(0.1)
        
        # Deduplicate by event ID
        seen_ids: Set[str] = set()
        unique_events = []
        for event in all_events:
            event_id = event.get('id')
            if event_id and event_id not in seen_ids:
                seen_ids.add(event_id)
                unique_events.append(event)
        
        return unique_events

    
    async def _parse_event(self, event: Dict) -> Optional[MarketInfo]:
        """Parse event data into MarketInfo."""
        markets = event.get('markets', [])
        if not markets:
            self.logger.debug(f"Event {event.get('slug', 'unknown')}: no markets")
            return None
        
        # For binary markets, we expect 1 market
        # For multi-outcome, skip for now
        if len(markets) != 1:
            self.logger.debug(f"Event {event.get('slug', 'unknown')}: {len(markets)} markets (need exactly 1)")
            return None
        
        market = markets[0]
        
        # Extract token IDs - handle JSON string format
        tokens_raw = market.get('clobTokenIds', [])
        if isinstance(tokens_raw, str):
            try:
                import json
                tokens = json.loads(tokens_raw)
            except (json.JSONDecodeError, ValueError):
                self.logger.debug(f"Event {event.get('slug', 'unknown')}: failed to parse clobTokenIds")
                return None
        else:
            tokens = tokens_raw
        
        if not isinstance(tokens, list) or len(tokens) != 2:
            self.logger.debug(f"Event {event.get('slug', 'unknown')}: need exactly 2 tokens, got {len(tokens) if isinstance(tokens, list) else 'non-list'}")
            return None
        
        # Parse close time
        end_date_str = market.get('endDate') or event.get('endDate')
        if not end_date_str:
            self.logger.debug(f"Event {event.get('slug', 'unknown')}: no endDate")
            return None
        
        try:
            close_time = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
        except ValueError:
            self.logger.debug(f"Event {event.get('slug', 'unknown')}: invalid endDate format")
            return None
        
        # Extract tags
        tags = []
        event_tags = event.get('tags', [])
        if isinstance(event_tags, list):
            for tag in event_tags:
                if isinstance(tag, dict):
                    tags.append(tag.get('label', ''))
                elif isinstance(tag, str):
                    tags.append(tag)
        
        return MarketInfo(
            condition_id=market.get('conditionId', ''),
            slug=event.get('slug', ''),
            question=market.get('question', ''),
            yes_token_id=str(tokens[0]),
            no_token_id=str(tokens[1]),
            close_time=close_time,
            tags=tags,
            volume=float(market.get('volume', 0) or 0),
            liquidity=float(market.get('liquidityNum', 0) or 0),
        )

    
    def _matches_filters(self, market: MarketInfo) -> bool:
        """Check if market matches filter criteria."""
        # Check time to close
        time_to_close = market.time_to_close_minutes
        if time_to_close <= 0:
            self.logger.debug(f"Rejected {market.slug}: already closed")
            return False  # Already closed
        
        # Only apply max_time_to_close filter if > 0
        if self.config.max_time_to_close_min > 0 and time_to_close > self.config.max_time_to_close_min:
            self.logger.debug(f"Rejected {market.slug}: closes in {time_to_close:.1f}min > max {self.config.max_time_to_close_min}min")
            return False
        
        # Check tags - at least one target tag must match
        market_tags_lower = [t.lower() for t in market.tags]
        target_tags_lower = [t.lower() for t in self.config.target_tags]
        
        # Strict Asset Matching: If 'bitcoin' or 'ethereum' are targeted, ensure one matches
        asset_targets = [t for t in target_tags_lower if t in ['bitcoin', 'ethereum', 'btc', 'eth']]
        
        # Also check question text for crypto keywords
        question_lower = market.question.lower()
        
        has_specific_asset = any(
            any(asset in market_tag for market_tag in market_tags_lower) or (asset in question_lower)
            for asset in asset_targets
        )
        
        if asset_targets and not has_specific_asset:
            return False
        
        has_target_tag = any(
            any(target in market_tag for market_tag in market_tags_lower)
            for target in target_tags_lower
        )
        
        if not has_target_tag and not any(k in question_lower for k in asset_targets):
            return False
        
        # Check exclude tags
        for exclude_tag in self.config.exclude_tags:
            if exclude_tag.lower() in market_tags_lower:
                return False
        
        # Check minimum volume/liquidity
        if market.volume < self.config.min_volume:
            return False
        if market.liquidity < self.config.min_liquidity:
            return False
        
        # Check token IDs are valid
        if not market.yes_token_id or not market.no_token_id:
            return False
        
        return True
    
    async def get_market_by_id(self, condition_id: str) -> Optional[MarketInfo]:
        """Get a specific market by condition ID."""
        if condition_id in self._cache:
            return self._cache[condition_id]
        
        # Fetch fresh if not in cache
        markets = await self.get_target_markets()
        return self._cache.get(condition_id)
    
    def clear_cache(self):
        """Clear the market cache."""
        self._cache.clear()
        self._last_fetch = None
