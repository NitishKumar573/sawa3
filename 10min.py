import json
import logging
import os
import time
from datetime import datetime, timedelta

import pandas as pd
import pyotp
import requests
from SmartApi import SmartConnect
from zoneinfo import ZoneInfo
import pytz

print("Local:", datetime.now())
print("IST:", datetime.now(pytz.timezone("Asia/Kolkata")))

# CONFIG — fill these in
# Groww API credentials.
# CONFIG — fill these in M50848322
API_KEY = "3LjGsQyt"
CLIENT_ID = "M50848322"
PASSWORD = "8581" 
TOTP_SECRET = "C4P6OKR4CY3QHB6DPTYGWLUIC4"     # Base32 secret from SmartAPI TOTP setup

TELEGRAM_BOT_TOKEN = "8805272234:AAFVqOaf2mrYzjqCb7zufjkaWGdwR39f460"
TELEGRAM_CHAT_ID = "926442490"
TELEGRAM_BOT_TOKEN2="8869988041:AAHyS7goXL3TKCJI-g2jNIi_jkMQU6-rcvo"
TELEGRAM_CHAT_ID2 = "7984464288"

DRY_RUN = False       # True = simulate orders only (no real order placed). Set False to go live.
PRODUCT_TYPE = "INTRADAY"   # INTRADAY / DELIVERY / CARRYFORWARD (Angel One naming)
ORDER_VARIETY = "NORMAL"
LOOP_SLEEP_SECONDS = 30      # how often the main loop ticks
STATE_FILE = "bot_state.json"


# 10-Minute candles are fetched every 10 minutes EXCEPT at the ":15" mark, because ":15" is
# already handled by the 1-Hour fetch above and would otherwise just re-read the same
# just-closed 1H candle boundary (e.g. 9:25, 9:35, 9:45, 9:55, 10:05, 10:25, 10:35 ... — never 9:15/10:15/...).
TEN_MIN_FETCH_MINUTES = {5,15,25,35,45,55}
login_time=["9:15","14:00","12:00"]
# LOGGINGgd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("ha_bot.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("ha_bot")

"""
"""

WATCHLIST = [
  {
        "symbol":"SENSEX",
        "exchange":"BSE",
        "token":"99919000",
    }
    
    # Add more symbols here...
]
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("ha_bot.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("ha_bot")
# TELEGRAM
def send_telegram(message: str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
def send_telegram2(message:str):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN2}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID2, "text": message}, timeout=10)
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
# STATE PERSISTENCE
def default_symbol_state():
    return {
        "position": None,                     # dict with entry_price, quantity, stoploss_price, entry_time
        "pending_signal": None,                # "BUY" or "SELL" (1H bias confirmed, waiting for 10min trigger)
        "pending_signal_1h_close_time": None,  # ISO timestamp of the 1H candle close that set the bias
        "last_processed_1h_time": None,        # avoid re-evaluating the same 1H candle repeatedly
        "last_processed_10m_time": None,       # avoid re-evaluating the same 10min candle repeatedly
    }


def reset_symbol_state_keep_position(sym_state):
    """Reset every tracked variable EXCEPT 'position'. Called for every symbol right before
    each fixed-time 1H candle fetch (9:15, 10:15, 11:15, 12:15, 1:15, 2:15)."""
    sym_state["position"]=None
    sym_state["pending_signal"] = None
    sym_state["pending_signal_1h_close_time"] = None
    sym_state["last_processed_1h_time"] = None
    sym_state["last_processed_10m_time"] = None


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            log.info("Loaded existing state from disk (resuming after restart).")
            return data
        except Exception as e:
            log.error(f"Failed to load state file, starting fresh: {e}")
    return {item["symbol"]: default_symbol_state() for item in WATCHLIST}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.error(f"Failed to save state: {e}")
# ANGEL ONE LOGIN
def login():
    obj = SmartConnect(api_key=API_KEY)
    totp = pyotp.TOTP(TOTP_SECRET).now()
    data = obj.generateSession(CLIENT_ID, PASSWORD, totp)
    if not data.get("status"):
        raise RuntimeError(f"Login failed: {data}")
    log.info("Logged in to Angel One SmartAPI.")
    return obj
def fetch_candles(smart_api, token, exchange, interval, lookback_minutes):
    """
    interval: "ONE_HOUR" or "TEN_MINUTE" (Angel One interval codes)
    Returns a DataFrame with columns: time, open, high, low, close, volume
    """
    to_date = datetime.now()
    from_date = to_date - timedelta(minutes=lookback_minutes)
    params = {
        "exchange": exchange,
        "symboltoken": token,
        "interval": interval,
        "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
        "todate": to_date.strftime("%Y-%m-%d %H:%M"),
    }
    
    for attempt in range(3):
        try:
            resp = smart_api.getCandleData(params)
            
            if resp.get("status") and resp.get("data"):
                df = pd.DataFrame(
                    resp["data"], columns=["time", "open", "high", "low", "close", "volume"]
                )
                
                df["time"] = pd.to_datetime(df["time"])
                for col in ["open", "high", "low", "close", "volume"]:
                    df[col] = df[col].astype(float)
                return df
                
            else:
                log.warning(f"Candle fetch returned no data (attempt {attempt+1}): {resp}")
        except Exception as e:
            log.error(f"Candle fetch error (attempt {attempt+1}): {e}")
            time.sleep(10)
        time.sleep(1)
    return None


def to_heikin_ashi(df):
    """Convert a normal OHLC dataframe to Heikin-Ashi OHLC."""
    ha = df.copy().reset_index(drop=True)
    ha["ha_close"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    ha_open = [(df["open"].iloc[0] + df["close"].iloc[0]) / 2.0]
    for i in range(1, len(df)):
        ha_open.append((ha_open[i - 1] + ha["ha_close"].iloc[i - 1]) / 2.0)
    ha["ha_open"] = ha_open
    ha["ha_high"] = ha[["ha_open", "ha_close"]].join(df["high"]).max(axis=1)
    ha["ha_low"] = ha[["ha_open", "ha_close"]].join(df["low"]).min(axis=1)
    return ha


def candle_color(open_price, close_price):
    return "GREEN" if close_price >= open_price else "RED"


def is_last_candle_completed(df, interval_minutes):
    """
    The LAST row returned may still be the currently-forming (incomplete) candle. A candle is
    only "completed" once its close-time (open-time + interval) has passed.
    Returns True if df.iloc[-2] is a fully completed candle we can safely evaluate,
    and there are at least 2 rows.
    """
    if df is None or len(df) < 2:
        return False
    last_candle_open = df["time"].iloc[-1]
    last_candle_close_time = last_candle_open + timedelta(minutes=interval_minutes)
    now = datetime.now(last_candle_open.tzinfo) if last_candle_open.tzinfo else datetime.now(ZoneInfo("Asia/Kolkata"))
    return now >= last_candle_close_time


def get_last_completed(df):
    """Return the second-to-last row = latest fully closed candle (last row may be forming)."""
    return df.iloc[-2]
#MARKET HOUR

def market_is_open():
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    open_t = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t and now.weekday() < 5
# CORE STRATEGY LOGIC — per symbol, per tick
def process_symbol(smart_api, symbol_info, state):
    symbol = symbol_info["symbol"]
    sym_state = state[symbol]
    df_10m = fetch_candles(smart_api, symbol_info["token"],symbol_info["exchange"], "TEN_MINUTE", 10 * 8)
    """if not is_last_candle_completed(df_10m, 10):
      print("HGH")
      return"""
    if df_10m is None:
        log.warning(f"{symbol}: No candle data.")
        print("HGH")
        return

    if len(df_10m) < 2:
        log.warning(f"{symbol}: Not enough candles.")
        print("HGH")
        return

    last_10m = get_last_completed(df_10m)
    last_10m_time = str(last_10m["time"])
    #if sym_state["last_processed_10m_time"] == last_10m_time:
        #return  # already evaluated this 10-min candle

    sym_state["last_processed_10m_time"] = last_10m_time
    heiken_ashi=to_heikin_ashi(df_10m)
    c1=heiken_ashi.iloc[-3]
    c2=heiken_ashi.iloc[-4]
    c3=heiken_ashi.iloc[-5]
    c4=heiken_ashi.iloc[-6]
    c5=heiken_ashi.iloc[-7]
    c6=heiken_ashi.iloc[-8]
    cl_color=candle_color(c1["ha_open"],c1["ha_close"])
    c2_color=candle_color(c2["ha_open"],c2["ha_close"])
    c3_color=candle_color(c3["ha_open"],c3["ha_close"])
    c4_color=candle_color(c4["ha_open"],c4["ha_close"])
    c5_color=candle_color(c5["ha_open"],c5["ha_close"])
    c6_color=candle_color(c6["ha_open"],c6["ha_close"])
    
    c0=heiken_ashi.iloc[-2]
    c0_color=candle_color(c0["ha_open"],c0["ha_close"])
    c0_n_color=candle_color(c0["open"],c0["close"])
    

   
    if(c0_color=="RED" and c0_n_color=="RED" ):

        if(cl_color == "GREEN" and c2_color == "GREEN" and c3_color == "GREEN" and c4_color == "GREEN" and c5_color == "GREEN" and c6_color == "GREEN"):
          #print(f"Sell Signal for {symbol}.Current candle is Red and Previous Six candle is Green")
          send_telegram(f"Sell Signal for {symbol}.Current candle is Red and Previous Six candle is Green")
          send_telegram2(f"Sell Signal for {symbol}.Current candle is Red and Previous Six candle is Green")
      
    if(c0_color=="GREEN" and c0_n_color=="GREEN" ):
        if(cl_color == "RED" and c2_color == "RED" and c3_color == "RED" and c4_color == "RED" and c5_color == "RED" and c6_color == "RED"):
          #print(f"Buy Signal for {symbol}.Current candle is Green and Previous Six candle is Red")
          send_telegram(f"Buy Signal for {symbol}.Current candle is Green and Previous Six candle is Red")
          send_telegram2(f"Buy Signal for {symbol}.Current candle is Green and Previous Six candle is Red")

def main():
    log.info(f"Starting bot. DRY_RUN={DRY_RUN}")
    if DRY_RUN:
        log.warning("Running in DRY_RUN mode — no real orders will be placed. Set DRY_RUN=False to go live.")

    smart_api = login()
    state = load_state()
    last_10m_marker = None
    last_10m_marker2 =None
    #send_telegram("🤖 Algo trading bot started (Angel One SmartAPI). Watching: "
     #     + ", ".join(c["symbol"] for c in WATCHLIST
     #     ))
    send_telegram2("🤖 Algo trading bot started (Angel One SmartAPI). Watching: "
           + ", ".join(c["symbol"] for c in WATCHLIST
           ))
    

    while True:
        time.sleep(3)
        
        try:
            if not market_is_open():
                log.info("Market closed. Sleeping 5 minutes.")
                time.sleep(300)
                continue
            #wait_for_next_candle()
            now = datetime.now(ZoneInfo("Asia/Kolkata"))
            current_hm = now.strftime("%M")
            current_hm2 = now.strftime("%H:%M")
            if now.minute in TEN_MIN_FETCH_MINUTES and last_10m_marker != current_hm:
                last_10m_marker = current_hm

                for symbol_info in WATCHLIST:
                    
                    if symbol_info["symbol"] not in state:
                        state[symbol_info["symbol"]] = default_symbol_state()
                    
                    try:
                        
                        process_symbol(smart_api, symbol_info, state)
                    except Exception as e:
                        log.error(f"Error processing {symbol_info['symbol']}: {e}", exc_info=True)
                    time.sleep(1)  # small gap between symbols to respect API rate limits
    
                save_state(state)
                time.sleep(LOOP_SLEEP_SECONDS)
            if current_hm2 in login_time and last_10m_marker2 != current_hm:
                last_10m_marker2 = current_hm2
                login()

        except KeyboardInterrupt:
            log.info("Bot stopped manually.")
            save_state(state)
            break
        except Exception as e:
            log.error(f"Main loop error: {e}", exc_info=True)
            send_telegram(f"⚠️ Bot main loop error: {e}")
            send_telegram2(f"⚠️ Bot main loop error: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
