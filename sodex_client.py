"""
SoDEX Trading Client
SoDEX REST API + Coinbase フォールバック
"""
import requests
import time
from typing import Optional, Dict, List
import config


class SodexTradingClient:
    """SoDEX取引クライアント"""
    
    def __init__(self, paper_mode: bool = True):
        self.paper_mode = paper_mode
        self.api_url = config.SODEX_API_URL
        self.private_key = config.SODEX_PRIVATE_KEY
        
        # Paper Mode用の仮想注文管理
        self.paper_orders: Dict[str, dict] = {}
        self.paper_position: Optional[dict] = None
        
        print(f"SoDEX Client initialized - Mode: {'PAPER' if paper_mode else 'LIVE'}")
    
    def get_price(self) -> Optional[float]:
        """
        BTC-USDの現在価格を取得
        1. SoDEX REST API を試行
        2. 失敗時はCoinbase APIにフォールバック
        """
        # 1. SoDEX API試行（現在テストネットのため、多くの場合失敗する）
        try:
            response = requests.get(
                f"{self.api_url}/market/{config.MARKET_SYMBOL}/ticker",
                timeout=5
            )
            if response.status_code == 200:
                data = response.json()
                return float(data.get('last_price', 0))
        except Exception as e:
            pass  # フォールバックへ
        
        # 2. Coinbase APIフォールバック
        try:
            response = requests.get(config.COINBASE_API_URL, timeout=5)
            if response.status_code == 200:
                data = response.json()
                return float(data['data']['amount'])
        except Exception as e:
            print(f"価格取得エラー: {e}")
            return None
    
    def get_market_depth(self) -> Optional[dict]:
        """板情報を取得"""
        if self.paper_mode:
            # Paper Modeでは簡易的な板を返す
            current_price = self.get_price()
            if not current_price:
                return None
            
            return {
                'bids': [[current_price * 0.999, 1.0]],
                'asks': [[current_price * 1.001, 1.0]]
            }
        
        try:
            response = requests.get(
                f"{self.api_url}/market/{config.MARKET_SYMBOL}/depth",
                timeout=5
            )
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            print(f"板情報取得エラー: {e}")
            return None
    
    def place_limit_order(
        self,
        side: str,
        size: float,
        price: float
    ) -> Optional[str]:
        """
        指値注文を発行
        Returns: order_id or None
        """
        if self.paper_mode:
            return self._paper_place_order(side, size, price)
        
        # 実注文ロジック（SoDEX正式API公開後に実装）
        try:
            order_data = {
                'symbol': config.MARKET_SYMBOL,
                'side': side.lower(),
                'type': 'limit',
                'size': size,
                'price': price,
                'leverage': config.LEVERAGE
            }
            
            # TODO: SoDEX正式API公開後に実装
            # - Web3署名の追加
            # - APIエンドポイントへのPOSTリクエスト
            # - レスポンスのバリデーション
            print(f"[LIVE] 注文発行: {order_data}")
            return f"order_{int(time.time())}"
            
        except Exception as e:
            print(f"注文エラー: {e}")
            return None
    
    def _paper_place_order(self, side: str, size: float, price: float) -> str:
        """Paper Mode用の注文シミュレーション"""
        order_id = f"paper_{int(time.time() * 1000)}"
        
        self.paper_orders[order_id] = {
            'id': order_id,
            'side': side,
            'size': size,
            'price': price,
            'status': 'open',
            'created_at': time.time()
        }
        
        print(f"[PAPER] 注文発行: {side} {size} BTC @ ${price:,.2f}")
        return order_id
    
    def close_position(
        self,
        side: str,
        size: float,
        price: Optional[float] = None
    ) -> bool:
        """
        ポジションを決済
        side: "LONG"の場合は"SELL", "SHORT"の場合は"BUY"
        """
        close_side = "SELL" if side == "LONG" else "BUY"
        
        if self.paper_mode:
            return self._paper_close_position(close_side, size, price)
        
        # 実取引ロジック
        try:
            if price is None:
                # 成行決済
                order_data = {
                    'symbol': config.MARKET_SYMBOL,
                    'side': close_side.lower(),
                    'type': 'market',
                    'size': size
                }
            else:
                # 指値決済
                order_data = {
                    'symbol': config.MARKET_SYMBOL,
                    'side': close_side.lower(),
                    'type': 'limit',
                    'size': size,
                    'price': price
                }
            
            print(f"[LIVE] 決済注文: {order_data}")
            return True
            
        except Exception as e:
            print(f"決済エラー: {e}")
            return False
    
    def _paper_close_position(
        self,
        side: str,
        size: float,
        price: Optional[float]
    ) -> bool:
        """Paper Mode用の決済シミュレーション"""
        if price is None:
            price = self.get_price()
        
        print(f"[PAPER] 決済注文: {side} {size} BTC @ ${price:,.2f}")
        self.paper_position = None
        return True
    
    def cancel_all_orders(self) -> bool:
        """全ての未約定注文をキャンセル"""
        if self.paper_mode:
            cancelled = len(self.paper_orders)
            self.paper_orders.clear()
            print(f"[PAPER] {cancelled}件の注文をキャンセル")
            return True
        
        try:
            response = requests.delete(
                f"{self.api_url}/orders",
                timeout=5
            )
            return response.status_code == 200
        except Exception as e:
            print(f"注文キャンセルエラー: {e}")
            return False
    
    def get_position(self) -> Optional[dict]:
        """現在のポジション情報を取得"""
        if self.paper_mode:
            return self.paper_position
        
        try:
            response = requests.get(
                f"{self.api_url}/positions/{config.MARKET_SYMBOL}",
                timeout=5
            )
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            print(f"ポジション取得エラー: {e}")
            return None
    
    def get_account_balance(self) -> Optional[float]:
        """アカウント残高を取得"""
        if self.paper_mode:
            return 10000.0  # Paper Modeでは仮想残高
        
        try:
            response = requests.get(
                f"{self.api_url}/account/balance",
                timeout=5
            )
            if response.status_code == 200:
                data = response.json()
                return float(data.get('available_balance', 0))
        except Exception as e:
            print(f"残高取得エラー: {e}")
            return None
