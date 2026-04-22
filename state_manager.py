"""
State Manager - ポジションと取引履歴の管理
Nado V6.0と同じ構造で実装
"""
import json
import csv
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional, List
from pathlib import Path
import config


@dataclass
class Position:
    """ポジション情報"""
    side: str  # "LONG" or "SHORT"
    entry_price: float
    size: float
    entry_time: str
    stop_loss: float
    take_profit: float
    trailing_stop: Optional[float] = None
    dca_count: int = 0
    highest_price: Optional[float] = None  # LONG用
    lowest_price: Optional[float] = None   # SHORT用
    
    def calculate_pnl(self, current_price: float) -> float:
        """未実現PnLを計算（レバレッジ考慮）"""
        if self.side == "LONG":
            pnl_pct = (current_price - self.entry_price) / self.entry_price
        else:  # SHORT
            pnl_pct = (self.entry_price - current_price) / self.entry_price
        
        return pnl_pct * config.LEVERAGE
    
    def update_trailing_stop(self, current_price: float) -> bool:
        """トレーリングストップの更新"""
        pnl_pct = self.calculate_pnl(current_price)
        
        # トレーリングストップ発動条件
        if pnl_pct >= config.TRAILING_STOP_ACTIVATION:
            if self.side == "LONG":
                # 最高価格を更新
                if self.highest_price is None or current_price > self.highest_price:
                    self.highest_price = current_price
                    self.trailing_stop = current_price * (1 - config.TRAILING_STOP_DISTANCE)
                    return True
            else:  # SHORT
                # 最低価格を更新
                if self.lowest_price is None or current_price < self.lowest_price:
                    self.lowest_price = current_price
                    self.trailing_stop = current_price * (1 + config.TRAILING_STOP_DISTANCE)
                    return True
        
        return False
    
    def should_close(self, current_price: float) -> tuple[bool, str]:
        """決済すべきかチェック"""
        # 利益確定チェック
        if self.side == "LONG":
            if current_price >= self.take_profit:
                return True, "TAKE_PROFIT"
            if current_price <= self.stop_loss:
                return True, "STOP_LOSS"
            if self.trailing_stop and current_price <= self.trailing_stop:
                return True, "TRAILING_STOP"
        else:  # SHORT
            if current_price <= self.take_profit:
                return True, "TAKE_PROFIT"
            if current_price >= self.stop_loss:
                return True, "STOP_LOSS"
            if self.trailing_stop and current_price >= self.trailing_stop:
                return True, "TRAILING_STOP"
        
        return False, ""


class StateManager:
    """ボット状態の永続化管理"""
    
    def __init__(self):
        self.state_file = config.STATE_FILE
        self.trade_history_file = config.TRADE_HISTORY_FILE
        self.current_position: Optional[Position] = None
        self._load_state()
        self._init_trade_history()
    
    def _load_state(self):
        """状態ファイルから読み込み"""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                    if data.get('position'):
                        self.current_position = Position(**data['position'])
            except Exception as e:
                print(f"状態ファイル読み込みエラー: {e}")
    
    def _save_state(self):
        """状態をファイルに保存"""
        data = {
            'position': asdict(self.current_position) if self.current_position else None,
            'last_updated': datetime.now().isoformat()
        }
        
        try:
            with open(self.state_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"状態ファイル保存エラー: {e}")
    
    def _init_trade_history(self):
        """取引履歴CSVの初期化"""
        if not self.trade_history_file.exists():
            with open(self.trade_history_file, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'side', 'action', 'entry_price', 'exit_price',
                    'size', 'pnl_pct', 'pnl_usd', 'reason', 'dca_count'
                ])
    
    def open_position(self, side: str, entry_price: float, size: float) -> Position:
        """新規ポジションを開く"""
        if self.current_position:
            raise ValueError("既にポジションが存在します")
        
        # ストップロスとテイクプロフィットを計算
        if side == "LONG":
            stop_loss = entry_price * (1 - config.STOP_LOSS_PCT)
            take_profit = entry_price * (1 + config.TAKE_PROFIT_PCT)
        else:  # SHORT
            stop_loss = entry_price * (1 + config.STOP_LOSS_PCT)
            take_profit = entry_price * (1 - config.TAKE_PROFIT_PCT)
        
        self.current_position = Position(
            side=side,
            entry_price=entry_price,
            size=size,
            entry_time=datetime.now().isoformat(),
            stop_loss=stop_loss,
            take_profit=take_profit
        )
        
        self._save_state()
        return self.current_position
    
    def close_position(self, exit_price: float, reason: str) -> dict:
        """ポジションを閉じる"""
        if not self.current_position:
            raise ValueError("閉じるポジションがありません")
        
        # PnL計算
        pnl_pct = self.current_position.calculate_pnl(exit_price)
        pnl_usd = pnl_pct * self.current_position.size * exit_price
        
        # 取引履歴に記録
        trade_record = {
            'timestamp': datetime.now().isoformat(),
            'side': self.current_position.side,
            'action': 'CLOSE',
            'entry_price': self.current_position.entry_price,
            'exit_price': exit_price,
            'size': self.current_position.size,
            'pnl_pct': round(pnl_pct * 100, 2),
            'pnl_usd': round(pnl_usd, 2),
            'reason': reason,
            'dca_count': self.current_position.dca_count
        }
        
        self._record_trade(trade_record)
        
        # ポジションをクリア
        self.current_position = None
        self._save_state()
        
        return trade_record
    
    def add_to_position(self, price: float, size: float):
        """DCA - ポジションに追加"""
        if not self.current_position:
            raise ValueError("追加するポジションがありません")
        
        # 平均エントリー価格を再計算
        total_size = self.current_position.size + size
        avg_price = (
            (self.current_position.entry_price * self.current_position.size) +
            (price * size)
        ) / total_size
        
        self.current_position.entry_price = avg_price
        self.current_position.size = total_size
        self.current_position.dca_count += 1
        
        # ストップロスとテイクプロフィットを再計算
        if self.current_position.side == "LONG":
            self.current_position.stop_loss = avg_price * (1 - config.STOP_LOSS_PCT)
            self.current_position.take_profit = avg_price * (1 + config.TAKE_PROFIT_PCT)
        else:
            self.current_position.stop_loss = avg_price * (1 + config.STOP_LOSS_PCT)
            self.current_position.take_profit = avg_price * (1 - config.TAKE_PROFIT_PCT)
        
        self._save_state()
    
    def _record_trade(self, trade: dict):
        """取引履歴をCSVに記録"""
        try:
            with open(self.trade_history_file, 'a', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=trade.keys())
                writer.writerow(trade)
        except Exception as e:
            print(f"取引履歴記録エラー: {e}")
    
    def get_trade_stats(self) -> dict:
        """取引統計を取得"""
        if not self.trade_history_file.exists():
            return {}
        
        trades = []
        with open(self.trade_history_file, 'r') as f:
            reader = csv.DictReader(f)
            trades = list(reader)
        
        if not trades:
            return {}
        
        total_trades = len(trades)
        winning_trades = sum(1 for t in trades if float(t['pnl_pct']) > 0)
        total_pnl = sum(float(t['pnl_usd']) for t in trades)
        
        return {
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'win_rate': round(winning_trades / total_trades * 100, 2) if total_trades > 0 else 0,
            'total_pnl_usd': round(total_pnl, 2),
            'avg_pnl_per_trade': round(total_pnl / total_trades, 2) if total_trades > 0 else 0
        }
