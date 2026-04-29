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
from dotenv import load_dotenv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

SESSION_LOG_FILE = BASE_DIR / "session_log.json"
TRADE_LOG_FILE = BASE_DIR / "trade_log.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "bot_v2.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("SoDEX_v2.5")

ORDER_SIDE_BUY = 1
ORDER_SIDE_SELL = 2
ORDER_TYPE_LIMIT = 1
MODIFIER_NORMAL = 1
POSITION_SIDE_BOTH = 1
TIF_IOC = 3

CONFIG = {
    "symbol": "BTC-USD",
    "symbol_id": 1,
    "leverage": 10,
    "risk_per_trade": 0.01,
    "min_size": 0.001,
    "max_size": 0.01,
    "fallback_size": 0.002,
    "kline_interval": "5m",
    "rsi_period": 14,
    "adx_period": 14,
    "ema_period": 50,
    "bb_period": 20,
    "bb_std_dev": 2,
    "atr_period": 14,
    "volume_sma_period": 20,
    "trend_rsi_buy_max": 47,
    "trend_rsi_sell_min": 53,
    "trend_adx_min": 18,
    "min_score_to_enter": 2,
    "scalp_rsi_buy_max": 35,
    "scalp_rsi_sell_min": 65,
    "secure_trigger_atr_mult": 1.2,
    "secure_floor_atr_mult": 0.6,
    "take_profit_atr_mult": 3.0,
    "stop_loss_atr_mult": 2.0,
    "dvol_threshold": 75,
    "dvol_extreme": 90,
    "dvol_api_url": "https://www.deribit.com/api/v2/public/get_index_price?index_name=btcdvol_usdc",
    "orderbook_slippage": 0.5,
    "avoid_hours_start": 0,
    "avoid_hours_end": 6,
    "volume_filter_mult": 0.7,
    "funding_rate_threshold": 0.01,
    "oi_change_threshold": 0.03,
    "book_imbalance_threshold": 0.6,
    "max_loss_per_session": 15.0,
    "cooldown_minutes": 5,
    "lockout_minutes": 90,
    "force_close_retries": 3,
    "force_close_slippages": [0.01, 0.03, 0.05],
    "force_close_confirm_wait": 2,
    "force_close_confirm_checks": 5,
    "macro_filter_enabled": True,
    "macro_api_url": "https://openapi.sosovalue.com/openapi/v1/macro/events",
    "macro_fetch_interval": 86400,
    "gmail_user": os.getenv("GMAIL_USER", ""),
    "gmail_pass": os.getenv("GMAIL_APP_PASSWORD", ""),
}

TAKER_FEE_RATE = 0.0005

MACRO_CONFIG = {
    "CPI": {"time": dtime(8, 30), "shield_min": 90, "aliases": ["cpi", "inflation"]},
    "Nonfarm Payrolls": {"time": dtime(8, 30), "shield_min": 120, "aliases": ["nonfarm", "payroll", "nfp"]},
    "FOMC": {"time": dtime(14, 0), "shield_min": 180, "aliases": ["fomc", "fed rate", "interest rate"]},
    "GDP": {"time": dtime(8, 30), "shield_min": 60, "aliases": ["gdp", "gross domestic"]},
    "PCE": {"time": dtime(8, 30), "shield_min": 60, "aliases": ["pce", "personal consumption"]},
}

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

        self._sodex = requests.Session()
        self._sodex.headers.update({
            "Accept": "application/json",
            "User-Agent": "SoDEX-Bot/2.5",
            "X-API-Key": self.api_key,
        })

        self._public = requests.Session()
        self._public.headers.update({
            "Accept": "application/json",
            "User-Agent": "SoDEX-Bot/2.5",
        })

        self.latest_btc_price = None
        self.last_order_time = 0
        self.last_close_time = 0
        self.is_profit_secured = False
        self.last_entry_price = None
        self.position_side = None
        self.current_size = CONFIG["fallback_size"]
        self.current_mode = "TREND"

        self.current_atr = None
        self.current_indicators = None
        self.last_indicator_fetch = 0
        self.current_dvol = 50.0
        self.last_dvol_fetch = 0

        self.best_bid = None
        self.best_ask = None
        self.current_funding_rate = 0.0
        self.last_funding_fetch = 0
        self.current_oi = None
        self.previous_oi = None
        self.last_oi_fetch = 0

        self.consecutive_loss = {"BUY": 0, "SELL": 0}
        self.lockout_time = {"BUY": 0, "SELL": 0}
        self.session_data = self._load_session_data()

        self.entry_mode = None
        self.entry_atr = None
        self.entry_dvol = None
        self.entry_rsi = None
        self.entry_adx = None

        self.ET = pytz.timezone("US/Eastern")
        self.UTC = pytz.utc
        self.macro_events = {}
        self.last_macro_fetch = 0

        self._ws = None
        self._ws_lock = threading.Lock()
        self._stop_ping = threading.Event()

        init_trade_log()

        logger.info("=" * 58)
        logger.info("  SoDEX Bot v2.5 - Multi-Entry Prevention Guard")
        logger.info(f"  ネットワーク: {'TESTNET' if is_testnet else 'MAINNET'}")
        logger.info(f"  ウォレット: {self.wallet_address}")
        logger.info("=" * 58)

    def _send_email(self, subject: str, text: str, is_html: bool = False):
        if not CONFIG["gmail_user"] or not CONFIG["gmail_pass"]: return
        threading.Thread(target=self._send_email_sync, args=(subject, text, is_html), daemon=True).start()

    def _send_email_sync(self, subject: str, text: str, is_html: bool = False):
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
        subject = f"【SoDEX v2.5】{'利確' if roe > 0 else '損切'}完了 ({side})"
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

    def _get_session_id(self) -> str:
        now = datetime.now(timezone.utc)
        h = now.hour
        d = now.strftime("%Y-%m-%d")
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
        notional = exit_price * self.current_size
        fee = notional * TAKER_FEE_RATE * 2
        net_pnl = pnl_usd - fee
        
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
                self._send_email("【SoDEX v2.5】セッション最大損失超過", f"累計損失: ${self.session_data['totalLoss']:.2f}\nBot停止")
                logger.critical("SESSION STOPPED: 最大損失超過")
            self._save_session_data()
        else:
            self.consecutive_loss[side] = 0

    def _sync_position(self) -> dict | None:
        try:
            res = self._sodex.get(
                f"{self.rest_url}/accounts/{self.wallet_address}/state", 
                params={"accountID": self.account_id}, # API精度向上のため追加
                timeout=5
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
            if self.position_side is None or synced["side"] != self.position_side:
                logger.warning(f"SYNC REPLACE: local={self.position_side} → api={synced['side']} entry=${synced['entry']:.2f} size={synced['size']}")
                self.position_side = synced["side"]
                self.last_entry_price = synced["entry"]
                self.current_size = synced["size"]
                self.is_profit_secured = False
                self.entry_mode = self.current_mode
                self.entry_atr = self.current_atr
                self.entry_dvol = self.current_dvol
                return
            
            if abs(self.current_size - synced["size"]) > 0.0001:
                self.current_size = synced["size"]
            if abs(self.last_entry_price - synced["entry"]) > 0.01:
                self.last_entry_price = synced["entry"]
        else:
            if self.position_side is not None:
                # ★最大の修正ポイント: APIが空を返しても、ローカルの記憶を勝手に消さない
                logger.warning(f"SYNC MISMATCH: ローカルでは {self.position_side} 保有中ですが、APIから取得できません。多重発注を防ぐため状態を維持します。")
                # メモリは消さないため、ボットは常に「ポジション保有中」と認識し、絶対に追撃発注しなくなります。

    def _get_recent_fills(self, symbol: str, limit: int = 10) -> list:
        try:
            res = self._sodex.get(
                f"{self.rest_url}/accounts/{self.wallet_address}/trades",
                params={"accountID": self.account_id, "symbol": symbol, "limit": limit},
                timeout=5
            ).json()
            if res.get("code") == 0 and res.get("data"):
                return res["data"]
            return []
        except Exception as e:
            logger.error(f"fills 取得エラー: {e}")
            return []

    def _force_close(self, current_price: float, reason: str) -> bool:
        synced = self._sync_position()
        
        # ★修正ポイント: APIが空でも、ローカルの記憶を元に強制的に決済を投げる
        side = synced["side"] if synced else self.position_side
        size = synced["size"] if synced else self.current_size
        
        if not side: return True

        close_side = "SELL" if side == "BUY" else "BUY"

        for i, slip in enumerate(CONFIG["force_close_slippages"]):
            exec_price = int(round(current_price * (1 + slip))) if close_side == "BUY" else int(round(current_price * (1 - slip)))
            logger.info(f"FORCE CLOSE ({i+1}): {reason} | {close_side} {size} BTC @ ${exec_price:.2f}")

            if self._place_order(close_side, current_price, size, is_close=True, override_price=exec_price):
                for check in range(CONFIG["force_close_confirm_checks"]):
                    time.sleep(CONFIG["force_close_confirm_wait"])
                    if self._sync_position() is None:
                        logger.info("FORCE CLOSE OK: 約定確認完了")
                        return True
                logger.warning("FORCE CLOSE: 決済注文送信完了 (API反映が遅れている可能性がありますが成功とみなします)")
                return True 

        logger.error(f"FORCE CLOSE FAILED: {reason} → 手動確認必要")
        self._send_email("【SoDEX v2.5】決済失敗", f"決済が完了しませんでした。SoDEX画面を確認してください。\n理由: {reason}")
        return False

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
            res = self._public.get(CONFIG["macro_api_url"], headers={"x-soso-api-key": api_key}, timeout=10).json()
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
        except Exception as e:
            logger.warning(f"マクロ指標取得失敗: {e}")
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

    def _get_safe_dvol(self) -> float:
        now = time.time()
        if now - self.last_dvol_fetch < 15 * 60: return self.current_dvol
        try:
            res = self._public.get(CONFIG["dvol_api_url"], timeout=5).json()
            if "result" in res and "index_price" in res["result"]:
                self.current_dvol = float(res["result"]["index_price"])
                self.last_dvol_fetch = now
        except Exception as e: 
            logger.warning(f"DVOL取得失敗 (次回リトライ): {e}")
            self.last_dvol_fetch = now
        return self.current_dvol

    def _get_market_data(self) -> dict | None:
        now = time.time()
        if self.current_indicators and now - self.last_indicator_fetch < 20:
            return self.current_indicators
        try:
            res = self._sodex.get(f"{self.rest_url}/markets/{CONFIG['symbol']}/klines", 
                               params={"interval": CONFIG["kline_interval"], "limit": 200}, timeout=10).json()
            if res.get("code") == 0 and res.get("data"):
                raw_data = res["data"]
                if isinstance(raw_data, list) and len(raw_data) > 0:
                    sample = raw_data[0]
                    if isinstance(sample, dict):
                        col_map = {}
                        for key in sample.keys():
                            kl = key.lower()
                            if kl in ("o", "open"): col_map[key] = "o"
                            elif kl in ("h", "high"): col_map[key] = "h"
                            elif kl in ("l", "low"): col_map[key] = "l"
                            elif kl in ("c", "close"): col_map[key] = "c"
                            elif kl in ("v", "vol", "volume"): col_map[key] = "v"
                        df = pd.DataFrame(raw_data)
                        if col_map: df = df.rename(columns=col_map)
                    elif isinstance(sample, list):
                        if len(sample) >= 6: df = pd.DataFrame(raw_data, columns=["t", "o", "h", "l", "c", "v"] + [f"x{i}" for i in range(len(sample)-6)])
                        else: df = pd.DataFrame(raw_data)
                    else:
                        df = pd.DataFrame(raw_data)

                    if "t" in df.columns:
                        df["t"] = pd.to_numeric(df["t"], errors="coerce")
                        df = df.sort_values("t").reset_index(drop=True)
                    elif "T" in df.columns:
                        df["T"] = pd.to_numeric(df["T"], errors="coerce")
                        df = df.sort_values("T").reset_index(drop=True)

                    for col in ["o", "h", "l", "c", "v"]:
                        if col in df.columns: df[col] = pd.to_numeric(df[col], errors='coerce')
                    
                    required_cols = {"o", "h", "l", "c", "v"}
                    if not required_cols.issubset(set(df.columns)): return None

                    df = df.dropna(subset=["o", "h", "l", "c"])
                    if len(df) < CONFIG["ema_period"] + 10: return None

                    indicators = compute_indicators(df, CONFIG)
                    if indicators:
                        self.current_indicators = indicators
                        self.current_atr = indicators["atr"]
                        self.last_indicator_fetch = now
                        return indicators
        except Exception as e:
            logger.error(f"kline取得例外: {e}")
        return None

    def _get_orderbook(self) -> dict | None:
        try:
            res = self._sodex.get(f"{self.rest_url}/markets/{CONFIG['symbol']}/orderbook", params={"depth": 5}, timeout=5).json()
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
        except Exception: pass
        return None

    def _get_funding_rate(self) -> float:
        now = time.time()
        if now - self.last_funding_fetch < 5 * 60: return self.current_funding_rate
        try:
            res = self._sodex.get(f"{self.rest_url}/markets/{CONFIG['symbol']}/funding-rate", timeout=5).json()
            if res.get("code") == 0 and res.get("data"):
                self.current_funding_rate = float(res["data"].get("fundingRate", 0))
                self.last_funding_fetch = now
        except Exception: self.last_funding_fetch = now
        return self.current_funding_rate

    def _get_open_interest(self) -> float | None:
        now = time.time()
        if now - self.last_oi_fetch < 5 * 60:
            if self.current_oi and self.previous_oi and self.previous_oi > 0:
                return (self.current_oi - self.previous_oi) / self.previous_oi
            return None
        try:
            res = self._sodex.get(f"{self.rest_url}/markets/{CONFIG['symbol']}/open-interest", timeout=5).json()
            if res.get("code") == 0 and res.get("data"):
                self.previous_oi = self.current_oi
                self.current_oi = float(res["data"].get("openInterest", 0))
                self.last_oi_fetch = now
                if self.previous_oi and self.previous_oi > 0:
                    return (self.current_oi - self.previous_oi) / self.previous_oi
        except Exception: self.last_oi_fetch = now
        return None

    def _calculate_position_size(self, stop_loss_distance: float) -> float:
        balance = 0
        try:
            res = self._sodex.get(f"{self.rest_url}/accounts/{self.account_id}/balance", timeout=5).json()
            if res.get("code") == 0 and res.get("data"):
                balance = float(res["data"].get("availableBalance", 0))
        except Exception: pass
        
        if balance <= 0 or stop_loss_distance <= 0: return float(CONFIG["fallback_size"])
        raw_size = (balance * CONFIG["risk_per_trade"]) / stop_loss_distance
        size = max(CONFIG["min_size"], min(CONFIG["max_size"], raw_size))
        return math.floor(size * 1000) / 1000

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

    def _place_order(self, side: str, base_price: float, size: float, is_close: bool = False, override_price: float = None) -> bool:
        self.last_order_time = time.time()
        
        if override_price is not None:
            execute_price = int(round(override_price))
        else:
            book = self._get_orderbook()
            slippage = CONFIG["orderbook_slippage"]
            if book:
                execute_price = book["best_ask"] + slippage if side == "BUY" else book["best_bid"] - slippage
                logger.info(f"板基準: BID=${book['best_bid']:.2f} ASK=${book['best_ask']:.2f} → 執行=${execute_price:.2f}")
            else:
                execute_price = base_price * 1.005 if side == "BUY" else base_price * 0.995
            execute_price = int(round(execute_price))

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
            "X-API-Sign": self._generate_eip712_signature(body_ordered, nonce),
            "X-API-Nonce": str(nonce),
        }

        try:
            res = self._sodex.post(f"{self.rest_url}/trade/orders", headers=headers, data=body_json, timeout=10).json()
            if res.get("code") == 0:
                action = "決済" if is_close else "新規"
                logger.info(f"ORDER OK: {side} {action} {size} BTC @ ${execute_price}")
                if not is_close:
                    self._send_email(f"【SoDEX v2.5】{side} エントリー ({self.current_mode})", f"方向: {side}\n価格: ${execute_price}\nサイズ: {size} BTC")
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

    def _manage_position(self, current_price: float) -> bool:
        if not self.last_entry_price or not self.position_side: return False

        atr = self.current_atr or self.entry_atr
        if not atr or atr <= 0: atr = self.last_entry_price * 0.02
        entry = self.last_entry_price

        pnl_raw = (current_price - entry) / entry if self.position_side == "BUY" else (entry - current_price) / entry
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
        if pnl_roe >= take_profit_roe: should_close, reason = True, f"利確 (ATR x{CONFIG['take_profit_atr_mult']})"
        elif pnl_roe <= -stop_loss_roe: should_close, reason = True, f"損切 (ATR x{CONFIG['stop_loss_atr_mult']})"
        elif self.is_profit_secured and pnl_roe <= secure_floor_roe: should_close, reason = True, f"利益確保撤退 (ATR x{CONFIG['secure_floor_atr_mult']})"

        if self.entry_mode == "SCALP" and not should_close:
            if pnl_roe >= atr_roe * 1.5: should_close, reason = True, "SCALP利確"
            elif pnl_roe <= -atr_roe * 1.0: should_close, reason = True, "SCALP損切"

        if should_close:
            if self._force_close(current_price, reason):
                time.sleep(1)
                fills = self._get_recent_fills(CONFIG["symbol"], limit=5)
                actual_exit_price = current_price
                if fills:
                    actual_exit_price = float(fills[0].get("price", current_price))
                    logger.info(f"実約定価格: ${actual_exit_price:.2f} (見積: ${current_price:.2f})")

                self._record_pnl(self.position_side, pnl_usd, actual_exit_price, reason)
                
                # ★決済完了時のみローカル状態をクリアする
                self.last_entry_price = None
                self.position_side = None
                self.is_profit_secured = False
                self.last_close_time = time.time()
                self.entry_mode = None
                self.entry_atr = None
                return False 
        return True

    def _analyze(self, current_price: float) -> str:
        # ★最終防衛ライン: ポジション保有中は絶対に分析（新規シグナル発行）しない
        if self.position_side is not None:
            return "WAIT"

        macro_mode = self._get_macro_mode()
        if macro_mode == "PRE_EVENT" or (macro_mode == "POST_EVENT" and not self._is_post_event_safe()):
            logger.debug(f"WAIT: マクロフィルター ({macro_mode})")
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

        ema = ind["ema"]
        rsi = ind["rsi"]
        adx = ind["adx"]
        bb_upper = ind["bb_upper"]
        bb_lower = ind["bb_lower"]
        volume = ind["volume"]
        vol_sma = ind["vol_sma"]
        adx_min = 40 if macro_mode == "POST_EVENT" else CONFIG["trend_adx_min"]

        if vol_sma and vol_sma > 0 and volume < vol_sma * CONFIG["volume_filter_mult"]: return "WAIT"

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
        min_score = CONFIG["min_score_to_enter"]

        if self.current_mode == "TREND":
            buy_score = 0
            buy_cond_price = current_price > ema
            if buy_cond_price and not buy_locked:
                if rsi < CONFIG["trend_rsi_buy_max"]: buy_score += 1
                if adx > adx_min: buy_score += 1
                if fr_buy_bonus: buy_score += 1
                if oi_confirms: buy_score += 1
                if book_buy: buy_score += 1
                if rsi < 40: buy_score += 1

            sell_score = 0
            sell_cond_price = current_price < ema
            if sell_cond_price and not sell_locked:
                if rsi > CONFIG["trend_rsi_sell_min"]: sell_score += 1
                if adx > adx_min: sell_score += 1
                if fr_sell_bonus: sell_score += 1
                if oi_confirms: sell_score += 1
                if book_sell: sell_score += 1
                if rsi > 60: sell_score += 1

            if buy_score >= min_score: signal = "BUY"
            elif sell_score >= min_score: signal = "SELL"
            else:
                logger.info(
                    f"[TREND] P=${current_price:.0f} EMA=${ema:.0f} RSI={rsi:.1f} ADX={adx:.1f} | "
                    f"BUY_score={buy_score}/{min_score} (p>ema={buy_cond_price}) | "
                    f"SELL_score={sell_score}/{min_score} (p<ema={sell_cond_price}) | "
                    f"FR={fr:.4f} OI={oi_confirms} Book={'B' if book_buy else 'S' if book_sell else '-'}"
                )

        elif self.current_mode == "SCALP":
            buy_cond_rsi = rsi < CONFIG["scalp_rsi_buy_max"]
            buy_cond_bb = current_price <= bb_lower * 1.01
            sell_cond_rsi = rsi > CONFIG["scalp_rsi_sell_min"]
            sell_cond_bb = current_price >= bb_upper * 0.99

            if (buy_cond_rsi or buy_cond_bb) and not buy_locked: signal = "BUY"
            elif (sell_cond_rsi or sell_cond_bb) and not sell_locked: signal = "SELL"
            else:
                logger.info(
                    f"[SCALP] P=${current_price:.0f} RSI={rsi:.1f} BB=[{bb_lower:.0f},{bb_upper:.0f}] | "
                    f"BUY: rsi<{CONFIG['scalp_rsi_buy_max']}={buy_cond_rsi} p<=bb_low={buy_cond_bb} | "
                    f"SELL: rsi>{CONFIG['scalp_rsi_sell_min']}={sell_cond_rsi} p>=bb_up={sell_cond_bb}"
                )

        if signal != "WAIT":
            logger.info(f">>> SIGNAL: {signal} ({self.current_mode}) | RSI={rsi:.1f} ADX={adx:.1f} FR={fr:.4f} OI_conf={oi_confirms} Book={'+BUY' if book_buy else '+SELL' if book_sell else 'neutral'} <<<")
        return signal

    def _on_ws_message(self, ws, message):
        try:
            data = json.loads(message)
            if data.get("op") == "pong": return
            if data.get("channel") == "ticker" and "data" in data:
                for item in data["data"]:
                    if item.get("s") == CONFIG["symbol"]: self.latest_btc_price = float(item["c"])
        except Exception: pass

    def _on_ws_error(self, ws, error):
        logger.error(f"WebSocket Error: {error}")

    def _on_ws_close(self, ws, close_status_code, close_msg):
        if ws is not self._ws:
            logger.debug("古いWebSocketからのclose callback → 無視")
            return

        logger.warning("WebSocket Closed, reconnecting in 5s...")
        if hasattr(self, "_stop_ping"): self._stop_ping.set()
        time.sleep(5)
        self._start_websocket()

    def _start_websocket(self):
        with self._ws_lock:
            if self._ws is not None:
                try: self._ws.close()
                except Exception: pass

            self._stop_ping = threading.Event()

            ws = websocket.WebSocketApp(
                self.ws_url,
                on_open=lambda w: w.send(json.dumps({"op": "subscribe", "params": {"channel": "ticker", "symbols": [CONFIG["symbol"]]}})),
                on_message=self._on_ws_message,
                on_error=self._on_ws_error,
                on_close=self._on_ws_close,
            )
            self._ws = ws
            threading.Thread(target=ws.run_forever, daemon=True).start()
            
            def ping_loop(stop_event):
                while not stop_event.is_set():
                    time.sleep(30)
                    try:
                        if ws.sock and ws.sock.connected: ws.send(json.dumps({"op": "ping"}))
                    except Exception as e:
                        logger.debug(f"Ping失敗: {e}")
                        break
            threading.Thread(target=ping_loop, args=(self._stop_ping,), daemon=True).start()

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
        loop_count = 0
        last_heartbeat = time.time()

        while True:
            try:
                loop_count += 1
                if time.time() - last_heartbeat >= 60:
                    logger.info(f"[HEARTBEAT] loop={loop_count} price=${self.latest_btc_price} pos={'LONG' if self.position_side == 'BUY' else 'SHORT' if self.position_side == 'SELL' else 'NONE'} mode={self.current_mode}")
                    last_heartbeat = time.time()

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
    required_envs = ["SODEX_API_KEY", "SODEX_PRIVATE_KEY", "SODEX_ACCOUNT_ID"]
    missing = [k for k in required_envs if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"必須環境変数が未設定です。'.env'ファイルを確認してください: {', '.join(missing)}")

    API_KEY = os.getenv("SODEX_API_KEY")
    PRIVATE_KEY = os.getenv("SODEX_PRIVATE_KEY")
    ACCOUNT_ID = int(os.getenv("SODEX_ACCOUNT_ID"))
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