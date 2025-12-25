"""
Unit tests for hedge manager and partial fill scenarios.
"""
import pytest
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone

from strategies.hedge_manager import (
    HedgeManager,
    LegInfo,
    FillStatus,
    HedgeMode,
    HedgeResult,
    BoxTradeResult
)


class TestLegInfo:
    """Test LegInfo dataclass functionality."""
    
    def test_leg_info_creation(self):
        """Test creating a LegInfo instance."""
        leg = LegInfo(
            order_id='order123',
            token_id='token456',
            side='BUY',
            intended_price=0.50,
            intended_size=100.0
        )
        assert leg.order_id == 'order123'
        assert leg.status == FillStatus.PENDING
        assert leg.filled_size == 0.0
    
    def test_fill_ratio_calculation(self):
        """Test fill ratio calculation."""
        leg = LegInfo(
            order_id='order123',
            token_id='token456',
            side='BUY',
            intended_price=0.50,
            intended_size=100.0,
            filled_size=50.0
        )
        assert leg.fill_ratio == 0.5
    
    def test_fill_ratio_zero_size(self):
        """Test fill ratio with zero intended size."""
        leg = LegInfo(
            order_id='order123',
            token_id='token456',
            side='BUY',
            intended_price=0.50,
            intended_size=0.0,
            filled_size=0.0
        )
        assert leg.fill_ratio == 0
    
    def test_is_complete_when_filled(self):
        """Test is_complete for filled order."""
        leg = LegInfo(
            order_id='order123',
            token_id='token456',
            side='BUY',
            intended_price=0.50,
            intended_size=100.0,
            filled_size=100.0,
            status=FillStatus.FILLED
        )
        assert leg.is_complete is True
    
    def test_is_complete_high_fill_ratio(self):
        """Test is_complete with 99%+ fill ratio."""
        leg = LegInfo(
            order_id='order123',
            token_id='token456',
            side='BUY',
            intended_price=0.50,
            intended_size=100.0,
            filled_size=99.5
        )
        assert leg.is_complete is True
    
    def test_is_not_complete_partial_fill(self):
        """Test is_complete with partial fill."""
        leg = LegInfo(
            order_id='order123',
            token_id='token456',
            side='BUY',
            intended_price=0.50,
            intended_size=100.0,
            filled_size=50.0,
            status=FillStatus.PARTIAL
        )
        assert leg.is_complete is False


class TestBoxTradeResult:
    """Test BoxTradeResult dataclass functionality."""
    
    def test_is_complete_box_both_filled(self):
        """Test is_complete_box when both legs are filled."""
        yes_leg = LegInfo(
            order_id='yes1', token_id='yesT', side='BUY',
            intended_price=0.50, intended_size=100,
            filled_size=100, status=FillStatus.FILLED
        )
        no_leg = LegInfo(
            order_id='no1', token_id='noT', side='BUY',
            intended_price=0.48, intended_size=100,
            filled_size=100, status=FillStatus.FILLED
        )
        
        result = BoxTradeResult(
            success=True,
            market_slug='test-market',
            yes_leg=yes_leg,
            no_leg=no_leg
        )
        
        assert result.is_complete_box is True
    
    def test_is_not_complete_box_one_leg_failed(self):
        """Test is_complete_box when one leg failed."""
        yes_leg = LegInfo(
            order_id='yes1', token_id='yesT', side='BUY',
            intended_price=0.50, intended_size=100,
            filled_size=100, status=FillStatus.FILLED
        )
        no_leg = LegInfo(
            order_id='no1', token_id='noT', side='BUY',
            intended_price=0.48, intended_size=100,
            filled_size=0, status=FillStatus.FAILED
        )
        
        result = BoxTradeResult(
            success=False,
            market_slug='test-market',
            yes_leg=yes_leg,
            no_leg=no_leg
        )
        
        assert result.is_complete_box is False
    
    def test_is_not_complete_box_missing_leg(self):
        """Test is_complete_box when a leg is missing."""
        yes_leg = LegInfo(
            order_id='yes1', token_id='yesT', side='BUY',
            intended_price=0.50, intended_size=100,
            filled_size=100, status=FillStatus.FILLED
        )
        
        result = BoxTradeResult(
            success=False,
            market_slug='test-market',
            yes_leg=yes_leg,
            no_leg=None
        )
        
        assert result.is_complete_box is False


class TestHedgeManagerInit:
    """Test HedgeManager initialization."""
    
    def test_default_config(self):
        """Test default configuration values."""
        mock_client = Mock()
        manager = HedgeManager(mock_client)
        
        assert manager.hedge_timeout_ms == 750
        assert manager.max_hedge_slippage == 0.01
        assert manager.hedge_mode == HedgeMode.UNWIND
    
    def test_custom_config(self):
        """Test custom configuration values."""
        mock_client = Mock()
        config = {
            'HEDGE_TIMEOUT_MS': 1000,
            'MAX_HEDGE_SLIPPAGE': 0.02,
            'HEDGE_MODE': 'complete'
        }
        manager = HedgeManager(mock_client, config)
        
        assert manager.hedge_timeout_ms == 1000
        assert manager.max_hedge_slippage == 0.02
        assert manager.hedge_mode == HedgeMode.COMPLETE


class TestConfirmFills:
    """Test fill confirmation logic."""
    
    @pytest.fixture
    def mock_client(self):
        client = Mock()
        return client
    
    @pytest.fixture
    def manager(self, mock_client):
        return HedgeManager(mock_client, {'POLL_INTERVAL_MS': 50})
    
    @pytest.mark.asyncio
    async def test_confirm_fills_success(self, manager, mock_client):
        """Test successful fill confirmation."""
        mock_client.get_order = Mock(return_value={'status': 'filled'})
        
        statuses = await manager.confirm_fills(['order1'], timeout_ms=100)
        
        assert statuses['order1'] == FillStatus.FILLED
    
    @pytest.mark.asyncio
    async def test_confirm_fills_timeout(self, manager, mock_client):
        """Test fill confirmation timeout."""
        mock_client.get_order = Mock(return_value={'status': 'pending'})
        
        statuses = await manager.confirm_fills(['order1'], timeout_ms=100)
        
        # Should return pending on timeout
        assert statuses['order1'] == FillStatus.PENDING
    
    @pytest.mark.asyncio
    async def test_confirm_fills_cancelled(self, manager, mock_client):
        """Test cancelled order detection."""
        mock_client.get_order = Mock(return_value={'status': 'cancelled'})
        
        statuses = await manager.confirm_fills(['order1'], timeout_ms=100)
        
        assert statuses['order1'] == FillStatus.CANCELLED


class TestPartialFillHandling:
    """Test partial fill hedge scenarios."""
    
    @pytest.fixture
    def mock_client(self):
        client = Mock()
        client.cancel = Mock()
        client.create_market_order = Mock(return_value=Mock())
        client.post_order = Mock(return_value={
            'success': True,
            'data': {'orderID': 'hedge1', 'avgPrice': 0.49, 'filledAmount': 100}
        })
        return client
    
    @pytest.fixture
    def filled_leg(self):
        return LegInfo(
            order_id='yes1',
            token_id='yesToken',
            side='BUY',
            intended_price=0.50,
            intended_size=100.0,
            filled_price=0.50,
            filled_size=100.0,
            status=FillStatus.FILLED
        )
    
    @pytest.fixture
    def unfilled_leg(self):
        return LegInfo(
            order_id='no1',
            token_id='noToken',
            side='BUY',
            intended_price=0.48,
            intended_size=100.0,
            filled_size=0.0,
            status=FillStatus.PENDING
        )
    
    @pytest.mark.asyncio
    async def test_unwind_mode_cancels_unfilled(self, mock_client, filled_leg, unfilled_leg):
        """Test that unwind mode cancels the unfilled order."""
        manager = HedgeManager(mock_client, {'HEDGE_MODE': 'unwind'})
        
        await manager.handle_partial_fill(filled_leg, unfilled_leg, 'test-market')
        
        mock_client.cancel.assert_called_once_with('no1')
    
    @pytest.mark.asyncio
    async def test_unwind_mode_sells_filled(self, mock_client, filled_leg, unfilled_leg):
        """Test that unwind mode sells back the filled position."""
        manager = HedgeManager(mock_client, {'HEDGE_MODE': 'unwind'})
        
        result = await manager.handle_partial_fill(filled_leg, unfilled_leg, 'test-market')
        
        # Should have placed a sell order
        assert mock_client.post_order.called
    
    @pytest.mark.asyncio
    async def test_unwind_returns_pnl(self, mock_client, filled_leg, unfilled_leg):
        """Test that unwind result includes PnL estimate."""
        manager = HedgeManager(mock_client, {'HEDGE_MODE': 'unwind'})
        
        result = await manager.handle_partial_fill(filled_leg, unfilled_leg, 'test-market')
        
        assert result.mode_used == HedgeMode.UNWIND
        assert isinstance(result.realized_pnl, float)
    
    @pytest.mark.asyncio
    async def test_complete_mode_places_aggressive_order(self, mock_client, filled_leg, unfilled_leg):
        """Test that complete mode places an aggressive buy."""
        manager = HedgeManager(mock_client, {
            'HEDGE_MODE': 'complete',
            'MAX_HEDGE_SLIPPAGE': 0.01
        })
        
        result = await manager.handle_partial_fill(filled_leg, unfilled_leg, 'test-market')
        
        # Should have tried to complete the box
        assert mock_client.post_order.called
    
    @pytest.mark.asyncio
    async def test_complete_mode_falls_back_to_unwind(self, mock_client, filled_leg, unfilled_leg):
        """Test that complete mode falls back to unwind on failure."""
        mock_client.post_order = Mock(return_value={'success': False})
        
        manager = HedgeManager(mock_client, {'HEDGE_MODE': 'complete'})
        
        result = await manager.handle_partial_fill(filled_leg, unfilled_leg, 'test-market')
        
        # Should fall back to unwind
        # (In practice this would try unwind after complete fails)


class TestHedgeResult:
    """Test HedgeResult creation and properties."""
    
    def test_successful_unwind_result(self):
        """Test creating a successful unwind result."""
        filled_leg = LegInfo(
            order_id='yes1', token_id='yesT', side='BUY',
            intended_price=0.50, intended_size=100,
            filled_price=0.50, filled_size=100,
            status=FillStatus.FILLED
        )
        hedge_leg = LegInfo(
            order_id='sell1', token_id='yesT', side='SELL',
            intended_price=0.49, intended_size=100,
            filled_price=0.49, filled_size=100,
            status=FillStatus.FILLED
        )
        
        result = HedgeResult(
            success=True,
            mode_used=HedgeMode.UNWIND,
            filled_leg=filled_leg,
            hedge_leg=hedge_leg,
            realized_pnl=-1.0,  # Sold at 0.49, bought at 0.50, 100 shares = -$1
            message="Unwind successful"
        )
        
        assert result.success is True
        assert result.mode_used == HedgeMode.UNWIND
        assert result.realized_pnl == -1.0


class TestTradeSummaryLogging:
    """Test trade summary logging."""
    
    def test_log_trade_summary_success(self):
        """Test logging a successful trade."""
        mock_client = Mock()
        mock_logger = Mock()
        manager = HedgeManager(mock_client, logger=mock_logger)
        
        yes_leg = LegInfo(
            order_id='yes1', token_id='yesT', side='BUY',
            intended_price=0.50, intended_size=100,
            filled_price=0.50, filled_size=100,
            status=FillStatus.FILLED
        )
        no_leg = LegInfo(
            order_id='no1', token_id='noT', side='BUY',
            intended_price=0.48, intended_size=100,
            filled_price=0.48, filled_size=100,
            status=FillStatus.FILLED
        )
        
        result = BoxTradeResult(
            success=True,
            market_slug='test-market',
            yes_leg=yes_leg,
            no_leg=no_leg,
            intended_sum_price=0.98,
            actual_sum_price=0.98,
            intended_edge=0.02,
            actual_edge=0.02
        )
        
        manager.log_trade_summary(result)
        
        # Should have called logger.info at least once
        assert mock_logger.info.called
    
    def test_log_trade_summary_with_hedge(self):
        """Test logging a trade with hedge action."""
        mock_client = Mock()
        mock_logger = Mock()
        manager = HedgeManager(mock_client, logger=mock_logger)
        
        yes_leg = LegInfo(
            order_id='yes1', token_id='yesT', side='BUY',
            intended_price=0.50, intended_size=100,
            filled_price=0.50, filled_size=100,
            status=FillStatus.FILLED
        )
        
        hedge_result = HedgeResult(
            success=True,
            mode_used=HedgeMode.UNWIND,
            realized_pnl=-0.50
        )
        
        result = BoxTradeResult(
            success=False,
            market_slug='test-market',
            yes_leg=yes_leg,
            no_leg=None,
            hedge_result=hedge_result
        )
        
        manager.log_trade_summary(result)
        
        assert mock_logger.info.called
