import sys
import os
from dotenv import load_dotenv

# Ensure we can import from parent directory
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.market_data import MarketTool
from utils.logger import setup_logger

logger = setup_logger("TestExchange")

def test_connectivity(config_id=None, symbol=None, exchange_name="binance"):
    logger.info(f"🚀 Testing connectivity for {exchange_name}...")
    try:
        # If we have a config_id, use it. Otherwise, we might need to mock a config.
        # For a simple test, we'll try to initialize MarketTool.
        # MarketTool reads from global_config.
        
        tool = MarketTool(config_id=config_id, symbol=symbol)
        
        logger.info(f"✅ MarketTool initialized for {tool.exchange.id}")
        
        # Test 1: Fetch Balance
        logger.info("Fetching balance...")
        balance = tool.exchange.fetch_balance()
        total_usdt = balance.get('total', {}).get('USDT', 0)
        logger.info(f"✅ Balance fetched. Total USDT: {total_usdt}")
        
        # Test 2: Fetch Ticker
        logger.info(f"Fetching ticker for {tool.symbol}...")
        ticker = tool.exchange.fetch_ticker(tool.symbol)
        last_price = ticker.get('last')
        logger.info(f"✅ Ticker fetched. Last price: {last_price}")
        
        # Test 3: Fetch Open Orders
        logger.info(f"Fetching open orders for {tool.symbol}...")
        orders = tool.exchange.fetch_open_orders(tool.symbol)
        logger.info(f"✅ Open orders fetched: {len(orders)} orders found.")
        
        logger.info("🎉 All basic connectivity tests passed!")
        
    except Exception as e:
        logger.error(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    load_dotenv()
    
    # You can pass a config_id that exists in your .env SYMBOL_CONFIGS
    # Or just a symbol if you have global API keys set.
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--config_id", help="Config ID to test")
    parser.add_argument("--symbol", help="Symbol to test", default="BTC/USDT")
    args = parser.parse_args()
    
    test_connectivity(config_id=args.config_id, symbol=args.symbol)
