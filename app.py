from flask import Flask, jsonify, render_template
from binance.client import Client
import pandas as pd
import numpy as np
from datetime import datetime
import os
from dotenv import load_dotenv
import logging
import sys
import time

# Nastavení logování
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('app.log')
    ]
)
logger = logging.getLogger(__name__)

# Načtení proměnných prostředí
load_dotenv()

app = Flask(__name__)

# Kontrola API klíčů
api_key = os.getenv('BINANCE_API_KEY')
api_secret = os.getenv('BINANCE_API_SECRET')

if not api_key or not api_secret:
    logger.error("API klíče nejsou nastaveny v .env souboru!")
    sys.exit(1)

logger.info("API klíče načteny úspěšně")

# Inicializace Binance klienta
try:
    client = Client(api_key, api_secret)
    # Test připojení
    client.get_system_status()
    logger.info("Připojení k Binance API úspěšné")
except Exception as e:
    logger.error(f"Chyba při připojení k Binance API: {str(e)}")
    sys.exit(1)

def calculate_rsi(data, periods=14):
    try:
        if len(data) < periods + 1:
            return None
        
        close_prices = pd.to_numeric(data['close'], errors='coerce')
        deltas = close_prices.diff()
        
        gains = deltas.where(deltas > 0, 0.0)
        losses = -deltas.where(deltas < 0, 0.0)
        
        # První průměr - SMA
        first_avg_gain = gains.iloc[:periods].mean()
        first_avg_loss = losses.iloc[:periods].mean()
        
        # Následující průměry - EMA
        avg_gains = [first_avg_gain]
        avg_losses = [first_avg_loss]
        
        for i in range(periods, len(gains)):
            avg_gain = (avg_gains[-1] * (periods - 1) + gains.iloc[i]) / periods
            avg_loss = (avg_losses[-1] * (periods - 1) + losses.iloc[i]) / periods
            avg_gains.append(avg_gain)
            avg_losses.append(avg_loss)
        
        if not avg_gains or not avg_losses:
            return None
            
        rs = avg_gains[-1] / avg_losses[-1] if avg_losses[-1] != 0 else 100
        rsi = 100 - (100 / (1 + rs))
        
        return float(rsi) if not pd.isna(rsi) else None
    except Exception as e:
        logger.error(f"Chyba při výpočtu RSI: {str(e)}")
        return None

def get_futures_data():
    try:
        logger.info("Začínám získávat futures data...")
        results = []
        processed = 0
        
        # Získání futures symbolů
        logger.info("Získávám seznam futures symbolů...")
        futures_exchange_info = client.futures_exchange_info()
        symbols = [s['symbol'] for s in futures_exchange_info['symbols'] 
                  if s['status'] == 'TRADING' and s['contractType'] == 'PERPETUAL']
        
        total_symbols = len(symbols)
        logger.info(f"Nalezeno {total_symbols} futures párů ke zpracování")
        
        for symbol in symbols:
            try:
                processed += 1
                logger.info(f"Zpracovávám {symbol} ({processed}/{total_symbols})")
                
                # Získání dat
                klines = client.futures_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=200)
                if not klines:
                    logger.warning(f"Žádná data pro {symbol}")
                    continue
                
                # Zpracování dat
                df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_av', 'trades', 'tb_base_av', 'tb_quote_av', 'ignore'])
                df['close'] = pd.to_numeric(df['close'])
                
                # Výpočet RSI
                rsi = calculate_rsi(df)
                if rsi is None:
                    logger.warning(f"Nelze vypočítat RSI pro {symbol}")
                    continue
                
                current_price = float(df['close'].iloc[-1])
                
                if rsi >= 65:  # Filtrujeme pouze RSI >= 65
                    logger.info(f"✓ Nalezen {symbol} s RSI {rsi:.2f}")
                    results.append({
                        'symbol': symbol,
                        'rsi': round(rsi, 2),
                        'price': f"${current_price:.4f}"
                    })
                
                # Kratší pauza mezi API calls
                time.sleep(0.05)
                
            except Exception as e:
                logger.error(f"Chyba při zpracování {symbol}: {str(e)}")
                continue
        
        logger.info(f"Dokončeno zpracování všech {total_symbols} symbolů")
        logger.info(f"Nalezeno {len(results)} symbolů s RSI >= 65")
        
        return sorted(results, key=lambda x: x['rsi'], reverse=True)
    except Exception as e:
        logger.error(f"Hlavní chyba při získávání futures dat: {str(e)}")
        return []

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_rsi_data')
def get_rsi_data():
    logger.info("Začínám získávat RSI data")
    start_time = time.time()
    
    try:
        results = get_futures_data()
        elapsed_time = time.time() - start_time
        logger.info(f"Nalezeno {len(results)} párů s RSI >= 65 za {elapsed_time:.1f} sekund")
        return jsonify({'1h': results})
    except Exception as e:
        error_msg = f"Chyba při získávání dat: {str(e)}"
        logger.error(error_msg)
        return jsonify({"error": error_msg}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5002, debug=False) 