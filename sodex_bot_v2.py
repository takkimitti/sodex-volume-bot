import requests
import time
import json
import uuid
import threading
import websocket
import smtplib
import csv
import math
import logging
import pytz
import os
from datetime import datetime, timezone, timedelta, time as dtime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from eth_account import Account
import pandas as pd
from ta.trend import EMAIndicator, ADXIndicator
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands, AverageTrueRange
from web3 import Web3
from collections import OrderedDict
from dotenv import load_dotenv                               # ← ★これを追加

load_dotenv("/home/thankyou_yukiko/sodex-bot/.env")          # ← ★これを追加

# ==========================================================
# ログ設定
# ==========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("/home/thankyou_yukiko/sodex-bot/bot_v2.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("SoDEX_v2.1")

# ==========================================================
# SoDEX Schema Enums
# ==========================================================
ORDER_SIDE_BUY = 1
ORDER_SIDE_SELL = 2
ORDER_TYPE_LIMIT = 1
MODIFIER_NORMAL = 1
POSITION_SIDE_BOTH = 1
TIF_IOC = 3  # Immediate Or Cancel

# ==========================================================
# 設定エリア (v2.1 Pro Config)
# ==========================================================
CONFIG = {
    # --- 基本設定 ---
    "symbol": "BTC-USD",
    "symbol_id": 1,
    "leverage": 10,

    # --- リスクベースポジションサイズ ---
    "risk_per_trade": 0.01,       # 1トレードあたりの最大リスク (残高の1%)
    "min_size": 0.001,            # 最小注文サイズ (BTC)
    "max_size": 0.01,             # 最大注文サイズ (BTC)
    "fallback_size": "0.002",     # API残高取得失敗時のフォールバック

    # --- テクニカル指標パラメータ ---
    "kline_interval": "5m",
    "rsi_period": 14,
    "adx_period": 14,
    "ema_period": 100,
    "bb_period": 20,
    "bb_std_dev": 2,
    "atr_period": 14,
    "volume_sma_period": 20,

    # --- エントリー条件 (押し目/戻り型) ---
    "trend_rsi_buy_max": 45,      # TREND: BUY時 RSI上限 (押し目)
    "trend_rsi_sell_min": 55,     # TREND: SELL時 RSI下限 (戻り)
    "trend_adx_min": 25,          # TREND: ADX最低値 (緩和して初動も拾う)
    "scalp_rsi_buy_max": 30,      # SCALP: BUY時 RSI上限 (逆張り)
    "scalp_rsi_sell_min": 70,     # SCALP: SELL時 RSI下限 (逆張り)

    # --- SecureTrigger ATR連動型 ---
    "secure_trigger_atr_mult": 1.2,  # ATR x 1.2 で保護ON
    "secure_floor_atr_mult": 0.6,    # ATR x 0.6 で利益確保撤退
    "take_profit_atr_mult": 3.0,     # ATR x 3.0 で利確
    "stop_loss_atr_mult": 2.0,       # ATR x 2.0 で損切

    # --- DVOL戦略切替 ---
    "dvol_threshold": 75,
    "dvol_extreme": 90,
    "dvol_api_url": "https://www.deribit.com/api/v2/public/get_index_price?index_name=btcdvol_usdc",

    # --- IOC注文 板情報ベース ---
    "orderbook_slippage": 0.5,       # best_ask/bid からの許容幅 ($)

    # --- 時間フィルター (UTC) ---
    "avoid_hours_start": 0,
    "avoid_hours_end": 6,

    # --- 出来高フィルター ---
    "volume_filter_mult": 0.8,

    # --- Funding Rate ---
    "funding_rate_threshold": 0.01,

    # --- OI (Open Interest) ---
    "oi_change_threshold": 0.03,

    # --- 板偏り ---
    "book_imbalance_threshold": 0.6,

    # --- リスク管理 ---
    "max_loss_per_session": 15.0,
    "cooldown_minutes": 10,
    "lockout_minutes": 90,

    # --- リトライ決済 ---
    "force_close_retries": 3,
    "force_close_slippages": [0.01, 0.03, 0.05],  # 段階的スリッページ
    "force_close_confirm_wait": 2,    # 約定確認間隔 (秒)
    "force_close_confirm_checks": 5,  # 約定確認回数

    # --- マクロ指標フィルター ---
    "macro_filter_enabled": True,
    "macro_api_url": "https://openapi.sosovalue.com/openapi/v1/macro/events",
    "macro_fetch_interval": 86400,    # 1日1回取得

    # --- 通知 ---
    "gmail_user": os.getenv("GMAIL_USER", ""),
    "gmail_pass": os.getenv("GMAIL_APP_PASSWORD", ""),
}

SESSION_LOG_FILE = "session_log.json"
TRADE_LOG_FILE = "trade_log.csv"

# ==========================================================
# マクロ指標設定 (CPI/FOMC/NFP等)
# ==========================================================
MACRO_CONFIG = {
    "CPI": {"time": dtime(8, 30), "shield_min": 90, "aliases": ["cpi", "inflation"]},
    "Nonfarm Payrolls": {"time": dtime(8, 30), "shield_min": 120, "aliases": ["nonfarm", "payroll", "nfp"]},
    "FOMC": {"time": dtime(14, 0), "shield_min": 180, "aliases": ["fomc", "fed rate", "interest rate"]},
    "GDP": {"time": dtime(8, 30), "shield_min": 60, "aliases": ["gdp", "gross domestic"]},
    "PCE": {"time": dtime(8, 30), "shield_min": 60, "aliases": ["pce", "personal consumption"]},
}

# ==========================================================
# トレードログ (CSV)
# ==========================================================
def init_trade_log():
    if not os.path.exists(TRADE_LOG_FILE):
        with open(TRADE_LOG_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "mode", "side", "entry_price", "exit_price",
                "size_btc", "pnl_usd", "pnl_roe_pct", "atr", "dvol",
                "rsi", "adx", "reason",
            ])

def append_trade_log(row: dict):
    with open(TRADE_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            row.get("timestamp", datetime.now(timezone.utc).isoformat()),
            row.get("mode", ""), row.get("side", ""),
            row.get("entry_price", ""), row.get("exit_price", ""),
            row.get("size_btc", ""), row.get("pnl_usd", ""),
            row.get("pnl_roe_pct", ""), row.get("atr", ""),
            row.get("dvol", ""), row.get("rsi", ""),
            row.get("adx", ""), row.get("reason", ""),
        ])

# ==========================================================
# テクニカル指標計算
# ==========================================================
def compute_indicators(df: pd.DataFrame, cfg: dict) -> dict | None:
    try:
        close = df["c"]
        high = df["h"]
        low = df["l"]

        ema_series = EMAIndicator(close=close, window=cfg["ema_period"]).ema_indicator()
        rsi_series = RSIIndicator(close=close, window=cfg["rsi_period"]).rsi()
        adx_series = ADXIndicator(high=high, low=low, close=close, window=cfg["adx_period"]).adx()
        
        bb_ind = BollingerBands(close=close, window=cfg["bb_period"], window_dev=cfg["bb_std_dev"])
        bbu_series = bb_ind.bollinger_hband()
        bbl_series = bb_ind.bollinger_lband()
        
        atr_series = AverageTrueRange(high=high, low=low, close=close, window=cfg["atr_period"]).average_true_range()
        vol_sma = df["v"].rolling(window=cfg["volume_sma_period"]).mean()

        last_idx = len(df) - 1
        result = {
            "ema": ema_series.iloc[last_idx],
            "rsi": rsi_series.iloc[last_idx],
            "adx": adx_series.iloc[last_idx],
            "atr": atr_series.iloc[last_idx],
            "bb_upper": bbu_series.iloc[last_idx],
            "bb_lower": bbl_series.iloc[last_idx],
            "volume": df["v"].iloc[last_idx],
            "vol_sma": vol_sma.iloc[last_idx] if not pd.isna(vol_sma.iloc[last_idx]) else 0,
        }

        for key in ["ema", "rsi", "adx", "atr", "bb_upper", "bb_lower"]:
            val = result[key]
            if val is None or (isinstance(val, float) and math.isnan(val)):
                return None
        return result
    except Exception as e:
        logger.error(f"指標計算エラー: {e}")
        return None

# ==========================================================
# メインBotクラス
# ==========================================================
class SodexAdvancedBotV2:
    def __init__(self, api_key: str, private_key_hex: str, account_id: int,
                 wallet_address: str = None, is_testnet: bool = False):
        self.api_key = api_key
        self.account_id = account_id
        self.private_key = private_key_hex
        self.account = Account.from_key(private_key_hex)
        self.wallet_address = wallet_address or self.account.address

        if is_testnet:
            self.rest_url = "https://testnet-gw.sodex.dev/api/v1/perps"
            self.ws_url = "wss://testnet-gw.sodex.dev/ws/perps"
            self.chain_id = 286623
        else:
            self.rest_url = "https://mainnet-gw.sodex.dev/api/v1/perps"
            self.ws_url = "wss://mainnet-gw.sodex.dev/ws/perps"
            self.chain_id = 286623

        # --- 状態管理 ---
        self.latest_btc_price = None
        self.last_order_time = 0
        self.last_close_time = 0
        self.is_profit_secured = False
        self.last_entry_price = None
        self.position_side = None
        self.current_size = float(CONFIG["fallback_size"])
        self.current_mode = "TREND"
        self.synced_position = None 

        # --- 指標キャッシュ ---
        self.current_atr = None
        self.current_indicators = None
        self.last_indicator_fetch = 0
        self.current_dvol = 50.0
        self.last_dvol_fetch = 0

        # --- 板情報 ---
        self.best_bid = None
        self.best_ask = None
        self.current_funding_rate = 0.0
        self.last_funding_fetch = 0
        self.current_oi = None
        self.previous_oi = None
        self.last_oi_fetch = 0

        # --- ロックアウト ---
        self.consecutive_loss = {"BUY": 0, "SELL": 0}
        self.lockout_time = {"BUY": 0, "SELL": 0}
        self.session_data = self._load_session_data()

        # --- エントリー時情報 (ログ用) ---
        self.entry_mode = None
        self.entry_atr = None
        self.entry_dvol = None
        self.entry_rsi = None
        self.entry_adx = None

        # --- マクロ指標フィルター ---
        self.ET = pytz.timezone("US/Eastern")
        self.UTC = pytz.utc
        self.macro_events = {}
        self.last_macro_fetch = 0

        # --- WebSocket ---
        self._ws = None
        self._ws_lock = threading.Lock()

        init_trade_log()

        logger.info("=" * 58)
        logger.info("  SoDEX Bot v2.1 - Fortress Edition (Final)")
        logger.info(f"  ネットワーク: {'TESTNET' if is_testnet else 'MAINNET'}")
        logger.info(f"  ウォレット: {self.wallet_address}")
        logger.info("=" * 58)

    # ==========================================================
    # 通知 (HTML対応)
    # ==========================================================
    def _send_email(self, subject: str, text: str, is_html: bool = False):
        if not CONFIG["gmail_user"] or not CONFIG["gmail_pass"]:
            return
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = CONFIG["gmail_user"]
            msg["To"] = CONFIG["gmail_user"]
            msg.attach(MIMEText(text, "html" if is_html else "plain", "utf-8"))
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(CONFIG["gmail_user"], CONFIG["gmail_pass"])
                server.send_message(msg)
        except Exception as e:
            logger.warning(f"メール送信エラー: {e}")

    def _send_settlement_report(self, pos_info: dict, exit_price: float, reason: str):
        side = pos_info["side"]
        entry = pos_info["entry"]
        size = pos_info["size"]
        pnl_pct = (exit_price - entry) / entry if side == "BUY" else (entry - exit_price) / entry
        roe = pnl_pct * CONFIG["leverage"] * 100
        pnl_usdt = pnl_pct * exit_price * size

        color = "#2ecc71" if roe > 0 else "#e74c3c"
        subject = f"【SoDEX v2.1】{'利確' if roe > 0 else '損切'}完了 ({side})"
        html = f"""
        <div style="font-family: sans-serif; padding: 20px; border: 1px solid #eee;">
            <h2 style="color: {color}; border-bottom: 2px solid;">{reason} 実行報告</h2>
            <table style="width: 100%; border-collapse: collapse;">
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee;"><b>方向 / サイズ</b></td>
                    <td style="text-align: right;">{side} ({size} BTC)</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee;"><b>エントリー建値</b></td>
                    <td style="text-align: right;">${int(entry)}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee;"><b>決済(市場)価格</b></td>
                    <td style="text-align: right;">${int(exit_price)}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-size: 1.2em;"><b>損益 (ROE)</b></td>
                    <td style="text-align: right; color: {color}; font-size: 1.2em;">
                    <b>{roe:+.2f}% (${pnl_usdt:+.4f})</b></td></tr>
            </table>
        </div>
        """
        self._send_email(subject, html, is_html=True)

    # ==========================================================
    # セッション管理
    # ==========================================================
    def _get_session_id(self) -> str:
        h = time.localtime().tm_hour
        d = time.strftime("%Y-%m-%d")
        if 6 <= h < 18: return f"{d}_DAY"
        if h < 6: return f"{d}_EARLY_MORNING"
        return f"{d}_NIGHT"

    def _load_session_data(self) -> dict:
        if os.path.exists(SESSION_LOG_FILE):
            try:
                with open(SESSION_LOG_FILE, "r") as f:
                    return json.load(f)
            except Exception: pass
        return {"currentSession": self._get_session_id(), "totalLoss": 0, "status": "ACTIVE"}

    def _save_session_data(self):
        with open(SESSION_LOG_FILE, "w") as f:
            json.dump(self.session_data, f, indent=2)

    def _check_session(self):
        now_id = self._get_session_id()
        if self.session_data.get("currentSession") != now_id:
            self.session_data = {"currentSession": now_id, "totalLoss": 0, "status": "ACTIVE"}
            self._save_session_data()
            self.consecutive_loss = {"BUY": 0, "SELL": 0}
            self.lockout_time = {"BUY": 0, "SELL": 0}
            logger.info(f"新セッション開始: {now_id}")

    def _record_pnl(self, side: str, pnl_usd: float, exit_price: float, reason: str):
        net_pnl = pnl_usd - 0.08  
        pnl_roe = 0.0
        if self.last_entry_price and self.last_entry_price > 0:
            pnl_raw = (exit_price - self.last_entry_price) / self.last_entry_price
            if side == "SELL": pnl_raw = -pnl_raw
            pnl_roe = pnl_raw * 100 * CONFIG["leverage"]

        append_trade_log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": self.entry_mode or self.current_mode, "side": side,
            "entry_price": self.last_entry_price, "exit_price": exit_price,
            "size_btc": self.current_size, "pnl_usd": round(net_pnl, 4),
            "pnl_roe_pct": round(pnl_roe, 2),
            "atr": round(self.entry_atr, 2) if self.entry_atr else "",
            "dvol": round(self.entry_dvol, 2) if self.entry_dvol else "",
            "rsi": round(self.entry_rsi, 1) if self.entry_rsi else "",
            "adx": round(self.entry_adx, 1) if self.entry_adx else "",
            "reason": reason,
        })

        if net_pnl < 0:
            self.session_data["totalLoss"] += abs(net_pnl)
            self.consecutive_loss[side] += 1
            if self.consecutive_loss[side] >= 2:
                self.lockout_time[side] = time.time() + (CONFIG["lockout_minutes"] * 60)
                self.consecutive_loss[side] = 0
                logger.warning(f"LOCKOUT: {side}方向 {CONFIG['lockout_minutes']}分間停止")

            if self.session_data["totalLoss"] >= CONFIG["max_loss_per_session"]:
                self.session_data["status"] = "STOPPED"
                self._send_email("【SoDEX v2.1】セッション最大損失超過", f"累計損失: ${self.session_data['totalLoss']:.2f}\nBot停止")
                logger.critical("SESSION STOPPED: 最大損失超過")
            self._save_session_data()
        else:
            self.consecutive_loss[side] = 0

    def _is_active_hours(self) -> bool:
        utc_hour = datetime.now(timezone.utc).hour
        if CONFIG["avoid_hours_start"] <= utc_hour < CONFIG["avoid_hours_end"]:
            return False
        return True

    # ==========================================================
    # ポジション同期 (API実態が唯一の真実)
    # ==========================================================
    def _sync_position(self) -> dict | None:
        try:
            res = requests.get(
                f"{self.rest_url}/accounts/{self.wallet_address}/state",
                headers={"X-API-Key": self.api_key, "Accept": "application/json", "User-Agent": "SoDEX-Bot/2.1"},
                timeout=5,
            ).json()
            positions = res.get("data", {}).get("P", [])
            for p in positions:
                symbol = str(p.get("s", ""))
                size = float(p.get("sz", 0) or 0)
                entry = float(p.get("ep", 0) or 0)
                if "BTC" in symbol and abs(size) > 0:
                    return {"side": "BUY" if size > 0 else "SELL", "entry": entry, "size": abs(size)}
            return None
        except Exception as e:
            logger.error(f"ポジション同期エラー: {e}")
            return None

    def _reconcile_position(self, synced: dict | None):
        if synced:
            if self.position_side is None:
                logger.warning(f"SYNC RESTORE: API上に {synced['side']} ポジション検出 (entry=${synced['entry']:.2f}, size={synced['size']})")
                self.position_side = synced["side"]
                self.last_entry_price = synced["entry"]
                self.current_size = synced["size"]
                self.is_profit_secured = False
                self.entry_mode = self.current_mode
                self.entry_atr = self.current_atr
                self.entry_dvol = self.current_dvol
                if self.current_indicators:
                    self.entry_rsi = self.current_indicators.get("rsi")
                    self.entry_adx = self.current_indicators.get("adx")
            else:
                if abs(self.current_size - synced["size"]) > 0.0001:
                    self.current_size = synced["size"]
                if abs(self.last_entry_price - synced["entry"]) > 0.01:
                    self.last_entry_price = synced["entry"]
        else:
            if self.position_side is not None:
                logger.warning("SYNC CLEAR: API上にポジションなし → ローカル状態クリア")
                self.position_side = None
                self.last_entry_price = None
                self.is_profit_secured = False
                self.entry_mode = None
                self.entry_atr = None

    # ==========================================================
    # リトライ付き決済 (force_close)
    # ==========================================================
    def _force_close(self, current_price: float, reason: str) -> bool:
        synced = self._sync_position()
        if synced is None: return True

        side, size = synced["side"], synced["size"]
        close_side = "SELL" if side == "BUY" else "BUY"

        for i, slip in enumerate(CONFIG["force_close_slippages"]):
            exec_price = current_price * (1 + slip) if close_side == "BUY" else current_price * (1 - slip)
            logger.info(f"FORCE CLOSE ({i+1}): {reason} | {close_side} {size} BTC @ ${exec_price:.2f}")

            if self._place_order(close_side, exec_price, size, is_close=True):
                for check in range(CONFIG["force_close_confirm_checks"]):
                    time.sleep(CONFIG["force_close_confirm_wait"])
                    if self._sync_position() is None:
                        logger.info("FORCE CLOSE OK: 約定確認完了")
                        self._send_settlement_report(synced, current_price, reason)
                        return True
                logger.warning("FORCE CLOSE: 注文送信済みだがポジション残存 → リトライ")

        logger.error(f"FORCE CLOSE FAILED: {reason} → 手動確認必要")
        self._send_email("【SoDEX v2.1】決済失敗", f"決済が完了しませんでした。SoDEX画面を確認してください。\n理由: {reason}")
        return False

    # ==========================================================
    # マクロ指標フィルター
    # ==========================================================
    def _convert_et_to_utc_timestamp(self, date_str: str, et_time: dtime) -> float:
        event_date = datetime.strptime(date_str, "%Y-%m-%d")
        event_et = self.ET.localize(datetime(
            year=event_date.year, month=event_date.month, day=event_date.day,
            hour=et_time.hour, minute=et_time.minute,
        ), is_dst=None)
        return event_et.astimezone(self.UTC).timestamp()

    def _fetch_macro_schedule(self):
        if not CONFIG["macro_filter_enabled"]: return
        api_key = os.getenv("SOSOVALUE_API_KEY", "")
        if not api_key: return

        try:
            res = requests.get(CONFIG["macro_api_url"], headers={"x-soso-api-key": api_key}, timeout=10).json()
            today_utc = datetime.now(self.UTC)
            target_dates = {today_utc.strftime("%Y-%m-%d"), (today_utc - timedelta(days=1)).strftime("%Y-%m-%d")}
            self.macro_events.clear()

            events_list = res.get("data", []) if isinstance(res, dict) else res
            for item in events_list:
                date_str = item.get("date")
                if date_str in target_dates:
                    for event in item.get("events", []):
                        event_lower = event.lower()
                        for target_kw, config in MACRO_CONFIG.items():
                            if any(alias in event_lower for alias in config["aliases"]):
                                try:
                                    event_ts = self._convert_et_to_utc_timestamp(date_str, config["time"])
                                    self.macro_events.setdefault(event_ts, 0)
                                    self.macro_events[event_ts] = max(self.macro_events[event_ts], config["shield_min"])
                                except Exception: pass
            self.last_macro_fetch = time.time()
        except Exception:
            self.last_macro_fetch = time.time()

    def _cleanup_macro_events(self):
        now = time.time()
        self.macro_events = {ts: shield for ts, shield in self.macro_events.items() if now <= ts + (shield * 60)}

    def _get_macro_mode(self) -> str:
        if not CONFIG["macro_filter_enabled"]: return "NORMAL"
        self._cleanup_macro_events()
        if not self.macro_events: return "NORMAL"

        now = time.time()
        for event_ts, shield_min in self.macro_events.items():
            diff = now - event_ts
            if abs(diff) <= (shield_min * 60):
                return "PRE_EVENT" if diff < 0 else "POST_EVENT"
        return "NORMAL"

    def _is_post_event_safe(self) -> bool:
        now = time.time()
        for event_ts in self.macro_events.keys():
            if 0 <= (now - event_ts) < 300: return False
        return True

    def _is_spread_safe(self) -> bool:
        if self.best_bid and self.best_ask and self.best_ask > 0:
            if (self.best_ask - self.best_bid) / self.best_ask > 0.004:
                return False
        return True

    # ==========================================================
    # データ取得 (DVOL, Indicators, Orderbook, Funding, OI, Balance)
    # ==========================================================
    def _get_safe_dvol(self) -> float:
        now = time.time()
        if now - self.last_dvol_fetch < 15 * 60: return self.current_dvol
        try:
            res = requests.get(CONFIG["dvol_api_url"], timeout=5).json()
            if "result" in res and "index_price" in res["result"]:
                self.current_dvol = float(res["result"]["index_price"])
                self.last_dvol_fetch = now
        except: self.last_dvol_fetch = now
        return self.current_dvol

    def _get_market_data(self) -> dict | None:
        now = time.time()
        if self.current_indicators and now - self.last_indicator_fetch < 30:
            return self.current_indicators
        try:
            res = requests.get(f"{self.rest_url}/markets/{CONFIG['symbol']}/klines", 
                               params={"interval": CONFIG["kline_interval"], "limit": 200}, timeout=10).json()
            if res.get("code") == 0 and res.get("data"):
                df = pd.DataFrame(res["data"])
                for col in ["o", "h", "l", "c", "v"]:
                    if col in df.columns: df[col] = df[col].astype(float)
                indicators = compute_indicators(df, CONFIG)
                if indicators:
                    self.current_indicators = indicators
                    self.current_atr = indicators["atr"]
                    self.last_indicator_fetch = now
                    return indicators
        except: pass
        return None

    def _get_orderbook(self) -> dict | None:
        try:
            res = requests.get(f"{self.rest_url}/markets/{CONFIG['symbol']}/orderbook", params={"depth": 5}, timeout=5).json()
            if res.get("code") == 0 and res.get("data"):
                book = res["data"]
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                if bids and asks:
                    parse_p = lambda e: float(e.get("price", e.get("p", 0))) if isinstance(e, dict) else float(e[0])
                    parse_q = lambda e: float(e.get("quantity", e.get("q", 0))) if isinstance(e, dict) else float(e[1])
                    self.best_bid = parse_p(bids[0])
                    self.best_ask = parse_p(asks[0])
                    bid_vol = sum(parse_q(b) for b in bids[:5])
                    ask_vol = sum(parse_q(a) for a in asks[:5])
                    return {
                        "best_bid": self.best_bid, "best_ask": self.best_ask,
                        "bid_volume": bid_vol, "ask_volume": ask_vol,
                        "imbalance": bid_vol / (bid_vol + ask_vol) if (bid_vol + ask_vol) > 0 else 0.5,
                    }
        except: pass
        return None

    def _get_funding_rate(self) -> float:
        now = time.time()
        if now - self.last_funding_fetch < 5 * 60: return self.current_funding_rate
        try:
            res = requests.get(f"{self.rest_url}/markets/{CONFIG['symbol']}/funding-rate", timeout=5).json()
            if res.get("code") == 0 and res.get("data"):
                self.current_funding_rate = float(res["data"].get("fundingRate", 0))
                self.last_funding_fetch = now
        except: self.last_funding_fetch = now
        return self.current_funding_rate

    def _get_open_interest(self) -> float | None:
        now = time.time()
        if now - self.last_oi_fetch < 5 * 60:
            if self.current_oi and self.previous_oi and self.previous_oi > 0:
                return (self.current_oi - self.previous_oi) / self.previous_oi
            return None
        try:
            res = requests.get(f"{self.rest_url}/markets/{CONFIG['symbol']}/open-interest", timeout=5).json()
            if res.get("code") == 0 and res.get("data"):
                self.previous_oi = self.current_oi
                self.current_oi = float(res["data"].get("openInterest", 0))
                self.last_oi_fetch = now
                if self.previous_oi and self.previous_oi > 0:
                    return (self.current_oi - self.previous_oi) / self.previous_oi
        except: self.last_oi_fetch = now
        return None

    def _calculate_position_size(self, stop_loss_distance: float) -> float:
        balance = 0
        try:
            res = requests.get(f"{self.rest_url}/accounts/{self.account_id}/balance", 
                               headers={"Accept": "application/json", "X-API-Key": self.api_key}, timeout=5).json()
            if res.get("code") == 0 and res.get("data"):
                balance = float(res["data"].get("availableBalance", 0))
        except: pass
        
        if balance <= 0 or stop_loss_distance <= 0: return float(CONFIG["fallback_size"])
        raw_size = (balance * CONFIG["risk_per_trade"]) / stop_loss_distance
        size = max(CONFIG["min_size"], min(CONFIG["max_size"], raw_size))
        return math.floor(size * 1000) / 1000

    # ==========================================================
    # カスタム EIP-712 署名
    # ==========================================================
    def _generate_eip712_signature(self, params: OrderedDict, nonce: int) -> str:
        payload = OrderedDict([("type", "newOrder"), ("params", params)])
        payload_json = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
        keccak = lambda b: Web3.keccak(b)
        
        domain_sep = keccak(
            keccak(b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)") +
            keccak(b"futures") + keccak(b"1") + self.chain_id.to_bytes(32, 'big') + bytes(32)
        )
        action_type_hash = keccak(b"ExchangeAction(bytes32 payloadHash,uint64 nonce)")
        payload_hash = keccak(payload_json.encode('utf-8'))
        struct_hash  = keccak(bytes(action_type_hash) + bytes(payload_hash) + nonce.to_bytes(32, 'big'))
        digest       = keccak(b'\x19\x01' + bytes(domain_sep) + bytes(struct_hash))
        
        signed  = self.account.unsafe_sign_hash(digest)
        raw_hex = signed.signature.hex()
        sig_bytes   = bytes.fromhex(raw_hex[2:] if raw_hex.startswith("0x") else raw_hex)
        v_corrected = sig_bytes[-1] - 27
        return "0x01" + (sig_bytes[:-1] + bytes([v_corrected])).hex()

    # ==========================================================
    # 発注処理 (OrderedDict使用)
    # ==========================================================
    def _place_order(self, side: str, base_price: float, size: float, is_close: bool = False) -> bool:
        self.last_order_time = time.time()
        book = self._get_orderbook()
        slippage = CONFIG["orderbook_slippage"]

        if book:
            execute_price = book["best_ask"] + slippage if side == "BUY" else book["best_bid"] - slippage
            logger.info(f"板基準: BID=${book['best_bid']:.2f} ASK=${book['best_ask']:.2f} → 執行=${execute_price:.2f}")
        else:
            execute_price = base_price * 1.005 if side == "BUY" else base_price * 0.995

        execute_price = round(execute_price, 2)
        side_enum = ORDER_SIDE_BUY if side == "BUY" else ORDER_SIDE_SELL

        nonce = int(time.time() * 1000)
        body_ordered = OrderedDict([
            ("accountID", self.account_id),
            ("symbolID", CONFIG["symbol_id"]),
            ("orders", [
                OrderedDict([
                    ("clOrdID", str(uuid.uuid4())[:36]),
                    ("modifier", MODIFIER_NORMAL),
                    ("side", side_enum),
                    ("type", ORDER_TYPE_LIMIT),
                    ("timeInForce", TIF_IOC),
                    ("price", str(execute_price)),
                    ("quantity", str(size)),
                    ("reduceOnly", is_close),
                    ("positionSide", POSITION_SIDE_BOTH),
                ])
            ])
        ])

        body_json = json.dumps(body_ordered, separators=(',', ':'), ensure_ascii=False)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-Key": self.api_key,
            "X-API-Sign": self._generate_eip712_signature(body_ordered, nonce),
            "X-API-Nonce": str(nonce),
        }

        try:
            res = requests.post(f"{self.rest_url}/trade/orders", headers=headers, data=body_json, timeout=10).json()
            if res.get("code") == 0:
                action = "決済" if is_close else "新規"
                logger.info(f"ORDER OK: {side} {action} {size} BTC @ ${execute_price}")
                if not is_close:
                    self._send_email(f"【SoDEX v2.1】{side} エントリー ({self.current_mode})", f"方向: {side}\n価格: ${execute_price}\nサイズ: {size} BTC")
                    self.last_entry_price = base_price
                    self.position_side = side
                    self.current_size = size
                    self.is_profit_secured = False
                    self.entry_mode = self.current_mode
                    self.entry_atr = self.current_atr
                    self.entry_dvol = self.current_dvol
                    if self.current_indicators:
                        self.entry_rsi = self.current_indicators.get("rsi")
                        self.entry_adx = self.current_indicators.get("adx")
                return True
            else:
                logger.error(f"ORDER REJECTED: {res}")
        except Exception as e:
            logger.error(f"注文エラー: {e}")
        return False

    # ==========================================================
    # ポジション管理
    # ==========================================================
    def _manage_position(self, current_price: float) -> bool:
        if not self.last_entry_price or not self.position_side:
            return False

        atr = self.current_atr or self.entry_atr
        if not atr or atr <= 0: atr = self.last_entry_price * 0.02
        entry = self.last_entry_price

        if self.position_side == "BUY":
            pnl_raw = (current_price - entry) / entry
        else:
            pnl_raw = (entry - current_price) / entry

        pnl_roe = pnl_raw * 100 * CONFIG["leverage"]
        pnl_usd = (current_price - entry) * self.current_size
        if self.position_side == "SELL": pnl_usd = -pnl_usd

        atr_roe = (atr / entry) * 100 * CONFIG["leverage"]
        secure_trigger_roe = atr_roe * CONFIG["secure_trigger_atr_mult"]
        secure_floor_roe = atr_roe * CONFIG["secure_floor_atr_mult"]
        take_profit_roe = atr_roe * CONFIG["take_profit_atr_mult"]
        stop_loss_roe = atr_roe * CONFIG["stop_loss_atr_mult"]

        if not self.is_profit_secured and pnl_roe >= secure_trigger_roe:
            self.is_profit_secured = True
            logger.info(f"SECURE ON: ROE {pnl_roe:+.2f}%")

        should_close, reason = False, ""
        if pnl_roe >= take_profit_roe:
            should_close, reason = True, f"利確 (ATR x{CONFIG['take_profit_atr_mult']})"
        elif pnl_roe <= -stop_loss_roe:
            should_close, reason = True, f"損切 (ATR x{CONFIG['stop_loss_atr_mult']})"
        elif self.is_profit_secured and pnl_roe <= secure_floor_roe:
            should_close, reason = True, f"利益確保撤退 (ATR x{CONFIG['secure_floor_atr_mult']})"

        if self.entry_mode == "SCALP" and not should_close:
            if pnl_roe >= atr_roe * 1.5:
                should_close, reason = True, "SCALP利確"
            elif pnl_roe <= -atr_roe * 1.0:
                should_close, reason = True, "SCALP損切"

        if should_close:
            if self._force_close(current_price, reason):
                self._record_pnl(self.position_side, pnl_usd, current_price, reason)
                self.last_entry_price = None
                self.position_side = None
                self.is_profit_secured = False
                self.last_close_time = time.time()
                self.entry_mode = None
                self.entry_atr = None
                return False 
        return True

    # ==========================================================
    # シグナル分析
    # ==========================================================
    def _analyze(self, current_price: float) -> str:
        if not self._is_active_hours(): return "WAIT"

        macro_mode = self._get_macro_mode()
        if macro_mode == "PRE_EVENT" or (macro_mode == "POST_EVENT" and not self._is_post_event_safe()):
            return "WAIT"

        if not self._is_spread_safe(): return "WAIT"

        dvol = self._get_safe_dvol()
        if dvol >= CONFIG["dvol_extreme"]: return "WAIT"
        elif dvol >= CONFIG["dvol_threshold"]: self.current_mode = "SCALP"
        else: self.current_mode = "TREND"

        now = time.time()
        buy_locked = now < self.lockout_time.get("BUY", 0)
        sell_locked = now < self.lockout_time.get("SELL", 0)
        if buy_locked and sell_locked: return "WAIT"

        ind = self._get_market_data()
        if not ind: return "WAIT"

        ema, rsi, adx, bb_upper, bb_lower, volume, vol_sma = ind["ema"], ind["rsi"], ind["adx"], ind["bb_upper"], ind["bb_lower"], ind["volume"], ind["vol_sma"]
        adx_min = 40 if macro_mode == "POST_EVENT" else CONFIG["trend_adx_min"]

        if vol_sma and vol_sma > 0 and volume < vol_sma * CONFIG["volume_filter_mult"]:
            return "WAIT"

        fr = self._get_funding_rate()
        fr_buy_bonus = fr < -CONFIG["funding_rate_threshold"]
        fr_sell_bonus = fr > CONFIG["funding_rate_threshold"]

        oi_change = self._get_open_interest()
        oi_confirms = oi_change is not None and abs(oi_change) > CONFIG["oi_change_threshold"]

        book = self._get_orderbook()
        book_buy = book_sell = False
        if book:
            imb = book["imbalance"]
            if imb > CONFIG["book_imbalance_threshold"]: book_buy = True
            elif imb < (1 - CONFIG["book_imbalance_threshold"]): book_sell = True

        signal = "WAIT"
        if self.current_mode == "TREND":
            if current_price > ema and rsi < CONFIG["trend_rsi_buy_max"] and adx > adx_min and not buy_locked:
                signal = "BUY"
            elif current_price < ema and rsi > CONFIG["trend_rsi_sell_min"] and adx > adx_min and not sell_locked:
                signal = "SELL"
        elif self.current_mode == "SCALP":
            if rsi < CONFIG["scalp_rsi_buy_max"] and current_price <= bb_lower * 1.005 and not buy_locked:
                signal = "BUY"
            elif rsi > CONFIG["scalp_rsi_sell_min"] and current_price >= bb_upper * 0.995 and not sell_locked:
                signal = "SELL"

        if signal != "WAIT": logger.info(f">>> SIGNAL: {signal} <<<")
        return signal

    # ==========================================================
    # WebSocket
    # ==========================================================
    def _on_ws_message(self, ws, message):
        try:
            data = json.loads(message)
            if data.get("op") == "pong": return
            if data.get("channel") == "ticker" and "data" in data:
                for item in data["data"]:
                    if item.get("s") == CONFIG["symbol"]:
                        self.latest_btc_price = float(item["c"])
        except: pass

    def _on_ws_error(self, ws, error):
        logger.error(f"WebSocket Error: {error}")

    def _on_ws_close(self, ws, close_status_code, close_msg):
        logger.warning("WebSocket Closed, reconnecting in 5s...")
        time.sleep(5)
        self._start_websocket()

    def _start_websocket(self):
        with self._ws_lock:
            ws = websocket.WebSocketApp(
                self.ws_url,
                on_open=lambda w: w.send(json.dumps({"op": "subscribe", "params": {"channel": "ticker", "symbols": [CONFIG["symbol"]]}})),
                on_message=self._on_ws_message,
                on_error=self._on_ws_error,
                on_close=self._on_ws_close,
            )
            self._ws = ws
            threading.Thread(target=ws.run_forever, daemon=True).start()
            
            # def ping_loop():
            #     while True:
            #         time.sleep(30)
            #         try: ws.send(json.dumps({"op": "ping"}))
            #         except: break
            # threading.Thread(target=ping_loop, daemon=True).start()

    # ==========================================================
    # メインループ
    # ==========================================================
    def run_strategy(self):
        self._start_websocket()
        self._fetch_macro_schedule()
        
        wait_count = 0
        while self.latest_btc_price is None:
            time.sleep(1)
            wait_count += 1
            if wait_count > 30:
                self._start_websocket()
                wait_count = 0

        logger.info(f"監視開始: ${self.latest_btc_price:.2f}")

        while True:
            try:
                self._check_session()
                if self.session_data.get("status") == "STOPPED":
                    time.sleep(60)
                    continue

                current_price = self.latest_btc_price
                if current_price is None:
                    time.sleep(1)
                    continue

                if time.time() - self.last_macro_fetch > CONFIG["macro_fetch_interval"]:
                    self._fetch_macro_schedule()

                synced = self._sync_position()
                self._reconcile_position(synced)

                is_holding = self._manage_position(current_price)

                if not is_holding:
                    cooldown_elapsed = time.time() - self.last_close_time
                    cooldown_required = CONFIG["cooldown_minutes"] * 60
                    if cooldown_elapsed >= cooldown_required:
                        signal = self._analyze(current_price)
                        if signal != "WAIT":
                            stop_distance = self.current_atr * CONFIG["stop_loss_atr_mult"] if self.current_atr else 0
                            size = self._calculate_position_size(stop_distance)

                            if self._place_order(signal, current_price, size):
                                time.sleep(2)
                                post_entry_sync = self._sync_position()
                                if post_entry_sync:
                                    logger.info(f"ENTRY CONFIRMED: {post_entry_sync['side']} @ ${post_entry_sync['entry']:.2f} ({post_entry_sync['size']} BTC)")
                                    self._reconcile_position(post_entry_sync)
                                else:
                                    logger.warning("ENTRY: 注文送信済みだがポジション未反映 → 次ループで再確認")
            except Exception as e:
                logger.error(f"ループ例外: {e}", exc_info=True)
            
            time.sleep(5)

if __name__ == "__main__":
    API_KEY = os.getenv("SODEX_API_KEY", "Your-API-Key")
    PRIVATE_KEY = os.getenv("SODEX_PRIVATE_KEY", "0xYourEVMPrivateKey...")
    ACCOUNT_ID = int(os.getenv("SODEX_ACCOUNT_ID", "1001"))
    WALLET_ADDR = os.getenv("SODEX_WALLET_ADDRESS", "")
    IS_TESTNET = os.getenv("SODEX_TESTNET", "false").lower() == "true"

    bot = SodexAdvancedBotV2(
        api_key=API_KEY,
        private_key_hex=PRIVATE_KEY,
        account_id=ACCOUNT_ID,
        wallet_address=WALLET_ADDR if WALLET_ADDR else None,
        is_testnet=IS_TESTNET,
    )
    bot.run_strategy()
