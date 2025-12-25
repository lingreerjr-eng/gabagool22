"""
Hedge Manager for YES/NO Arbitrage Strategy

Handles fill confirmation, partial fill scenarios, and hedge execution.
Implements both "unwind" and "complete" modes for partial fills.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Any, Tuple
import logging


class FillStatus(Enum):
    """Status of an order fill."""
    PENDING = "pending"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


class HedgeMode(Enum):
    """Mode for handling partial fills."""
    UNWIND = "unwind"      # Cancel unfilled, sell back filled
    COMPLETE = "complete"  # Cross aggressively to fill missing leg


@dataclass
class LegInfo:
    """Information about a trade leg."""
    order_id: str
    token_id: str
    side: str  # 'BUY' or 'SELL'
    intended_price: float
    intended_size: float
    filled_price: float = 0.0
    filled_size: float = 0.0
    status: FillStatus = FillStatus.PENDING
    
    @property
    def fill_ratio(self) -> float:
        """Ratio of filled to intended size."""
        if self.intended_size == 0:
            return 0
        return self.filled_size / self.intended_size
    
    @property
    def is_complete(self) -> bool:
        """Check if leg is fully filled."""
        return self.status == FillStatus.FILLED or self.fill_ratio >= 0.99


@dataclass
class HedgeResult:
    """Result of a hedge operation."""
    success: bool
    mode_used: HedgeMode
    filled_leg: Optional[LegInfo] = None
    hedge_leg: Optional[LegInfo] = None
    realized_pnl: float = 0.0
    message: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class BoxTradeResult:
    """Result of a complete box trade."""
    success: bool
    market_slug: str
    yes_leg: Optional[LegInfo] = None
    no_leg: Optional[LegInfo] = None
    intended_sum_price: float = 0.0
    actual_sum_price: float = 0.0
    intended_edge: float = 0.0
    actual_edge: float = 0.0
    hedge_result: Optional[HedgeResult] = None
    error: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    @property
    def is_complete_box(self) -> bool:
        """Check if both legs filled successfully."""
        return (
            self.yes_leg is not None and 
            self.no_leg is not None and
            self.yes_leg.is_complete and 
            self.no_leg.is_complete
        )


class HedgeManager:
    """
    Manages fill confirmation and hedge execution for box trades.
    
    When a box trade has one leg fill and the other fail, the hedge manager
    attempts to either:
    - UNWIND: Cancel the unfilled leg and sell back the filled leg
    - COMPLETE: Cross more aggressively to complete the box
    """
    
    def __init__(
        self,
        clob_client: Any,
        config: Optional[Dict[str, Any]] = None,
        logger: Optional[logging.Logger] = None
    ):
        """
        Initialize hedge manager.
        
        Args:
            clob_client: py-clob-client instance
            config: Configuration dictionary
            logger: Logger instance
        """
        self.client = clob_client
        config = config or {}
        self.hedge_timeout_ms = config.get('HEDGE_TIMEOUT_MS', 750)
        self.max_hedge_slippage = config.get('MAX_HEDGE_SLIPPAGE', 0.01)
        self.hedge_mode = HedgeMode(config.get('HEDGE_MODE', 'unwind'))
        self.max_loss_cap = config.get('MAX_LOSS_CAP', 0.05)  # 5% max loss on unwind
        self.poll_interval_ms = config.get('POLL_INTERVAL_MS', 100)
        self.logger = logger or logging.getLogger(__name__)
    
    async def confirm_fills(
        self,
        order_ids: List[str],
        timeout_ms: Optional[int] = None
    ) -> Dict[str, FillStatus]:
        """
        Poll order status until filled, cancelled, or timeout.
        
        Args:
            order_ids: List of order IDs to monitor
            timeout_ms: Override timeout in milliseconds
            
        Returns:
            Dictionary mapping order_id to FillStatus
        """
        timeout = timeout_ms or self.hedge_timeout_ms
        timeout_seconds = timeout / 1000.0
        poll_interval = self.poll_interval_ms / 1000.0
        
        statuses: Dict[str, FillStatus] = {
            oid: FillStatus.PENDING for oid in order_ids
        }
        
        start_time = asyncio.get_event_loop().time()
        
        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= timeout_seconds:
                self.logger.warning(f"⏰ Fill confirmation timeout after {timeout}ms")
                break
            
            all_complete = True
            for order_id in order_ids:
                if statuses[order_id] in [FillStatus.PENDING, FillStatus.PARTIAL]:
                    try:
                        status = await self._get_order_status(order_id)
                        statuses[order_id] = status
                        if status in [FillStatus.PENDING, FillStatus.PARTIAL]:
                            all_complete = False
                    except Exception as e:
                        self.logger.error(f"Error checking order {order_id}: {e}")
                        all_complete = False
            
            if all_complete:
                break
            
            await asyncio.sleep(poll_interval)
        
        return statuses
    
    async def _get_order_status(self, order_id: str) -> FillStatus:
        """Get status of a single order from CLOB."""
        try:
            # Use py-clob-client to get order status
            order = self.client.get_order(order_id)
            
            if order is None:
                return FillStatus.FAILED
            
            status_str = order.get('status', '').lower()
            
            if status_str == 'filled' or status_str == 'matched':
                return FillStatus.FILLED
            elif status_str == 'cancelled':
                return FillStatus.CANCELLED
            elif status_str == 'expired':
                return FillStatus.EXPIRED
            elif status_str == 'partially_filled':
                return FillStatus.PARTIAL
            else:
                return FillStatus.PENDING
                
        except Exception as e:
            self.logger.error(f"Error getting order status: {e}")
            return FillStatus.PENDING
    
    async def handle_partial_fill(
        self,
        filled_leg: LegInfo,
        unfilled_leg: LegInfo,
        market_slug: str
    ) -> HedgeResult:
        """
        Handle partial fill scenario based on configured hedge mode.
        
        Args:
            filled_leg: The leg that was filled
            unfilled_leg: The leg that failed to fill
            market_slug: Market identifier for logging
            
        Returns:
            HedgeResult with details of the hedge operation
        """
        self.logger.warning(
            f"⚠️ Partial fill on {market_slug}: "
            f"{filled_leg.side} filled {filled_leg.filled_size:.2f} @ {filled_leg.filled_price:.4f}, "
            f"{unfilled_leg.side} unfilled"
        )
        
        if self.hedge_mode == HedgeMode.UNWIND:
            return await self._execute_unwind(filled_leg, unfilled_leg, market_slug)
        else:
            return await self._execute_complete(filled_leg, unfilled_leg, market_slug)
    
    async def _execute_unwind(
        self,
        filled_leg: LegInfo,
        unfilled_leg: LegInfo,
        market_slug: str
    ) -> HedgeResult:
        """
        Unwind mode: Cancel unfilled leg and sell back the filled leg.
        
        Args:
            filled_leg: The leg to unwind (sell back)
            unfilled_leg: The leg to cancel
            market_slug: Market identifier
            
        Returns:
            HedgeResult with unwind details
        """
        self.logger.info(f"🔄 Executing UNWIND hedge for {market_slug}")
        
        # 1. Cancel the unfilled order
        try:
            if unfilled_leg.status == FillStatus.PENDING:
                await self._cancel_order(unfilled_leg.order_id)
                self.logger.info(f"✅ Cancelled unfilled {unfilled_leg.side} order")
        except Exception as e:
            self.logger.warning(f"Could not cancel unfilled order: {e}")
        
        # 2. Sell back the filled position
        try:
            # Calculate maximum acceptable loss
            min_sell_price = filled_leg.filled_price * (1 - self.max_loss_cap)
            
            hedge_leg = await self._place_market_sell(
                token_id=filled_leg.token_id,
                size=filled_leg.filled_size,
                min_price=min_sell_price
            )
            
            if hedge_leg and hedge_leg.status == FillStatus.FILLED:
                # Calculate realized PnL
                buy_cost = filled_leg.filled_price * filled_leg.filled_size
                sell_proceeds = hedge_leg.filled_price * hedge_leg.filled_size
                realized_pnl = sell_proceeds - buy_cost
                
                self.logger.info(
                    f"✅ Unwind complete: Sold {hedge_leg.filled_size:.2f} @ "
                    f"{hedge_leg.filled_price:.4f}, PnL: ${realized_pnl:.4f}"
                )
                
                return HedgeResult(
                    success=True,
                    mode_used=HedgeMode.UNWIND,
                    filled_leg=filled_leg,
                    hedge_leg=hedge_leg,
                    realized_pnl=realized_pnl,
                    message="Unwind successful"
                )
            else:
                return HedgeResult(
                    success=False,
                    mode_used=HedgeMode.UNWIND,
                    filled_leg=filled_leg,
                    message="Unwind sell order failed"
                )
                
        except Exception as e:
            self.logger.error(f"Error executing unwind: {e}")
            return HedgeResult(
                success=False,
                mode_used=HedgeMode.UNWIND,
                filled_leg=filled_leg,
                message=f"Unwind error: {str(e)}"
            )
    
    async def _execute_complete(
        self,
        filled_leg: LegInfo,
        unfilled_leg: LegInfo,
        market_slug: str
    ) -> HedgeResult:
        """
        Complete mode: Cross more aggressively to fill the missing leg.
        
        Args:
            filled_leg: The leg already filled
            unfilled_leg: The leg to complete aggressively
            market_slug: Market identifier
            
        Returns:
            HedgeResult with completion details
        """
        self.logger.info(f"🎯 Executing COMPLETE hedge for {market_slug}")
        
        try:
            # 1. Cancel the existing unfilled order
            if unfilled_leg.status == FillStatus.PENDING:
                await self._cancel_order(unfilled_leg.order_id)
            
            # 2. Place a more aggressive order
            aggressive_price = unfilled_leg.intended_price * (1 + self.max_hedge_slippage)
            
            hedge_leg = await self._place_aggressive_buy(
                token_id=unfilled_leg.token_id,
                size=unfilled_leg.intended_size,
                max_price=aggressive_price
            )
            
            if hedge_leg and hedge_leg.status == FillStatus.FILLED:
                # Box is now complete
                actual_sum = filled_leg.filled_price + hedge_leg.filled_price
                actual_edge = 1.0 - actual_sum
                
                self.logger.info(
                    f"✅ Box completed: Sum price = {actual_sum:.4f}, "
                    f"Edge = {actual_edge:.4f}"
                )
                
                return HedgeResult(
                    success=True,
                    mode_used=HedgeMode.COMPLETE,
                    filled_leg=filled_leg,
                    hedge_leg=hedge_leg,
                    realized_pnl=actual_edge * min(filled_leg.filled_size, hedge_leg.filled_size),
                    message="Box completed successfully"
                )
            else:
                # Complete failed, fall back to unwind
                self.logger.warning("Complete failed, falling back to unwind")
                return await self._execute_unwind(filled_leg, unfilled_leg, market_slug)
                
        except Exception as e:
            self.logger.error(f"Error executing complete: {e}")
            # Fall back to unwind
            return await self._execute_unwind(filled_leg, unfilled_leg, market_slug)
    
    async def _cancel_order(self, order_id: str) -> bool:
        """Cancel an order by ID."""
        try:
            self.client.cancel(order_id)
            return True
        except Exception as e:
            self.logger.error(f"Error cancelling order {order_id}: {e}")
            return False
    
    async def _place_market_sell(
        self,
        token_id: str,
        size: float,
        min_price: float
    ) -> Optional[LegInfo]:
        """Place a market sell order with price floor."""
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import SELL
            
            order_args = MarketOrderArgs(
                token_id=str(token_id),
                amount=float(size),
                side=SELL,
            )
            signed_order = self.client.create_market_order(order_args)
            response = self.client.post_order(signed_order, OrderType.FOK)
            
            if response.get('success'):
                data = response.get('data', {})
                return LegInfo(
                    order_id=data.get('orderID', ''),
                    token_id=token_id,
                    side='SELL',
                    intended_price=min_price,
                    intended_size=size,
                    filled_price=float(data.get('avgPrice', min_price)),
                    filled_size=float(data.get('filledAmount', size)),
                    status=FillStatus.FILLED
                )
            return None
        except Exception as e:
            self.logger.error(f"Error placing market sell: {e}")
            return None
    
    async def _place_aggressive_buy(
        self,
        token_id: str,
        size: float,
        max_price: float
    ) -> Optional[LegInfo]:
        """Place an aggressive buy order at max_price."""
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY
            
            order_args = MarketOrderArgs(
                token_id=str(token_id),
                amount=float(size * max_price),  # Amount in USDC
                side=BUY,
            )
            signed_order = self.client.create_market_order(order_args)
            response = self.client.post_order(signed_order, OrderType.FOK)
            
            if response.get('success'):
                data = response.get('data', {})
                return LegInfo(
                    order_id=data.get('orderID', ''),
                    token_id=token_id,
                    side='BUY',
                    intended_price=max_price,
                    intended_size=size,
                    filled_price=float(data.get('avgPrice', max_price)),
                    filled_size=float(data.get('filledAmount', size)),
                    status=FillStatus.FILLED
                )
            return None
        except Exception as e:
            self.logger.error(f"Error placing aggressive buy: {e}")
            return None
    
    def log_trade_summary(self, result: BoxTradeResult):
        """Log a summary of the box trade result."""
        status = "✅" if result.success else "❌"
        
        self.logger.info(
            f"\n{'='*60}\n"
            f"{status} BOX TRADE SUMMARY: {result.market_slug}\n"
            f"{'='*60}\n"
            f"  Intended sum_price: {result.intended_sum_price:.4f}\n"
            f"  Intended edge:      {result.intended_edge:.4f}\n"
        )
        
        if result.yes_leg:
            self.logger.info(
                f"  YES leg: {result.yes_leg.status.value} | "
                f"Price: {result.yes_leg.filled_price:.4f} | "
                f"Size: {result.yes_leg.filled_size:.2f}"
            )
        
        if result.no_leg:
            self.logger.info(
                f"  NO leg:  {result.no_leg.status.value} | "
                f"Price: {result.no_leg.filled_price:.4f} | "
                f"Size: {result.no_leg.filled_size:.2f}"
            )
        
        if result.is_complete_box:
            self.logger.info(
                f"  Actual sum_price:   {result.actual_sum_price:.4f}\n"
                f"  Actual edge:        {result.actual_edge:.4f}"
            )
        
        if result.hedge_result:
            self.logger.info(
                f"  Hedge: {result.hedge_result.mode_used.value} | "
                f"PnL: ${result.hedge_result.realized_pnl:.4f}"
            )
        
        if result.error:
            self.logger.error(f"  Error: {result.error}")
        
        self.logger.info(f"{'='*60}\n")
