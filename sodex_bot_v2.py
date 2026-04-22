import os, time, json, uuid, logging, requests, smtplib
import pandas as pd
import ta
from email.mime.text import MIMEText
from eth_account import Account
from web3 import Web3
from dotenv import load_dotenv
from collections import OrderedDict

# 環境パス設定
ENV_PATH = "/home/thankyou_yukiko/sodex-bot/.env"
LOG_PATH = "/home/thankyou_yukiko/sodex-bot/bot_v2.log"

load_dotenv(ENV_PATH)

# ログ設定 (ファイルとコンソール両方出力)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class SodexBot:
    def __init__(self):
        self.api_key    = os.getenv("SODEX_API_KEY")
        self.account    = Account.from_key(os.getenv("SODEX_PRIVATE_KEY"))
        self.account_id = int(os.getenv("SODEX_ACCOUNT_ID"))
        self.gmail_user = os.getenv("GMAIL_USER")
        self.gmail_pass = os.getenv("GMAIL_APP_PASSWORD")
        self.url        = "https://mainnet-gw.sodex.dev/api/v1/perps"
        self.chain_id   = 286623

        # 戦略設定 (ボリュームファーム専用)
        self.cfg = {
            "symbol":           "BTC-USD",
            "interval":         "5m",
            "leverage":         10,
            "order_size":       "0.002",
            "tp_pct":           6.0,
            "sl_pct":           5.0,
            "secure_trigger":   2.5,
            "secure_floor":     1.5,
            "adx_threshold":    35,
            "ema_period":       100,
            "rsi_low":          40,
            "rsi_high":         60,
            "dvol_threshold":   75,
            "cooldown_min":     10,
            "lockout_min":      90,
        }

        # 状態管理
        self.position       = None   # {"side": "BUY"/"SELL", "entry": float}
        self.profit_secured = False
        self.last_close_ts  = 0
        self.consec_loss    = {"BUY": 0, "SELL": 0}
        self.lockout_until  = {"BUY": 0, "SELL": 0}
        self.last_dvol      = 50.0
        self.last_dvol_ts   = 0

    # ── メール通知機能 ───────────────────────────────────────────────
    def notify(self, subject: str, message: str):
        if not self.gmail_user or not self.gmail_pass:
            return
        try:
            msg = MIMEText(message)
            msg['Subject'] = subject
            msg['From'] = self.gmail_user
            msg['To'] = self.gmail_user
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
                server.login(self.gmail_user, self.gmail_pass)
                server.send_message(msg)
        except Exception as e:
            logger.error(f"メール送信エラー: {e}")

    # ── 署名 (Go仕様完全対応版) ──────────────────────────────────────
    def _sign(self, params: OrderedDict, nonce: int) -> str:
        payload = OrderedDict([("type", "newOrder"), ("params", params)])
        payload_json = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)

        keccak = lambda b: Web3.keccak(b)
        domain_sep = keccak(
            keccak(b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)") +
            keccak(b"futures") + keccak(b"1") +
            self.chain_id.to_bytes(32, 'big') + bytes(32)
        )
        action_type_hash = keccak(b"ExchangeAction(bytes32 payloadHash,uint64 nonce)")
        payload_hash = keccak(payload_json.encode('utf-8'))
        nonce_bytes  = nonce.to_bytes(32, 'big')
        struct_hash  = keccak(bytes(action_type_hash) + bytes(payload_hash) + nonce_bytes)
        digest       = keccak(b'\x19\x01' + bytes(domain_sep) + bytes(struct_hash))

        signed  = self.account.unsafe_sign_hash(digest)
        raw_hex = signed.signature.hex()
        raw_hex = raw_hex[2:] if raw_hex.startswith("0x") else raw_hex

        # vバイト変換: Python(1b/1c) → Go(00/01)
        sig_bytes    = bytes.fromhex(raw_hex)
        v_corrected  = sig_bytes[-1] - 27
        final_bytes  = sig_bytes[:-1] + bytes([v_corrected])
        return "0x01" + final_bytes.hex()

    # ── 発注 ──────────────────────────────────────────────────────
    def place_order(self, side: str, price: float, is_close: bool = False) -> bool:
        nonce = int(time.time() * 1000)
        order = OrderedDict([
            ("clOrdID",      str(uuid.uuid4())),
            ("modifier",     1),
            ("side",         1 if side == "BUY" else 2),
            ("type",         1),   # LIMIT
            ("timeInForce",  1),   # GTC
            ("price",        str(int(price))),
            ("quantity",     self.cfg["order_size"]),
            ("reduceOnly",   is_close),
            ("positionSide", 1),
        ])
        params = OrderedDict([
            ("accountID", self.account_id),
            ("symbolID",  1),
            ("orders",    [order]),
        ])
        sig     = self._sign(params, nonce)
        headers = {
            "Content-Type": "application/json",
            "X-API-Key":    self.api_key,
            "X-API-Sign":   sig,
            "X-API-Nonce":  str(nonce),
        }
        body = json.dumps(params, separators=(',', ':'), ensure_ascii=False)
        res  = requests.post(f"{self.url}/trade/orders", headers=headers, data=body, timeout=5)
        result = res.json()
        
        if result.get("code") == 0:
            order_type = "決済" if is_close else "新規"
            logger.info(f"✅ {order_type} ORDER OK: {side} {self.cfg['order_size']} @ {int(price)}")
            # 新規の時だけシンプルな約定メールを送る
            if not is_close:
                self.notify(f"【SoDEX】{order_type}注文約定 ({side})", f"価格: ${int(price)}\nサイズ: {self.cfg['order_size']} BTC")
            return True
        else:
            logger.error(f"❌ ORDER REJECTED: {result}")
            return False

    # ── Kline(ローソク足)取得 ─────────────────────────────────────
    def get_klines(self) -> pd.DataFrame:
        try:
            res  = requests.get(
                f"{self.url}/markets/{self.cfg['symbol']}/klines",
                params={"interval": self.cfg["interval"], "limit": 300},
                timeout=5
            )
            data = res.json()
            if data.get("code") != 0:
                return pd.DataFrame()
            df = pd.DataFrame(data["data"]).rename(columns={
                "t": "timestamp", "o": "open", "h": "high",
                "l": "low",       "c": "close", "v": "volume"
            })
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col])
            return df.sort_values("timestamp").reset_index(drop=True)
        except Exception as e:
            logger.error(f"Kline取得エラー: {e}")
            return pd.DataFrame()

    # ── DVOL(恐怖指数)取得 ────────────────────────────────────────
    def get_dvol(self) -> float:
        if time.time() - self.last_dvol_ts < 900: # 15分キャッシュ
            return self.last_dvol
        try:
            res = requests.get(
                "https://www.deribit.com/api/v2/public/get_index_price?index_name=btcdvol_usdc",
                timeout=5
            )
            self.last_dvol    = res.json()["result"]["index_price"]
            self.last_dvol_ts = time.time()
            logger.info(f"DVOL: {self.last_dvol:.1f}")
        except Exception as e:
            logger.warning(f"DVOL取得失敗: {e}")
        return self.last_dvol

    # ── エントリー判定 (taライブラリ使用版) ───────────────────────
    def check_entry(self, df: pd.DataFrame) -> str:
        now = time.time()
        if now - self.last_close_ts < self.cfg["cooldown_min"] * 60:
            return "WAIT"
            
        dvol = self.get_dvol()
        if dvol > self.cfg["dvol_threshold"]:
            return "WAIT"

        # 指標計算の書き換え部分
        df["ema"] = ta.trend.ema_indicator(df["close"], window=self.cfg["ema_period"])
        df["rsi"] = ta.momentum.rsi(df["close"], window=14)
        df["adx"] = ta.trend.adx(df["high"], df["low"], df["close"], window=14)

        r = df.iloc[-1]
        logger.info(f"${r['close']:.0f} EMA:{r['ema']:.0f} RSI:{r['rsi']:.1f} ADX:{r['adx']:.1f}")

        if r["adx"] < self.cfg["adx_threshold"]:
            return "WAIT"

        now_ms = now * 1000
        rl, rh = self.cfg["rsi_low"], self.cfg["rsi_high"]

        if (r["close"] > r["ema"] and rl < r["rsi"] < rh
                and now_ms > self.lockout_until["BUY"]):
            return "BUY"
        if (r["close"] < r["ema"] and rl < r["rsi"] < rh
                and now_ms > self.lockout_until["SELL"]):
            return "SELL"
        return "WAIT"

    # ── 決済判定 ──────────────────────────────────────────────────
    def check_exit(self, current_price: float) -> str:
        if not self.position:
            return None
        side  = self.position["side"]
        entry = self.position["entry"]
        pnl   = ((current_price - entry) / entry
                  if side == "BUY" else (entry - current_price) / entry)
        roe   = pnl * 100 * self.cfg["leverage"]

        if not self.profit_secured and roe >= self.cfg["secure_trigger"]:
            self.profit_secured = True
            logger.info(f"🔒 利益確保モード (ROE:{roe:.2f}%)")
            self.notify("【SoDEX】🔒 利益確保モード突入", f"現在のROEが +{roe:.2f}% に達しました。")

        if roe >= self.cfg["tp_pct"]:
            return "TAKE_PROFIT"
        if roe <= -self.cfg["sl_pct"]:
            return "STOP_LOSS"
        if self.profit_secured and roe <= self.cfg["secure_floor"]:
            return "SECURE_EXIT"
        return None

    # ── メインループ ──────────────────────────────────────────────
    def run(self):
        logger.info("===== SoDEX Bot Final (Vol Farm) 起動 =====")
        self.notify("【SoDEX】🤖 Bot起動", "GCPにてSoDEX自動取引Botの稼働を開始しました。")
        
        while True:
            try:
                df = self.get_klines()
                if df.empty:
                    time.sleep(10)
                    continue

                current_price = df.iloc[-1]["close"]

                # ポジション保有中 → 決済判定
                if self.position:
                    reason = self.check_exit(current_price)
                    if reason:
                        side       = self.position["side"]
                        close_side = "SELL" if side == "BUY" else "BUY"
                        entry      = self.position["entry"]
                        
                        # 🌟 追加: ROEと推定損益(USD)の計算
                        pnl_raw = ((current_price - entry) / entry) if side == "BUY" else ((entry - current_price) / entry)
                        roe = pnl_raw * 100 * self.cfg["leverage"]
                        pnl_usd = (current_price - entry) * float(self.cfg["order_size"]) * (1 if side == "BUY" else -1)
                        
                        logger.info(f"🚪 決済: {reason} ({close_side} @ {current_price:.0f})")
                        
                        # 決済実行 (is_close=True)
                        if self.place_order(close_side, current_price, is_close=True):
                            
                            # 🌟 追加: StandX風の詳細メール通知
                            reason_jp = {"TAKE_PROFIT": "利確ターゲット", "STOP_LOSS": "損切ライン", "SECURE_EXIT": "利益確保撤退"}.get(reason, reason)
                            self.notify(
                                f"【SoDEX】⚡ 決済トリガー発動 ({reason_jp})", 
                                f"最終ROE: {roe:+.2f}%\n推定損益: {pnl_usd:+.2f} USD\n価格: ${current_price:.2f}"
                            )

                            # 損切り時のロックアウト処理
                            if reason == "STOP_LOSS":
                                self.consec_loss[side] += 1
                                if self.consec_loss[side] >= 2:
                                    self.lockout_until[side] = (
                                        time.time() * 1000 + self.cfg["lockout_min"] * 60 * 1000
                                    )
                                    self.consec_loss[side] = 0
                                    msg = f"🚫 {side} ロックアウト発動 ({self.cfg['lockout_min']}分)"
                                    logger.warning(msg)
                                    self.notify("【SoDEX】警告: ロックアウト発動", msg)
                            else:
                                self.consec_loss[side] = 0
                                
                            self.position       = None
                            self.profit_secured = False
                            self.last_close_ts  = time.time()
                else:
                    signal = self.check_entry(df)
                    if signal in ("BUY", "SELL"):
                        logger.info(f">>> SIGNAL: {signal} @ {current_price:.0f} <<<")
                        if self.place_order(signal, current_price, is_close=False):
                            self.position       = {"side": signal, "entry": current_price}
                            self.profit_secured = False
                time.sleep(30)
            except KeyboardInterrupt:
                logger.info("Bot手動停止")
                break
            except Exception as e:
                logger.error(f"Loop Error: {e}")
                time.sleep(10)

if __name__ == "__main__":
    SodexBot().run()
