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
import threading

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

# Globální cache pro výsledky
results_cache = {
    'high_rsi': [],
    'low_rsi': [],
    'last_update': None
}

# Kontrola API klíčů
api_key = os.getenv('BINANCE_API_KEY')
api_secret = os.getenv('BINANCE_API_SECRET')

if not api_key or not api_secret:
    logger.error("API klíče nejsou nastaveny v proměnných prostředí!")
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
        high_rsi_results = []  # Pro RSI >= 65 (možný SHORT)
        low_rsi_results = []   # Pro RSI <= 28 (možný LONG)
        processed = 0
        
        # Získání futures symbolů
        logger.info("Získávám seznam futures symbolů...")
        futures_exchange_info = client.futures_exchange_info()
        symbols = [s['symbol'] for s in futures_exchange_info['symbols'] 
                  if s['status'] == 'TRADING' and s['contractType'] == 'PERPETUAL' and s['symbol'].endswith('USDT')]
        
        # Omezení pouze na USDT páry a maximálně 100 symbolů pro dodržení časového limitu
        symbols = [s for s in symbols if 'USDT' in s][:100]
        
        total_symbols = len(symbols)
        logger.info(f"Nalezeno {total_symbols} futures párů ke zpracování")
        
        for symbol in symbols:
            try:
                processed += 1
                logger.info(f"Zpracovávám {symbol} ({processed}/{total_symbols})")
                
                # Získání dat
                klines = client.futures_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=100)
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
                
                # Kontrola podmínek pro RSI
                if rsi >= 65:  # Signál pro možný SHORT
                    logger.info(f"✓ Nalezen {symbol} s RSI {rsi:.2f} (možný SHORT)")
                    high_rsi_results.append({
                        'symbol': symbol,
                        'rsi': round(rsi, 2),
                        'price': f"${current_price:.4f}"
                    })
                elif rsi <= 28:  # Signál pro možný LONG
                    logger.info(f"✓ Nalezen {symbol} s RSI {rsi:.2f} (možný LONG)")
                    low_rsi_results.append({
                        'symbol': symbol,
                        'rsi': round(rsi, 2),
                        'price': f"${current_price:.4f}"
                    })
                
                # Aktualizace cache po každých 10 zpracovaných párech
                if processed % 10 == 0:
                    # Seřazení výsledků
                    high_rsi_sorted = sorted(high_rsi_results, key=lambda x: x['rsi'], reverse=True)
                    low_rsi_sorted = sorted(low_rsi_results, key=lambda x: x['rsi'])
                    
                    # Aktualizace globální cache
                    global results_cache
                    results_cache['high_rsi'] = high_rsi_sorted
                    results_cache['low_rsi'] = low_rsi_sorted
                    results_cache['last_update'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    
                    logger.info(f"Cache aktualizována po zpracování {processed} párů")
                
                # Kratší pauza mezi API calls
                time.sleep(0.02)  # Zkrátíme pauzu z 0.05 na 0.02
                
            except Exception as e:
                logger.error(f"Chyba při zpracování {symbol}: {str(e)}")
                continue
        
        logger.info(f"Dokončeno zpracování všech {total_symbols} symbolů")
        logger.info(f"Nalezeno {len(high_rsi_results)} symbolů s RSI >= 65 (možný SHORT)")
        logger.info(f"Nalezeno {len(low_rsi_results)} symbolů s RSI <= 28 (možný LONG)")
        
        # Finální aktualizace cache
        high_rsi_sorted = sorted(high_rsi_results, key=lambda x: x['rsi'], reverse=True)
        low_rsi_sorted = sorted(low_rsi_results, key=lambda x: x['rsi'])
        
        global results_cache
        results_cache['high_rsi'] = high_rsi_sorted
        results_cache['low_rsi'] = low_rsi_sorted
        results_cache['last_update'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        return {
            'high_rsi': high_rsi_sorted,  # Pro SHORT
            'low_rsi': low_rsi_sorted     # Pro LONG
        }
    except Exception as e:
        logger.error(f"Hlavní chyba při získávání futures dat: {str(e)}")
        return {'high_rsi': [], 'low_rsi': []}

# Funkce pro spuštění na pozadí
def background_update():
    while True:
        try:
            logger.info("Spouštím aktualizaci dat na pozadí")
            get_futures_data()
            logger.info("Aktualizace dat dokončena, čekám 60 sekund")
            time.sleep(60)  # Aktualizace každou minutu
        except Exception as e:
            logger.error(f"Chyba při aktualizaci na pozadí: {str(e)}")
            time.sleep(60)  # I v případě chyby počkáme minutu

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_rsi_data')
def get_rsi_data():
    logger.info("Požadavek na RSI data")
    
    global results_cache
    if results_cache['last_update'] is None:
        # První požadavek - vrátíme prázdná data, backend začne ihned zpracovávat
        if not hasattr(app, 'background_thread_started') or not app.background_thread_started:
            logger.info("Spouštím první aktualizaci na pozadí")
            background_thread = threading.Thread(target=background_update)
            background_thread.daemon = True
            background_thread.start()
            app.background_thread_started = True
        return jsonify({'high_rsi': [], 'low_rsi': []})
    
    # Vrátíme data z cache
    logger.info(f"Vracím data z cache, poslední aktualizace: {results_cache['last_update']}")
    return jsonify({
        'high_rsi': results_cache['high_rsi'],
        'low_rsi': results_cache['low_rsi'],
        'last_update': results_cache['last_update']
    })

@app.route('/test_data')
def test_data():
    logger.info("Požadavek na testovací data")
    
    # Vrátíme statická testovací data
    test_data = {
        'high_rsi': [
            {'symbol': 'BTCUSDT', 'rsi': 75.25, 'price': '$65,432.10'},
            {'symbol': 'ETHUSDT', 'rsi': 72.18, 'price': '$3,245.67'},
            {'symbol': 'ADAUSDT', 'rsi': 68.42, 'price': '$0.5678'}
        ],
        'low_rsi': [
            {'symbol': 'XRPUSDT', 'rsi': 26.75, 'price': '$0.4321'},
            {'symbol': 'DOGEUSDT', 'rsi': 22.33, 'price': '$0.1234'}
        ],
        'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    
    return jsonify(test_data)

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5002))
    
    # Nastartujeme background thread pro aktualizaci dat
    if not hasattr(app, 'background_thread_started') or not app.background_thread_started:
        logger.info("Spouštím aktualizaci na pozadí")
        background_thread = threading.Thread(target=background_update)
        background_thread.daemon = True
        background_thread.start()
        app.background_thread_started = True
    
    app.run(host='0.0.0.0', port=port, debug=False) 