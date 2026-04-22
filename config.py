"""
SoDEX BTC-USD Futures Bot Configuration
全設定を集約 - カスタマイズ版
"""
import os
from pathlib import Path

# ========================================
# 重要: Paper Mode 設定
# ========================================
# False にすると実注文が発行されます！
PAPER_MODE = True  # デフォルトは安全のため True

# ========================================
# 市場設定
# ========================================
MARKET_SYMBOL = "BTC-USD"
BASE_SIZE = 0.002  # BTC単位でのポジションサイズ
LEVERAGE = 10  # レバレッジ倍率

# ========================================
# テクニカル指標設定（カスタマイズ版）
# ========================================
# トレンド判定
EMA_PERIOD = 20  # 長期的な波に逆らわない順張り

# 勢い判定
ADX_PERIOD = 14
ADX_THRESHOLD = 35  # 30と40の中間、取引頻度を保ちつつ迷い相場を回避

# 過熱感/押し目判定
RSI_PERIOD = 14
RSI_LOWER = 40  # 広めのレンジで約定チャンス確保
RSI_UPPER = 60

# ========================================
# エントリー戦略
# ========================================
ENTRY_PULLBACK_PCT = 0.002  # 0.2% - 押し目/戻りのエントリー閾値
LOOKBACK_PERIODS = 20  # 直近高値/安値の判定期間

# ========================================
# エグジット戦略（カスタマイズ版）
# ========================================
TAKE_PROFIT_PCT = 0.06  # 6.0% - レバ10倍なら現物0.6%の動きで決済
STOP_LOSS_PCT = 0.05    # 5.0% - 損切りを利益より少し狭く

# トレーリングストップ
TRAILING_STOP_ACTIVATION = 0.015  # +1.5%で発動
TRAILING_STOP_DISTANCE = 0.005    # +0.5%にロック（手数料負けなし）

# DCA（ナンピン）
DCA_ENABLED = True
DCA_TRIGGER_PCT = 0.01  # -1.0%で追加ポジション
DCA_MAX_ADDITIONS = 1   # 1回のみ

# ========================================
# リスク管理
# ========================================
MAX_POSITIONS = 1  # 同時保有は1ポジションのみ
MIN_PROFIT_CLOSE = 0.001  # 最小利益確定閾値（手数料考慮）

# ========================================
# SoDEX ネットワーク設定
# ========================================
# 注: SoDEXは現在テストネットフェーズ
# 正式API公開後にURLを更新してください
SODEX_API_URL = os.getenv("SODEX_API_URL", "https://api.sodex.com/v1")
SODEX_CHAIN_ID = 1  # ValueChain L1
SODEX_PRIVATE_KEY = os.getenv("SODEX_PRIVATE_KEY", "")

# Coinbase API（フォールバック用）
COINBASE_API_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"

# ========================================
# ボット動作設定
# ========================================
LOOP_INTERVAL = 5  # 秒単位でのメインループ間隔
PRICE_PRECISION = 2  # 価格の小数点以下桁数
SIZE_PRECISION = 6   # サイズの小数点以下桁数

# ========================================
# ファイルパス
# ========================================
BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "bot_state.json"
TRADE_HISTORY_FILE = BASE_DIR / "trade_history.csv"
LOG_FILE = BASE_DIR / "bot.log"

# ========================================
# ログ設定
# ========================================
LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# ========================================
# 検証用設定
# ========================================
ENABLE_BACKTESTING = False
BACKTEST_START_DATE = "2024-01-01"
BACKTEST_END_DATE = "2024-12-31"
