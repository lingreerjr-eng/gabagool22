"""
Unit tests for fee model and edge calculations.
"""
import pytest
from strategies.fee_model import FeeModel


class TestFeeModelBasics:
    """Test basic FeeModel functionality."""
    
    def test_default_values(self):
        """Test default fee model configuration."""
        model = FeeModel()
        assert model.taker_fee_bps == 0.0
        assert model.slippage_bps == 20.0
        assert model.min_edge_buffer_bps == 10.0
    
    def test_custom_values(self):
        """Test custom fee configuration."""
        model = FeeModel(taker_fee_bps=10, slippage_bps=50, min_edge_buffer_bps=20)
        assert model.taker_fee_bps == 10
        assert model.slippage_bps == 50
        assert model.min_edge_buffer_bps == 20
    
    def test_negative_fees_rejected(self):
        """Test that negative fees raise an error."""
        with pytest.raises(ValueError):
            FeeModel(taker_fee_bps=-10)
    
    def test_negative_slippage_rejected(self):
        """Test that negative slippage raises an error."""
        with pytest.raises(ValueError):
            FeeModel(slippage_bps=-20)


class TestTotalCostCalculation:
    """Test total cost calculations."""
    
    def test_zero_fees(self):
        """Test with zero fees."""
        model = FeeModel(taker_fee_bps=0, slippage_bps=0)
        assert model.total_cost_bps == 0
        assert model.total_cost_decimal == 0
    
    def test_taker_fees_doubled(self):
        """Test that taker fees apply to both legs."""
        model = FeeModel(taker_fee_bps=10, slippage_bps=0)
        # 10 bps on YES + 10 bps on NO = 20 bps total
        assert model.total_cost_bps == 20
    
    def test_slippage_doubled(self):
        """Test that slippage applies to both legs."""
        model = FeeModel(taker_fee_bps=0, slippage_bps=25)
        # 25 bps on YES + 25 bps on NO = 50 bps total
        assert model.total_cost_bps == 50
    
    def test_combined_costs(self):
        """Test combined fees and slippage."""
        model = FeeModel(taker_fee_bps=10, slippage_bps=20)
        # (10 * 2) + (20 * 2) = 60 bps
        assert model.total_cost_bps == 60
        assert model.total_cost_decimal == pytest.approx(0.006)
    
    def test_calculate_total_cost_notional(self):
        """Test cost calculation for a notional amount."""
        model = FeeModel(taker_fee_bps=10, slippage_bps=20)  # 60 bps total
        cost = model.calculate_total_cost(1000)
        assert cost == pytest.approx(6.0)  # $1000 * 0.006 = $6


class TestEdgeCalculation:
    """Test edge calculation with various scenarios."""
    
    def test_edge_no_fees(self):
        """Test edge calculation with zero fees."""
        model = FeeModel(taker_fee_bps=0, slippage_bps=0)
        
        # Sum price = 0.98, raw edge = 0.02
        edge = model.calculate_edge(0.98)
        assert edge == pytest.approx(0.02)
    
    def test_edge_with_fees(self):
        """Test edge calculation accounting for fees."""
        model = FeeModel(taker_fee_bps=10, slippage_bps=20)  # 60 bps total
        
        # Sum price = 0.98, raw edge = 0.02
        # Net edge = 0.02 - 0.006 = 0.014
        edge = model.calculate_edge(0.98)
        assert edge == pytest.approx(0.014)
    
    def test_edge_at_parity(self):
        """Test edge when sum price = 1.0 (no profit)."""
        model = FeeModel(taker_fee_bps=0, slippage_bps=20)
        
        edge = model.calculate_edge(1.0)
        assert edge < 0  # Negative edge due to slippage
    
    def test_edge_above_parity(self):
        """Test edge when sum price > 1.0 (guaranteed loss)."""
        model = FeeModel(taker_fee_bps=0, slippage_bps=0)
        
        edge = model.calculate_edge(1.02)
        assert edge == pytest.approx(-0.02)
    
    def test_large_edge_opportunity(self):
        """Test edge with significant spread."""
        model = FeeModel(taker_fee_bps=0, slippage_bps=20)  # 40 bps total
        
        # Sum price = 0.90, raw edge = 0.10 (10%)
        # Net edge = 0.10 - 0.004 = 0.096
        edge = model.calculate_edge(0.90)
        assert edge == pytest.approx(0.096)


class TestProfitabilityCheck:
    """Test profitability determination."""
    
    def test_profitable_trade(self):
        """Test detection of profitable opportunity."""
        model = FeeModel(taker_fee_bps=0, slippage_bps=20, min_edge_buffer_bps=10)
        
        # Need edge > 0.004 + 0.001 = 0.005
        # Sum price 0.98 gives edge 0.02 - 0.004 = 0.016
        assert model.is_profitable(0.98)
    
    def test_unprofitable_trade(self):
        """Test detection of unprofitable opportunity."""
        model = FeeModel(taker_fee_bps=0, slippage_bps=20, min_edge_buffer_bps=10)
        
        # Sum price 0.995 gives edge 0.005 - 0.004 = 0.001
        # Min threshold is 0.005, so not profitable
        assert not model.is_profitable(0.995)
    
    def test_profitable_with_custom_min(self):
        """Test profitability with custom minimum edge."""
        model = FeeModel(taker_fee_bps=0, slippage_bps=0)
        
        # Edge = 0.01, custom min = 0.005
        assert model.is_profitable(0.99, min_edge=0.005)
        
        # Edge = 0.01, custom min = 0.02
        assert not model.is_profitable(0.99, min_edge=0.02)
    
    def test_min_profitable_edge(self):
        """Test calculation of minimum profitable edge."""
        model = FeeModel(taker_fee_bps=10, slippage_bps=20, min_edge_buffer_bps=10)
        
        # Total cost = 60 bps, buffer = 10 bps
        # Min edge = 70 bps = 0.007
        min_edge = model.calculate_min_profitable_edge()
        assert min_edge == pytest.approx(0.007)


class TestPnLEstimation:
    """Test PnL estimation for box trades."""
    
    def test_pnl_estimate_positive(self):
        """Test PnL estimation for profitable trade."""
        model = FeeModel(taker_fee_bps=0, slippage_bps=0)
        
        # Sum price = 0.98, edge = 0.02
        # 100 share pairs = $2 profit
        pnl = model.estimate_pnl(0.98, 100)
        assert pnl == pytest.approx(2.0)
    
    def test_pnl_estimate_with_fees(self):
        """Test PnL estimation including fees."""
        model = FeeModel(taker_fee_bps=10, slippage_bps=20)  # 60 bps total
        
        # Sum price = 0.98, raw edge = 0.02, net edge = 0.014
        # 100 share pairs = $1.40 profit
        pnl = model.estimate_pnl(0.98, 100)
        assert pnl == pytest.approx(1.4)
    
    def test_pnl_estimate_negative(self):
        """Test PnL estimation for losing trade."""
        model = FeeModel(taker_fee_bps=0, slippage_bps=20)
        
        # Sum price = 1.01, edge = -0.01 - 0.004 = -0.014
        pnl = model.estimate_pnl(1.01, 100)
        assert pnl < 0
    
    def test_pnl_scales_with_size(self):
        """Test that PnL scales linearly with size."""
        model = FeeModel(taker_fee_bps=0, slippage_bps=0)
        
        pnl_10 = model.estimate_pnl(0.98, 10)
        pnl_100 = model.estimate_pnl(0.98, 100)
        
        assert pnl_100 == pytest.approx(pnl_10 * 10)


class TestRepr:
    """Test string representation."""
    
    def test_repr(self):
        """Test FeeModel string representation."""
        model = FeeModel(taker_fee_bps=10, slippage_bps=20)
        repr_str = repr(model)
        
        assert 'taker=10' in repr_str
        assert 'slippage=20' in repr_str
        assert 'total_cost=60' in repr_str


# Edge case tests
class TestEdgeCases:
    """Test edge cases and boundary conditions."""
    
    def test_zero_sum_price(self):
        """Test with sum price of 0 (impossible but test boundary)."""
        model = FeeModel()
        edge = model.calculate_edge(0.0)
        assert edge > 0  # Edge would be 1.0 minus costs
    
    def test_very_small_edge(self):
        """Test with very small edge opportunity."""
        model = FeeModel(taker_fee_bps=0, slippage_bps=5)
        edge = model.calculate_edge(0.998)
        # Raw edge = 0.002, cost = 0.001
        # Net edge = 0.001
        assert edge == pytest.approx(0.001)
    
    def test_large_fees(self):
        """Test with unusually large fees."""
        model = FeeModel(taker_fee_bps=100, slippage_bps=100)
        # Total cost = 4%
        assert model.total_cost_decimal == pytest.approx(0.04)
