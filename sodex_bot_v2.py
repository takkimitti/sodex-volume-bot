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
import sys
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
PANIC_LOCK_FILE = BASE_DIR / "panic.lock"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "bot_v2.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("SoDEX_OMS_v3.5.0")

ORDER_SIDE_BUY = 1
ORDER_SIDE_SELL = 2
ORDER_TYPE_LIMIT = 1
MODIFIER_NORMAL = 1
POSITION_SIDE_BOTH = 1
TIF_GTC = 1
TIF_IOC = 3

CONFIG = {
    "symbol": "BTC-USD",
    "symbol_id": 1,
    "leverage": 10,
    "min_size": 0.002,
    "max_size": 0.002, 
    "fallback_size": 0.002,
    "kline_interval": "5m",
    
    # テクニカル設定
    "rsi_period": 14,
    "adx_period": 14,
    "ema_period": 50,
    "bb_period": 20,
    "bb_std_dev": 2,
    "atr_period": 14,
    "volume_sma_period": 20,
    
    # 🎯 エントリー設定
    "trend_rsi_buy_max": 55,
    "trend_rsi_sell_min": 45,
    "trend_adx_min": 28,
    "min_score_to_enter": 3,
    "min_score_to_enter_scalp": 4,
    "scalp_rsi_buy_max": 35,
    "scalp_rsi_sell_min": 65,
    
    # 🎯 決済設定
    "take_profit_atr_mult": 2.0,
    "stop_loss_atr_mult": 1.5,
    "tp_cap_pct_trend": 0.015,
    "tp_cap_pct_scalp": 0.006,
    "sl_cap_pct": 0.008,
    "secure_trigger_atr_mult": 2.5,
    "secure_floor_atr_mult": 1.2,
    
    # 🎯 ダイナミックリスク設定
    "risk_per_trade_base": 0.02,
    "risk_dvol_high": 0.005,
    "dvol_threshold": 75,
    "dvol_extreme": 85,
    "dvol_api_url": "https://www.deribit.com/api/v2/public/get_index_price?index_name=btcdvol_usdc",
    
    "volume_filter_mult": 1.0,
    "funding_rate_threshold": 0.01,
    "oi_change_threshold": 0.03,
    "book_imbalance_threshold": 0.6,
    
    "max_loss_per_session": 15.0,
    "cooldown_minutes": 15,
    "lockout_minutes": 90,
    "force_close_slippages": [0.01, 0.03, 0.05],
    "force_close_confirm_wait": 3,
    "force_close_confirm_checks": 10,
    
    # 🚨 新規: 検疫期間 (Ghost Fill 対策)
    "settlement_quarantine_seconds": 10, 
    
    "macro_filter_enabled": True,
    "macro_api_url": "https://openapi.sosovalue.com/openapi/v1/macro/events",
    "macro_fetch_interval": 86400,
    "gmail_user": os.getenv("GMAIL_USER", ""),
    "gmail_pass": os.getenv("GMAIL_PASSWORD", ""),
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
                "rsi", "adx", "mfe_pct", "mae_pct", "reason",
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
            row.get("adx", ""), row.get("mfe", ""), 
            row.get("mae", ""), row.get("reason", ""),
        ])

def compute_indicators(df: pd.DataFrame, cfg: dict) -> dict | None:
    try:
        mapping = {
            "o": "o", "open": "o",
            "h": "h", "high": "h",
            "l": "l", "low": "l",
            "c": "c", "close": "c",
            "v": "v", "vol": "v", "volume": "v",
        }
        rename_map = {col: mapping[col.lower()] for col in df.columns if col.lower() in mapping}
        df = df.rename(columns=rename_map)
        df = df.loc[:, ~df.columns.duplicated(keep="last")]
        
        for col in ["o", "h", "l", "c", "v"]:
            if col in df.columns: df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df.dropna(subset=["o", "h", "l", "c"])

        close = df["c"]
        high = df["h"]
        low = df["l"]

        ema_series = EMAIndicator(close=close, window=cfg["ema_period"]).ema_indicator()
        rsi_series = RSIIndicator(close=close, window=cfg["rsi_period"]).rsi()
        
        adx_ind = ADXIndicator(high=high, low=low, close=close, window=cfg["adx_period"])
        adx_series = adx_ind.adx()
        plus_di = adx_ind.adx_pos()
        minus_di = adx_ind.adx_neg()
        
        bb_ind = BollingerBands(close=close, window=cfg["bb_period"], window_dev=cfg["bb_std_dev"])
        bbu_series = bb_ind.bollinger_hband()
        bbl_series = bb_ind.bollinger_lband()
        
        atr_series = AverageTrueRange(high=high, low=low, close=close, window=cfg["atr_period"]).average_true_range()
        vol_sma = df["v"].rolling(window=cfg["volume_sma_period"]).mean() if "v" in df.columns else pd.Series(0, index=df.index)

        last_idx = len(df) - 1
        prev_close = close.iloc[-2] if len(close) > 1 else close.iloc[-1]
        
        result = {
            "ema": ema_series.iloc[last_idx],
            "rsi": rsi_series.iloc[last_idx],
            "adx": adx_series.iloc[last_idx],
            "plus_di": plus_di.iloc[last_idx],
            "minus_di": minus_di.iloc[last_idx],
            "atr": atr_series.iloc[last_idx],
            "bb_upper": bbu_series.iloc[last_idx],
            "bb_lower": bbl_series.iloc[last_idx],
            "volume": df["v"].iloc[last_idx] if "v" in df.columns else 0,
            "vol_sma": vol_sma.iloc[last_idx] if not pd.isna(vol_sma.iloc[last_idx]) else 0,
            "prev_close": prev_close
        }
        for key in ["ema", "rsi", "adx", "plus_di", "minus_di", "atr", "bb_upper", "bb_lower"]:
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
        
        if os.path.exists(PANIC_LOCK_FILE):
            logger.critical("=" * 60)
            logger.critical("🚨 [FATAL] panic.lock が存在します！")
            logger.critical("🚨 システムは永続的ロック状態（Panic Mode）です。")
            logger.critical("🚨 手動で取引所の状態を確認・修正し、panic.lock を削除するまで起動しません。")
            logger.critical("=" * 60)
            sys.exit(1)

        self.api_key = api_key
        self.account_id = account_id
        self.private_key = private_key_hex
        self.account = Account.from_key(private_key_hex)
        self.wallet_address = (wallet_address or self.account.address).lower()

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
            "User-Agent": "SoDEX-OMS/3.5.0",
            "X-API-Key": self.api_key,
        })

        self._public = requests.Session()
        self._public.headers.update({
            "Accept": "application/json",
            "User-Agent": "SoDEX-OMS/3.5.0",
        })

        self.latest_btc_price = None
        self.last_order_time = 0
        self.last_close_time = 0
        self.sync_mismatch_count = 0
        
        # 🚨 OMS State Machine Variables
        self.state = "FLAT" 
        self.pending_order_time = 0
        self.cancel_wait_start = 0
        self.settlement_start_time = 0
        self.pending_cl_ord_id = None 
        
        self.current_mode = "TREND"
        self._reset_local_state()

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

        self.ET = pytz.timezone("US/Eastern")
        self.UTC = pytz.utc
        self.macro_events = {}
        self.last_macro_fetch = 0

        self._ws = None
        self._stop_ws = threading.Event()
        self._stop_ping = threading.Event()

        init_trade_log()

        logger.info("=" * 60)
        logger.info("  SoDEX OMS v3.5.0 - INSTITUTIONAL EXECUTION EDITION")
        logger.info(f"  ネットワーク: {'TESTNET' if is_testnet else 'MAINNET'}")
        logger.info(f"  ウォレット: {self.wallet_address}")
        logger.info("=" * 60)

    # 🚨 1. 絶対防衛 (Reactive Kill Switch)
    def _trigger_panic_lock(self, current_price: float, reason: str):
        logger.critical(f"🚨🚨 PANIC SEQUENCE INITIATED: {reason} 🚨🚨")
        with open(PANIC_LOCK_FILE, "w") as f:
            f.write(f"PANIC TRIGGERED AT: {datetime.now(timezone.utc).isoformat()}\nREASON: {reason}")
            
        self._cancel_all_orders()
        self._force_close(current_price, f"EMERGENCY FLATTEN ({reason})")
        
        self.state = "PANIC"
        self._send_email("【緊急停止】SoDEX OMS v3.5.0", f"致命的な異常を検知したため、panic.lockを生成し完全停止しました。\n理由: {reason}")
        logger.critical("🚨 システムを安全に停止しました。手動介入が必要です。")

    # 🚨 2. Invariant チェック (事後検知)
    def _check_exposure_invariant(self, current_price: float, synced_data: dict | None) -> bool:
        exchange_size = synced_data["size"] if synced_data else 0.0
        if exchange_size > CONFIG["max_size"] * 1.1:
            self._trigger_panic_lock(current_price, f"Hard Exposure Cap 超過 (Exchange: {exchange_size} BTC > Max: {CONFIG['max_size']} BTC)")
            return False

        if self.state in ["FLAT", "IN_POSITION"]:
            local_size = self.current_size if self.state == "IN_POSITION" else 0.0
            drift = abs(local_size - exchange_size)
            if drift > 0:
                logger.error(f"⚠️ EXPOSURE DRIFT DETECTED: Local={local_size} BTC, Exchange={exchange_size} BTC, Drift={drift} BTC")
                if drift >= CONFIG["min_size"]:
                    self._trigger_panic_lock(current_price, f"致命的なポジション乖離 (Drift: {drift} BTC)")
                    return False
        return True

    # 🚨 3. Pre-Trade Risk Engine (事前抑止)
    def _get_effective_exposure(self) -> float:
        try:
            res = self._sodex.get(
                f"{self.rest_url}/accounts/{self.wallet_address}/state", 
                params={"accountID": self.account_id, "t": int(time.time() * 1000)},
                timeout=5
            ).json()
            data = res.get("data", {})
            
            # 1. ライブポジションサイズの計算
            positions = data.get("P", []) or data.get("positions", [])
            live_size = 0.0
            base_asset = CONFIG["symbol"].split("-")[0]
            
            for p in positions:
                symbol = str(p.get("s", p.get("symbol", "")))
                if symbol == CONFIG["symbol"] or symbol == base_asset:
                    live_size += abs(float(p.get("sz", p.get("size", p.get("positionAmt", 0))) or 0))
            
            # 2. 未約定注文の厳密な残量計算
            open_orders = data.get("orders", []) or []
            outstanding_size = 0.0
            
            for o in open_orders:
                is_reduce_only = o.get("reduceOnly", False) or o.get("ro", False)
                if not is_reduce_only:
                    original_qty = float(o.get("q", o.get("quantity", 0)) or 0)
                    filled_qty = float(o.get("f", o.get("filledQty", o.get("executedQty", 0))) or 0)
                    remaining = max(0.0, original_qty - filled_qty)
                    outstanding_size += remaining
            
            effective_exp = live_size + outstanding_size
            if effective_exp > 0:
                logger.info(f"[RISK ENGINE] Effective Exposure: {effective_exp:.4f} BTC (Live: {live_size:.4f}, Pending: {outstanding_size:.4f})")
                
            return effective_exp
            
        except Exception as e:
            logger.error(f"[RISK ENGINE] Effective Exposure 取得エラー(Fail Safe): {e}")
            return float('inf') # 安全側に倒す

    def _reset_local_state(self):
        self.position_side = None
        self.last_entry_price = None
        self.is_profit_secured = False
        self.state = "FLAT"
        self.mfe = 0.0
        self.mae = 0.0
        self.entry_mode = None
        self.entry_atr = None
        self.entry_dvol = None
        self.entry_rsi = None
        self.entry_adx = None
        self.current_size = 0.0
        self.entry_timestamp = 0
        self.penalty_time = 0

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
        except Exception: pass

    def _send_settlement_report(self, pos_info: dict, exit_price: float, reason: str):
        side = pos_info["side"]
        entry = pos_info["entry"]
        size = pos_info["size"]
        pnl_usd = (exit_price - entry) * size if side == "BUY" else (entry - exit_price) * size
        pnl_pct = (exit_price - entry) / entry if side == "BUY" else (entry - exit_price) / entry
        roe = pnl_pct * CONFIG["leverage"] * 100
        color = "#2ecc71" if roe > 0 else "#e74c3c"
        subject = f"【SoDEX OMS v3.5.0】{'利確' if roe > 0 else '損切'}完了 ({side})"
        html = f"""<div style="font-family: sans-serif; padding: 20px; border: 1px solid #eee;"><h2 style="color: {color}; border-bottom: 2px solid;">{reason} 実行報告</h2><table style="width: 100%; border-collapse: collapse;"><tr><td style="padding: 8px; border-bottom: 1px solid #eee;"><b>方向 / サイズ</b></td><td style="text-align: right;">{side} ({size} BTC)</td></tr><tr><td style="padding: 8px; border-bottom: 1px solid #eee;"><b>エントリー建値</b></td><td style="text-align: right;">${int(entry)}</td></tr><tr><td style="padding: 8px; border-bottom: 1px solid #eee;"><b>決済価格</b></td><td style="text-align: right;">${int(exit_price)}</td></tr><tr><td style="padding: 8px; border-bottom: 1px solid #eee; font-size: 1.2em;"><b>損益 (ROE)</b></td><td style="text-align: right; color: {color}; font-size: 1.2em;"><b>{roe:+.2f}% (${pnl_usd:+.2f})</b></td></tr></table></div>"""
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
        temp_file = str(SESSION_LOG_FILE) + ".tmp"
        with open(temp_file, "w") as f:
            json.dump(self.session_data, f, indent=2)
        os.replace(temp_file, SESSION_LOG_FILE)

    def _check_session(self):
        now_id = self._get_session_id()
        if self.session_data.get("currentSession") != now_id:
            old_recent = self.session_data.get("recent_results", [])
            self.session_data = {"currentSession": now_id, "totalLoss": 0, "status": "ACTIVE", "recent_results": old_recent[-5:]}
            self._save_session_data()
            self.consecutive_loss = {"BUY": 0, "SELL": 0}
            self.lockout_time = {"BUY": 0, "SELL": 0}
            logger.info(f"新セッション開始: {now_id} (記憶引継ぎ完了)")

    def _record_pnl(self, side: str, pnl_usd: float, exit_price: float, reason: str):
        notional = exit_price * self.current_size
        fee = notional * TAKER_FEE_RATE * 2
        net_pnl = pnl_usd - fee
        pnl_roe = 0.0
        if self.last_entry_price and self.last_entry_price > 0:
            pnl_raw = (exit_price - self.last_entry_price) / self.last_entry_price
            if side == "SELL": pnl_raw = -pnl_raw
            pnl_roe = pnl_raw * 100 * CONFIG["leverage"]

        if pnl_roe != 0.0:
            if "recent_results" not in self.session_data: self.session_data["recent_results"] = []
            self.session_data["recent_results"].append(pnl_roe)
            if len(self.session_data["recent_results"]) > 5: self.session_data["recent_results"].pop(0)

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
            "mfe": round(self.mfe, 2), "mae": round(self.mae, 2), "reason": reason,
        })

        if net_pnl < 0:
            self.session_data["totalLoss"] += abs(net_pnl)
            self.consecutive_loss[side] += 1
            self.penalty_time = 900 if "緊急撤退" in reason else 180
            if self.consecutive_loss[side] >= 2:
                self.lockout_time[side] = time.time() + (CONFIG["lockout_minutes"] * 60)
                self.consecutive_loss[side] = 0
                logger.warning(f"LOCKOUT: {side}方向 {CONFIG['lockout_minutes']}分間停止")
            if self.session_data["totalLoss"] >= CONFIG["max_loss_per_session"]:
                self.session_data["status"] = "STOPPED"
                self._send_email("【SoDEX OMS】セッション最大損失超過", f"累計損失: ${self.session_data['totalLoss']:.2f}\nBot停止")
                logger.critical("SESSION STOPPED: 最大損失超過")
            self._save_session_data()
        else:
            self.consecutive_loss[side] = 0
            self.penalty_time = 0

    def _sync_position(self) -> dict | None:
        try:
            res = self._sodex.get(f"{self.rest_url}/accounts/{self.wallet_address}/state", params={"accountID": self.account_id, "t": int(time.time() * 1000)}, timeout=5).json()
            base_asset = CONFIG["symbol"].split("-")[0]
            for p in res.get("data", {}).get("P", []) or res.get("data", {}).get("positions", []):
                symbol = str(p.get("s", p.get("symbol", "")))
                size = float(p.get("sz", p.get("size", p.get("positionAmt", 0))) or 0)
                entry = float(p.get("ep", p.get("entryPrice", 0)) or 0)
                if (symbol == CONFIG["symbol"] or symbol == base_asset) and abs(size) > 0:
                    return {"side": "BUY" if size > 0 else "SELL", "entry": entry, "size": abs(size)}
            return None
        except Exception: return None

    def _reconcile_position(self, synced: dict | None):
        if synced:
            self.sync_mismatch_count = 0  
            if self.position_side is None or synced["side"] != self.position_side:
                logger.warning(f"SYNC REPLACE (NEW POSITION): local={self.position_side} → api={synced['side']} entry=${synced['entry']:.2f} size={synced['size']}")
                self.position_side = synced["side"]
                self.last_entry_price = synced["entry"]
                self.current_size = synced["size"]
                self.is_profit_secured = False
                self.mfe = 0.0; self.mae = 0.0
                self.entry_timestamp = time.time()
                self.state = "IN_POSITION"
                if not self.entry_mode: self.entry_mode = self.current_mode
                if not self.entry_atr: self.entry_atr = self.current_atr
                if not self.entry_dvol: self.entry_dvol = self.current_dvol
            else:
                if abs(self.current_size - synced["size"]) > 0.0001: self.current_size = synced["size"]
                if abs(self.last_entry_price - synced["entry"]) > 0.01: self.last_entry_price = synced["entry"]
        else:
            if self.position_side is not None:
                self.sync_mismatch_count += 1
                logger.warning(f"SYNC MISMATCH ({self.sync_mismatch_count}/10): APIから取得できません。")
                if self.sync_mismatch_count >= 10:
                    logger.critical("SYNC MISMATCH が10回連続したため、ポジション情報を強制リセットします。")
                    self._reset_local_state()
                    self.sync_mismatch_count = 0

    def _cancel_all_orders(self) -> bool:
        try:
            nonce = int(time.time() * 1000)
            body = OrderedDict([("accountID", self.account_id), ("symbolID", CONFIG["symbol_id"])])
            headers = {"Content-Type": "application/json", "X-API-Sign": self._generate_eip712_signature(body, nonce), "X-API-Nonce": str(nonce)}
            self._sodex.delete(f"{self.rest_url}/trade/orders", headers=headers, data=json.dumps(body, separators=(',', ':')), timeout=5)
            logger.info("❌ 未約定の注文 (Stale Order) をキャンセルしました。")
            return True
        except Exception as e:
            logger.error(f"キャンセルリクエストエラー: {e}")
            return False

    def _force_close(self, current_price: float, reason: str) -> bool:
        for i, slip in enumerate(CONFIG["force_close_slippages"]):
            synced = self._sync_position()
            if synced is None: return True
            side = synced["side"]
            size = synced["size"]
            close_side = "SELL" if side == "BUY" else "BUY"
            exec_price = int(round(current_price * (1 + slip))) if close_side == "BUY" else int(round(current_price * (1 - slip)))
            logger.info(f"FORCE CLOSE ({i+1}): {reason} | {close_side} {size} BTC @ ${exec_price:.2f}")
            if self._place_order(close_side, current_price, size, is_close=True, override_price=exec_price):
                for _ in range(CONFIG["force_close_confirm_checks"]):
                    time.sleep(CONFIG["force_close_confirm_wait"])
                    if self._sync_position() is None:
                        logger.info("FORCE CLOSE OK: 約定確認完了")
                        return True
        return False

    def _convert_et_to_utc_timestamp(self, date_str: str, et_time: dtime) -> float:
        event_date = datetime.strptime(date_str, "%Y-%m-%d")
        event_et = self.ET.localize(datetime(year=event_date.year, month=event_date.month, day=event_date.day, hour=et_time.hour, minute=et_time.minute), is_dst=False)
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
            for item in (res.get("data", []) if isinstance(res, dict) else res):
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
        except Exception: self.last_macro_fetch = time.time()

    def _get_macro_mode(self) -> str:
        if not CONFIG["macro_filter_enabled"]: return "NORMAL"
        now = time.time()
        self.macro_events = {ts: shield for ts, shield in self.macro_events.items() if now <= ts + (shield * 60)}
        if not self.macro_events: return "NORMAL"
        for event_ts, shield_min in self.macro_events.items():
            diff = now - event_ts
            if abs(diff) <= (shield_min * 60): return "PRE_EVENT" if diff < 0 else "POST_EVENT"
        return "NORMAL"

    def _is_spread_safe(self) -> bool:
        if self.best_bid and self.best_ask and self.best_ask > 0:
            if (self.best_ask - self.best_bid) / self.best_ask > 0.004: return False
        return True

    def _get_safe_dvol(self) -> float:
        now = time.time()
        if now - self.last_dvol_fetch < 15 * 60: return self.current_dvol
        try:
            res = self._public.get(CONFIG["dvol_api_url"], params={"t": int(time.time() * 1000)}, timeout=5).json()
            if "result" in res and "index_price" in res["result"]:
                self.current_dvol = float(res["result"]["index_price"])
                self.last_dvol_fetch = now
        except Exception: self.last_dvol_fetch = now
        return self.current_dvol

    def _get_market_data(self) -> dict | None:
        now = time.time()
        if self.current_indicators and now - self.last_indicator_fetch < 20: return self.current_indicators
        try:
            res = self._sodex.get(f"{self.rest_url}/markets/{CONFIG['symbol']}/klines", params={"interval": CONFIG["kline_interval"], "limit": 250, "t": int(time.time() * 1000)}, timeout=10).json()
            if res.get("code") == 0 and res.get("data"):
                indicators = compute_indicators(pd.DataFrame(res["data"]), CONFIG)
                if indicators:
                    self.current_indicators = indicators
                    self.current_atr = indicators["atr"]
                    self.last_indicator_fetch = now
                    return indicators
        except Exception: pass
        return None

    def _get_orderbook(self) -> dict | None:
        try:
            res = self._sodex.get(f"{self.rest_url}/markets/{CONFIG['symbol']}/orderbook", params={"depth": 5, "t": int(time.time() * 1000)}, timeout=5).json()
            if res.get("code") == 0 and res.get("data"):
                book = res["data"]
                bids, asks = book.get("bids", []), book.get("asks", [])
                if bids and asks:
                    parse_p = lambda e: float(e.get("price", e.get("p", 0))) if isinstance(e, dict) else float(e[0])
                    parse_q = lambda e: float(e.get("quantity", e.get("q", 0))) if isinstance(e, dict) else float(e[1])
                    self.best_bid, self.best_ask = parse_p(bids[0]), parse_p(asks[0])
                    bid_vol, ask_vol = sum(parse_q(b) for b in bids[:5]), sum(parse_q(a) for a in asks[:5])
                    return {"best_bid": self.best_bid, "best_ask": self.best_ask, "imbalance": bid_vol / (bid_vol + ask_vol) if (bid_vol + ask_vol) > 0 else 0.5}
        except Exception: pass
        return None

    def _get_funding_rate(self) -> float:
        now = time.time()
        if now - self.last_funding_fetch < 5 * 60: return self.current_funding_rate
        try:
            res = self._sodex.get(f"{self.rest_url}/markets/{CONFIG['symbol']}/funding-rate", params={"t": int(time.time() * 1000)}, timeout=5).json()
            if res.get("code") == 0 and res.get("data"):
                self.current_funding_rate = float(res["data"].get("fundingRate", 0))
                self.last_funding_fetch = now
        except Exception: self.last_funding_fetch = now
        return self.current_funding_rate

    def _get_open_interest(self) -> float | None:
        now = time.time()
        if now - self.last_oi_fetch < 5 * 60:
            return (self.current_oi - self.previous_oi) / self.previous_oi if self.current_oi and self.previous_oi else None
        try:
            res = self._sodex.get(f"{self.rest_url}/markets/{CONFIG['symbol']}/open-interest", params={"t": int(time.time() * 1000)}, timeout=5).json()
            if res.get("code") == 0 and res.get("data"):
                self.previous_oi, self.current_oi = self.current_oi, float(res["data"].get("openInterest", 0))
                self.last_oi_fetch = now
                return (self.current_oi - self.previous_oi) / self.previous_oi if self.previous_oi else None
        except Exception: self.last_oi_fetch = now
        return None

    def _calculate_position_size(self, stop_loss_distance: float, risk_override: float = None) -> float:
        balance = 0.0
        try:
            res = self._sodex.get(f"{self.rest_url}/accounts/{self.wallet_address}/state", params={"accountID": self.account_id, "t": int(time.time() * 1000)}, timeout=5).json()
            if res.get("code") == 0 and res.get("data"):
                balance = float(res["data"].get("availableMargin", res["data"].get("availableBalance", res["data"].get("marginBalance", 0))))
        except Exception: pass
            
        if balance <= 0: return float(CONFIG["min_size"]) if CONFIG["min_size"] == CONFIG["max_size"] else 0.0
        if stop_loss_distance <= 0: return 0.0
        
        current_risk = risk_override if risk_override is not None else CONFIG["risk_per_trade_base"]
        dvol = getattr(self, 'current_dvol', 50)
        if dvol > CONFIG["dvol_extreme"]: current_risk = min(current_risk, CONFIG["risk_dvol_high"])
                    
        size = max(CONFIG["min_size"], min(CONFIG["max_size"], (balance * current_risk) / stop_loss_distance))
        return math.floor(size * 1000) / 1000

    def _generate_eip712_signature(self, params: OrderedDict, nonce: int) -> str:
        payload_json = json.dumps(OrderedDict([("type", "newOrder"), ("params", params)]), separators=(',', ':'), ensure_ascii=False)
        keccak = lambda b: Web3.keccak(b)
        domain_sep = keccak(keccak(b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)") + keccak(b"futures") + keccak(b"1") + self.chain_id.to_bytes(32, 'big') + bytes(32))
        struct_hash  = keccak(bytes(keccak(b"ExchangeAction(bytes32 payloadHash,uint64 nonce)")) + bytes(keccak(payload_json.encode('utf-8'))) + nonce.to_bytes(32, 'big'))
        signed = self.account.unsafe_sign_hash(keccak(b'\x19\x01' + bytes(domain_sep) + bytes(struct_hash)))
        sig_bytes = bytes.fromhex(signed.signature.hex()[2:])
        return "0x01" + (sig_bytes[:-1] + bytes([sig_bytes[-1] - 27])).hex()

    def _place_order(self, side: str, base_price: float, size: float, is_close: bool = False, override_price: float = None) -> bool:
        self.last_order_time = time.time()
        book = self._get_orderbook()
        dvol = getattr(self, 'current_dvol', 50)
        adx = self.entry_adx or (self.current_indicators["adx"] if getattr(self, 'current_indicators', None) else 20)

        if override_price is not None: 
            execute_price = int(round(override_price))
            tif = TIF_IOC
        else:
            if not is_close and dvol < 70 and adx < 35 and book:
                execute_price = int(round(book["best_bid"] if side == "BUY" else book["best_ask"]))
                tif = TIF_GTC
            else:
                execute_price = int(round(book["best_ask"] + 0.5 if side == "BUY" else book["best_bid"] - 0.5)) if book else int(round(base_price * (1.005 if side == "BUY" else 0.995)))
                tif = TIF_IOC

        nonce = int(time.time() * 1000)
        cl_ord_id = str(uuid.uuid4())[:36]
        
        if not is_close: self.pending_cl_ord_id = cl_ord_id

        body = OrderedDict([("accountID", self.account_id), ("symbolID", CONFIG["symbol_id"]), ("orders", [OrderedDict([("clOrdID", cl_ord_id), ("modifier", MODIFIER_NORMAL), ("side", ORDER_SIDE_BUY if side == "BUY" else ORDER_SIDE_SELL), ("type", ORDER_TYPE_LIMIT), ("timeInForce", tif), ("price", str(execute_price)), ("quantity", str(size)), ("reduceOnly", is_close), ("positionSide", POSITION_SIDE_BOTH)])])])
        headers = {"Content-Type": "application/json", "X-API-Sign": self._generate_eip712_signature(body, nonce), "X-API-Nonce": str(nonce)}
        
        try:
            # json()でいきなりパースせず、まずは生のレスポンスオブジェクトを受け取る
            response = self._sodex.post(f"{self.rest_url}/trade/orders", headers=headers, data=json.dumps(body, separators=(',', ':')), timeout=10)
            
            # JSONとしてパース
            res = response.json()
            
            if res.get("code") == 0:
                logger.info(f"ORDER SENT: {side} {'決済' if is_close else '新規'} {size} BTC @ ${execute_price}")
                return True
            else:
                # APIが0以外を返した場合：取引所調査用のヘッダーIDと生テキストを抽出して警告ログに出力
                req_id = response.headers.get("X-Request-Id", response.headers.get("Trace-Id", "Unknown"))
                logger.warning(f"⚠️ API ORDER REJECTED: {res.get('msg', 'Unknown Error')} (Code: {res.get('code')}) | ReqID: {req_id} | Raw: {response.text}")
                
        except Exception as e: 
            logger.error(f"注文エラー: {e}")
            
        return False

    def _manage_position(self, current_price: float) -> bool:
        if not self.last_entry_price or not self.position_side: return False
        
        atr = self.current_atr or self.entry_atr or self.last_entry_price * 0.02
        entry = self.last_entry_price
        current_mode = self.entry_mode or self.current_mode
        active_tp_cap = CONFIG["tp_cap_pct_trend"] if current_mode == "TREND" else CONFIG["tp_cap_pct_scalp"]
        
        tp_dist = min(atr * CONFIG["take_profit_atr_mult"], entry * active_tp_cap)
        sl_dist = min(atr * CONFIG["stop_loss_atr_mult"], entry * CONFIG["sl_cap_pct"])
        secure_trigger_dist = atr * CONFIG["secure_trigger_atr_mult"]
        secure_floor_dist = atr * CONFIG["secure_floor_atr_mult"]

        pnl_roe = ((current_price - entry) / entry if self.position_side == "BUY" else (entry - current_price) / entry) * 100 * CONFIG["leverage"]
        tp_roe = (tp_dist / entry) * 100 * CONFIG["leverage"]
        sl_roe = (sl_dist / entry) * 100 * CONFIG["leverage"]
        secure_trigger_roe = (secure_trigger_dist / entry) * 100 * CONFIG["leverage"]
        secure_floor_roe = (secure_floor_dist / entry) * 100 * CONFIG["leverage"]

        hold_time = time.time() - getattr(self, 'entry_timestamp', time.time())
        sl_enabled = hold_time >= 30

        if pnl_roe > self.mfe: self.mfe = pnl_roe
        if pnl_roe < self.mae: self.mae = pnl_roe

        if not self.is_profit_secured and pnl_roe >= secure_trigger_roe:
            self.is_profit_secured = True
            logger.info(f"SECURE ON: ROE {pnl_roe:+.2f}% (Trigger: {secure_trigger_roe:.1f}%)")
        
        should_close, reason = False, ""
        if pnl_roe >= tp_roe: 
            should_close, reason = True, f"利確 (Target: {tp_roe:.1f}%)"
        elif sl_enabled and pnl_roe <= -sl_roe: 
            should_close, reason = True, f"損切 (Stop: -{sl_roe:.1f}%)"
        elif self.is_profit_secured and hold_time > 60: 
            trail_ratio = 0.70 if current_mode == "TREND" else 0.45
            trail_limit = max(self.mfe * trail_ratio, secure_floor_roe)
            if pnl_roe <= trail_limit:
                should_close, reason = True, f"利益確保トレール (Peak: {self.mfe:.1f}%)"

        if not should_close and pnl_roe < -3.0 and hold_time >= 180:
            ind = self._get_market_data()  
            if ind:
                c_ema, c_rsi, c_adx = ind["ema"], ind["rsi"], ind["adx"]
                if self.position_side == "BUY" and current_price < c_ema and c_adx > 25 and c_rsi < 38:
                    should_close, reason = True, f"緊急撤退 (下落検知: ADX={c_adx:.1f} RSI={c_rsi:.1f})"
                elif self.position_side == "SELL" and current_price > c_ema and c_adx > 25 and c_rsi > 62:
                    should_close, reason = True, f"緊急撤退 (上昇検知: ADX={c_adx:.1f} RSI={c_rsi:.1f})"

        if should_close:
            if self._force_close(current_price, reason):
                exit_p = current_price
                entry = self.last_entry_price
                self._send_settlement_report({"side": self.position_side, "entry": self.last_entry_price, "size": self.current_size}, exit_p, reason)
                self._record_pnl(self.position_side, (exit_p - entry if self.position_side == "BUY" else entry - exit_p) * self.current_size, exit_p, reason)
                self._reset_local_state()
                self.last_close_time = time.time() + getattr(self, 'penalty_time', 0)
                return False 
        return True

    def _apply_sync_rate(self, base_score_req, base_risk):
        recent = self.session_data.get("recent_results", [])
        adjusted_score_req = base_score_req
        adjusted_risk = base_risk
        if len(recent) >= 3:
            last_3 = recent[-3:]
            win_count = sum(1 for r in last_3 if r > 0)
            loss_count = sum(1 for r in last_3 if r < 0)
            recent_pnl = sum(last_3)
            current_dvol = self._get_safe_dvol()
            latest_roe = last_3[-1]

            if win_count == 3 and recent_pnl > 8.0 and latest_roe > 2.0 and current_dvol > 55.0:
                adjusted_score_req = max(1, base_score_req - 1)
                adjusted_risk = base_risk * 1.3
            elif loss_count >= 2 and latest_roe < -1.5 and current_dvol > 70.0:
                adjusted_score_req = base_score_req + 1
                adjusted_risk = base_risk * 0.5
        return adjusted_score_req, adjusted_risk

    def _analyze(self, current_price: float) -> str:
        macro_m = self._get_macro_mode()
        if macro_m != "NORMAL": return "WAIT"
        if not self._is_spread_safe(): return "WAIT"
        
        ind = self._get_market_data()
        if not ind: return "WAIT"

        # 🚨 ADXフィルター (死海での撤退)
        if ind["adx"] < 15:
            return "WAIT"

        dvol = self._get_safe_dvol()
        dynamic_trend_score = CONFIG["min_score_to_enter"]
        dynamic_scalp_score = CONFIG["min_score_to_enter_scalp"]
        current_risk = CONFIG["risk_per_trade_base"]

        dynamic_trend_score, current_risk = self._apply_sync_rate(dynamic_trend_score, current_risk)
        score_diff = dynamic_trend_score - CONFIG["min_score_to_enter"]
        dynamic_scalp_score = max(1, dynamic_scalp_score + score_diff)
        self.current_dynamic_risk = current_risk

        if dvol > CONFIG["dvol_extreme"]: 
            dynamic_trend_score += 1; dynamic_scalp_score += 1

        self.current_mode = "SCALP" if dvol >= CONFIG["dvol_threshold"] else "TREND"       
        now = time.time()
        can_buy = now >= self.lockout_time.get("BUY", 0)
        can_sell = now >= self.lockout_time.get("SELL", 0)
        
        ema, rsi, adx = ind["ema"], ind["rsi"], ind["adx"]
        p_di, m_di = ind["plus_di"], ind["minus_di"]
        bb_upper, bb_lower = ind["bb_upper"], ind["bb_lower"]

        if rsi < 25 and adx > 25 and can_buy: can_buy = False
        if rsi > 75 and adx > 25 and can_sell: can_sell = False
        if not can_buy and not can_sell: return "WAIT"

        fr = self._get_funding_rate()
        oi_change = self._get_open_interest()
        
        is_price_up = current_price > ind["prev_close"]
        oi_bullish = oi_change is not None and oi_change > CONFIG["oi_change_threshold"] and is_price_up and fr >= 0
        oi_bearish = oi_change is not None and oi_change > CONFIG["oi_change_threshold"] and not is_price_up and fr <= 0
        
        if ind["volume"] < ind["vol_sma"] * CONFIG["volume_filter_mult"]: return "WAIT"
        
        book = self._get_orderbook()
        imb = book["imbalance"] if book else 0.5
        b_buy, b_sell = imb > CONFIG["book_imbalance_threshold"], imb < (1 - CONFIG["book_imbalance_threshold"])
        
        signal = "WAIT"
        if self.current_mode == "TREND":
            buy_s = (1 if rsi < CONFIG["trend_rsi_buy_max"] else 0) + (1 if adx > CONFIG["trend_adx_min"] else 0) + (1 if p_di > m_di else 0) + (0.25 if fr < -CONFIG["funding_rate_threshold"] else 0) + (1 if oi_bullish else 0) + (1 if b_buy else 0)
            sell_s = (1 if rsi > CONFIG["trend_rsi_sell_min"] else 0) + (1 if adx > CONFIG["trend_adx_min"] else 0) + (1 if m_di > p_di else 0) + (0.25 if fr > CONFIG["funding_rate_threshold"] else 0) + (1 if oi_bearish else 0) + (1 if b_sell else 0)
            
            if sell_s > buy_s and can_sell and sell_s >= dynamic_trend_score and (current_price < ema or sell_s >= 3): signal = "SELL"
            elif buy_s >= sell_s and can_buy and buy_s >= dynamic_trend_score and (current_price > ema or buy_s >= 3): signal = "BUY"
            
        elif self.current_mode == "SCALP":
            buy_s = (1 if rsi < CONFIG["scalp_rsi_buy_max"] else 0) + (1 if current_price <= bb_lower * 1.01 else 0) + (1 if b_buy else 0) + (1 if fr < 0 else 0)
            sell_s = (1 if rsi > CONFIG["scalp_rsi_sell_min"] else 0) + (1 if current_price >= bb_upper * 0.99 else 0) + (1 if b_sell else 0) + (1 if fr > 0 else 0)
            
            if can_buy and buy_s >= dynamic_scalp_score and current_price > ema * 0.998: signal = "BUY"
            elif can_sell and sell_s >= dynamic_scalp_score and current_price < ema * 1.002: signal = "SELL"

        if signal != "WAIT":
            self.entry_rsi = rsi; self.entry_adx = adx
            logger.info(f"[SIGNAL FIRED] {signal} | P=${current_price:.0f} EMA=${ema:.0f} RSI={rsi:.1f} ADX={adx:.1f}")
        return signal

    def _on_ws_message(self, ws, message):
        try:
            data = json.loads(message)
            if data.get("channel") == "ticker":
                for item in data.get("data", []):
                    if item.get("s") == CONFIG["symbol"]: self.latest_btc_price = float(item["c"])
        except Exception: pass

    def _ws_maintainer(self):
        while not self._stop_ws.is_set():
            self._stop_ping.clear()
            self._ws = websocket.WebSocketApp(
                self.ws_url,
                on_open=lambda w: w.send(json.dumps({"op": "subscribe", "params": {"channel": "ticker", "symbols": [CONFIG["symbol"]]}})),
                on_message=self._on_ws_message,
                on_error=lambda w, e: logger.error(f"WS Error: {e}"),
                on_close=lambda w, c, m: logger.warning("WS Closed. Reconnecting...")
            )
            threading.Thread(target=self._pinger, daemon=True).start()
            self._ws.run_forever()
            self._stop_ping.set()
            time.sleep(5)

    def _pinger(self):
        while not self._stop_ping.is_set() and not self._stop_ws.is_set():
            time.sleep(30)
            if self._ws and self._ws.sock and self._ws.sock.connected:
                try: self._ws.send(json.dumps({"op": "ping"}))
                except Exception: break

    def run_strategy(self):
        self._stop_ws.clear()
        threading.Thread(target=self._ws_maintainer, daemon=True).start()
        self._fetch_macro_schedule()
        
        wait_start = time.time()
        while self.latest_btc_price is None:
            if time.time() - wait_start > 30:
                try:
                    res = self._public.get(f"{self.rest_url}/markets/tickers", params={"symbol": CONFIG["symbol"], "t": int(time.time() * 1000)}, timeout=5).json()
                    data = res.get("data", [])
                    if data: self.latest_btc_price = float(data[0].get("lastPx", 0))
                except Exception: pass
                wait_start = time.time()
            time.sleep(1)
            
        logger.info(f"監視開始: ${self.latest_btc_price:.2f}")
        loop_c, last_h = 0, time.time()
        
        while True:
            try:
                loop_c += 1
                if time.time() - last_h >= 60:
                    ind = self.current_indicators
                    if ind: logger.info(f"[HEARTBEAT] loop={loop_c} price=${self.latest_btc_price} state={self.state} | EMA={ind['ema']:.0f} ADX={ind['adx']:.1f} RSI={ind['rsi']:.1f}")
                    last_h = time.time()
                    
                self._check_session()
                cur_p = self.latest_btc_price
                if cur_p is None: time.sleep(1); continue
                if time.time() - self.last_macro_fetch > CONFIG["macro_fetch_interval"]: self._fetch_macro_schedule()
                
                self._get_market_data()
                synced = self._sync_position()

                # 🚨 絶対防衛の関所 (Reactive Invariant)
                if self.state != "PANIC":
                    if not self._check_exposure_invariant(cur_p, synced):
                        time.sleep(10)
                        continue

                # 💠 OMS State Machine 💠
                if self.state == "FLAT":
                    if synced:
                        self._reconcile_position(synced)
                    else:
                        if time.time() - self.last_close_time >= CONFIG["cooldown_minutes"] * 60:
                            sig = self._analyze(cur_p)
                            if sig != "WAIT":
                                target_size = self._calculate_position_size(
                                    self.current_atr * CONFIG["stop_loss_atr_mult"] if self.current_atr else 0,
                                    risk_override=getattr(self, 'current_dynamic_risk', None)
                                )
                                
                                # 🚨 PRE_TRADE_CHECK: 実効露出の事前承認
                                eff_exp = self._get_effective_exposure()
                                if eff_exp + target_size > CONFIG["max_size"] * 1.05:
                                    logger.warning(f"🚫 [REJECT] Effective Exposure ({eff_exp:.4f} BTC) が超過。発注を拒否し残骸をパージします。")
                                    self._cancel_all_orders()
                                    self.state = "WAIT_CANCEL"
                                    self.cancel_wait_start = time.time()
                                    continue
                                
                                if target_size > 0:
                                    if self._place_order(sig, cur_p, target_size):
                                        self.state = "PENDING_ENTRY"
                                        self.pending_order_time = time.time()

                elif self.state == "PENDING_ENTRY":
                    if synced:
                        fill_delay = time.time() - self.pending_order_time
                        logger.info(f"✅ 約定確認 (Fill Delay: {fill_delay:.1f}秒)")
                        self._reconcile_position(synced)
                        self.pending_cl_ord_id = None
                    else:
                        timeout = 45
                        if time.time() - self.pending_order_time > timeout:
                            logger.warning(f"⏳ GTC Timeout ({timeout}秒). キャンセルを送信し WAIT_CANCEL へ移行します。")
                            self._cancel_all_orders()
                            self.state = "WAIT_CANCEL"
                            self.cancel_wait_start = time.time()

                elif self.state == "WAIT_CANCEL":
                    if synced and synced["size"] > 0:
                        logger.warning(f"⚠️ キャンセル待機中に部分約定(または遅延約定)を検知。サイズ: {synced['size']} BTC。IN_POSITIONへ移行します。")
                        self._reconcile_position(synced)
                        self.pending_cl_ord_id = None
                        continue

                    try:
                        state_res = self._sodex.get(f"{self.rest_url}/accounts/{self.wallet_address}/state", params={"accountID": self.account_id, "t": int(time.time() * 1000)}, timeout=5).json()
                        open_orders = state_res.get("data", {}).get("orders", []) or []
                        is_still_alive = any(o.get("cl") == self.pending_cl_ord_id for o in open_orders)
                        
                        if not is_still_alive:
                            logger.info("✅ 板からの消失を確認。ゴースト約定を警戒し SETTLEMENT_PENDING(検疫) へ移行します。")
                            self.state = "SETTLEMENT_PENDING"
                            self.settlement_start_time = time.time()
                        else:
                            if time.time() - self.cancel_wait_start > 30:
                                logger.error("❌ 30秒経過しても板から消えません。再度キャンセルを送信します。")
                                self._cancel_all_orders()
                                self.cancel_wait_start = time.time()
                    except Exception as e: logger.error(f"WAIT_CANCEL 確認エラー: {e}")

                elif self.state == "SETTLEMENT_PENDING":
                    if synced and synced["size"] > 0:
                        logger.critical("👻 CANCEL GHOST DETECTED: 板から消えた後の遅延約定を捕捉！IN_POSITIONへ移行し保護します。")
                        self._reconcile_position(synced)
                        self.pending_cl_ord_id = None
                        continue
                        
                    if time.time() - getattr(self, 'settlement_start_time', 0) > CONFIG.get("settlement_quarantine_seconds", 10):
                        logger.info("🛡️ 検疫期間クリア。遅延約定なしを確認し FLAT に復帰します。")
                        self.state = "FLAT"
                        self.pending_cl_ord_id = None
                        self.last_close_time = time.time() - (CONFIG["cooldown_minutes"] * 60) + 15

                elif self.state == "IN_POSITION":
                    if not synced:
                        self._reconcile_position(synced)
                    else:
                        self._reconcile_position(synced)
                        self._manage_position(cur_p)

            except Exception as e: logger.error(f"例外: {e}", exc_info=True)
            time.sleep(5)

if __name__ == "__main__":
    required_env = ["SODEX_API_KEY", "SODEX_PRIVATE_KEY", "SODEX_ACCOUNT_ID"]
    missing = [k for k in required_env if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"必須の環境変数が設定されていません -> {missing}")

    bot = SodexAdvancedBotV2(
        api_key=os.getenv("SODEX_API_KEY"), 
        private_key_hex=os.getenv("SODEX_PRIVATE_KEY"), 
        account_id=int(os.getenv("SODEX_ACCOUNT_ID")), 
        wallet_address=os.getenv("SODEX_WALLET_ADDRESS"), 
        is_testnet=os.getenv("SODEX_TESTNET", "false").lower() == "true"
    )
    bot.run_strategy()