from flask import Flask, jsonify, render_template, Response
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
import signal
import atexit
import functools
import requests

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

# Přidáváme CORS hlavičky
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET,PUT,POST,DELETE,OPTIONS'
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# Globální cache pro výsledky
results_cache = {
    'high_rsi': [],
    'low_rsi': [],
    'last_update': None
}

# Kontrola API klíčů
api_key = os.getenv('BINANCE_API_KEY')
api_secret = os.getenv('BINANCE_API_SECRET')

# Inicializace Binance klienta - podporuje i provoz bez API klíčů
try:
    # Pokud jsou k dispozici API klíče, použijeme je
    if api_key and api_secret:
        client = Client(api_key, api_secret)
        logger.info("API klíče načteny úspěšně, používám autentizované API")
    else:
        # Pro veřejné API nepotřebujeme klíče
        client = Client("", "")
        logger.info("Používám veřejné Binance API bez autentizace")
    
    # Modifikujeme timeout pro zvýšení stability
    client.session.request = functools.partial(client.session.request, timeout=30)
    
    # Test připojení
    client.get_system_status()
    logger.info("Připojení k Binance API úspěšné")
except Exception as e:
    logger.error(f"Chyba při připojení k Binance API: {str(e)}")
    # I v případě selhání budeme pokračovat a zkusíme to znovu později
    client = Client("", "")
    client.session.request = functools.partial(client.session.request, timeout=30)
    logger.warning("Nouzová inicializace Binance klienta bez autentizace po selhání")

# Proměnná pro sledování běžícího stavu
running = True

# Proměnná pro sledování změn dat
data_version = 0

# Handler pro graceful shutdown
def shutdown_handler(signum=None, frame=None):
    global running
    logger.info("Přijat signál pro ukončení, provádím graceful shutdown...")
    running = False

# Registrace signal handlerů
signal.signal(signal.SIGINT, shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)

# Registrace funkce pro čistý exit
def cleanup():
    logger.info("Úklid aplikace před ukončením")

atexit.register(cleanup)

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

# Funkce pro získání dat z Binance s mnohem robustnější implementací opakovaných pokusů
def get_futures_data_with_retry(symbol, interval, max_retries=7, initial_delay=1):
    """
    Získá data z Binance s opakovanými pokusy v případě selhání.
    
    Args:
        symbol: Symbol, pro který chceme data
        interval: Časový interval (např. "1h", "15m")
        max_retries: Maximální počet pokusů
        initial_delay: Počáteční zpoždění mezi pokusy v sekundách
    
    Returns:
        List s daty nebo None v případě selhání
    """
    delay = initial_delay
    
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                logger.info(f"Pokus {attempt+1}/{max_retries} o získání {interval} dat pro {symbol}")
            
            if attempt >= 2:
                # Po dvou selhaných pokusech se pokusíme reinicializovat klienta
                client.session = requests.Session()
                client.session.request = functools.partial(client.session.request, timeout=30)
                logger.info(f"Reinicializuji klienta před pokusem {attempt+1}")
            
            klines = client.futures_klines(symbol=symbol, interval=interval, limit=50)
            
            # Ověření, že data mají správný formát
            if not klines or not isinstance(klines, list) or len(klines) < 2:
                logger.warning(f"Získaná data pro {symbol} - {interval} jsou neplatná nebo prázdná")
                if attempt < max_retries - 1:
                    time.sleep(delay * (2 ** attempt))
                    continue
                else:
                    return None
                    
            return klines
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Chyba při získávání {interval} dat pro {symbol}: {error_msg}")
            
            if "IP banned" in error_msg:
                logger.error(f"IP adresa byla dočasně zablokována Binance API. Čekám 5 minut: {error_msg}")
                time.sleep(300)  # Čekáme 5 minut při IP banned
            elif "429" in error_msg or "too many requests" in error_msg.lower(): 
                # Rate limit - exponenciální backoff
                wait_time = delay * (2 ** attempt)
                logger.warning(f"Rate limit dosažen, čekám {wait_time} sekund: {error_msg}")
                time.sleep(wait_time)
            elif "Connection" in error_msg or "Timeout" in error_msg or "timeout" in error_msg.lower():
                # Síťové problémy - zkusíme to znovu s delším timeoutem
                wait_time = delay * (2 ** attempt)
                logger.warning(f"Síťový problém při získávání dat, čekám {wait_time} sekund: {error_msg}")
                time.sleep(wait_time)
            elif attempt < max_retries - 1:
                # Běžná chyba, zkusíme znovu
                wait_time = delay * (2 ** attempt)
                logger.warning(f"Pokus {attempt+1} o získání {interval} dat pro {symbol} selhal: {error_msg}. Zkouším znovu za {wait_time}s")
                time.sleep(wait_time)
            else:
                # Poslední pokus selhal
                logger.error(f"Všechny pokusy o získání {interval} dat pro {symbol} selhaly: {error_msg}")
                return None
    
    return None

# Funkce pro získání seznamu futures symbolů s opakovanými pokusy
def get_futures_symbols_with_retry(max_retries=7, initial_delay=1):
    """
    Získá seznam futures symbolů s opakovanými pokusy v případě selhání.
    
    Args:
        max_retries: Maximální počet pokusů
        initial_delay: Počáteční zpoždění mezi pokusy v sekundách
    
    Returns:
        List se symboly nebo prázdný list v případě selhání
    """
    delay = initial_delay
    
    for attempt in range(max_retries):
        try:
            if attempt > 0:
                logger.info(f"Pokus {attempt+1}/{max_retries} o získání seznamu futures symbolů")
            
            if attempt >= 2:
                # Po dvou selhaných pokusech se pokusíme reinicializovat klienta
                client.session = requests.Session()
                client.session.request = functools.partial(client.session.request, timeout=30)
                logger.info(f"Reinicializuji klienta před pokusem {attempt+1} pro futures symboly")
            
            futures_exchange_info = client.futures_exchange_info()
            
            # Filtrování symbolů
            if not futures_exchange_info or not isinstance(futures_exchange_info, dict) or 'symbols' not in futures_exchange_info:
                logger.warning(f"Získaná data pro futures_exchange_info jsou neplatná nebo prázdná")
                if attempt < max_retries - 1:
                    time.sleep(delay * (2 ** attempt))
                    continue
                else:
                    return []
            
            symbols = [s['symbol'] for s in futures_exchange_info['symbols'] 
                      if s['status'] == 'TRADING' and s['contractType'] == 'PERPETUAL' and s['symbol'].endswith('USDT')]
            
            if not symbols:
                logger.warning("Nepodařilo se najít žádné vhodné futures symboly")
                if attempt < max_retries - 1:
                    logger.info("Zkusím to znovu s jiným přístupem...")
                    # Alternativní přístup - získat všechny USDT páry
                    try:
                        all_tickers = client.futures_ticker()
                        symbols = [t['symbol'] for t in all_tickers if 'USDT' in t['symbol']]
                        if symbols:
                            logger.info(f"Úspěšně načteno {len(symbols)} futures symbolů alternativní metodou")
                            return symbols
                    except Exception as e:
                        logger.error(f"Alternativní metoda také selhala: {str(e)}")
                    
                    time.sleep(delay * (2 ** attempt))
                    continue
                else:
                    return []
            
            logger.info(f"Úspěšně načteno {len(symbols)} futures symbolů")
            return symbols
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Chyba při získávání seznamu futures symbolů: {error_msg}")
            
            if "IP banned" in error_msg:
                logger.error(f"IP adresa byla dočasně zablokována Binance API. Čekám 5 minut: {error_msg}")
                time.sleep(300)  # Čekáme 5 minut při IP banned
            elif "429" in error_msg or "too many requests" in error_msg.lower(): 
                # Rate limit - exponenciální backoff
                wait_time = delay * (2 ** attempt)
                logger.warning(f"Rate limit dosažen, čekám {wait_time} sekund: {error_msg}")
                time.sleep(wait_time)
            elif "Connection" in error_msg or "Timeout" in error_msg or "timeout" in error_msg.lower():
                # Síťové problémy - zkusíme to znovu s delším timeoutem
                wait_time = delay * (2 ** attempt)
                logger.warning(f"Síťový problém při získávání futures symbolů, čekám {wait_time} sekund: {error_msg}")
                time.sleep(wait_time)
            elif attempt < max_retries - 1:
                # Běžná chyba, zkusíme znovu
                wait_time = delay * (2 ** attempt)
                logger.warning(f"Pokus {attempt+1} o získání seznamu futures symbolů selhal: {error_msg}. Zkouším znovu za {wait_time}s")
                time.sleep(wait_time)
            else:
                # Poslední pokus selhal, zkusíme alternativní přístup
                logger.error(f"Všechny pokusy o získání seznamu futures symbolů selhaly: {error_msg}")
                
                # Záchranný mechanismus - zkusíme získat nejběžnější páry ručně
                logger.info("Používám záložní seznam základních párů")
                return ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "MATICUSDT", "AVAXUSDT", "DOTUSDT"]
    
    # Pokud jsme došli sem, všechny pokusy selhaly
    logger.error("Nepodařilo se získat futures symboly žádným způsobem")
    return ["BTCUSDT", "ETHUSDT"]  # Vrátíme alespoň základní páry

def get_futures_data():
    try:
        logger.info("Začínám získávat futures data...")
        global results_cache  # Přidáno - globální proměnná musí být deklarována před použitím
        global running
        global data_version
        
        high_rsi_results = []  # Pro RSI >= 55 (možný SHORT)
        low_rsi_results = []   # Pro RSI <= 28 (možný LONG)
        processed = 0
        
        # Získání futures symbolů
        logger.info("Získávám seznam futures symbolů...")
        symbols = get_futures_symbols_with_retry()
        
        if not symbols:
            logger.error("Nepodařilo se získat seznam symbolů, končím zpracování")
            return {'high_rsi': [], 'low_rsi': []}
        
        total_symbols = len(symbols)
        logger.info(f"Nalezeno {total_symbols} futures párů ke zpracování")
        
        # Rozdělíme páry do skupin po 20, abychom je mohli zpracovávat postupně
        # a aktualizovat cache po každé skupině
        symbol_batches = [symbols[i:i+20] for i in range(0, len(symbols), 20)]
        batch_num = 0
        
        for batch in symbol_batches:
            # Kontrola, zda nemáme ukončit aplikaci
            if not running:
                logger.info("Ukončuji zpracování futures dat - byl požadován shutdown")
                break
                
            batch_num += 1
            logger.info(f"Zpracovávám skupinu {batch_num}/{len(symbol_batches)} ({len(batch)} párů)")
            
            for symbol in batch:
                # Kontrola, zda nemáme ukončit aplikaci
                if not running:
                    break
                    
                try:
                    processed += 1
                    logger.info(f"Zpracovávám {symbol} ({processed}/{total_symbols})")
                    
                    # Získání dat - 1h timeframe
                    klines_1h = get_futures_data_with_retry(symbol, Client.KLINE_INTERVAL_1HOUR)
                    if not klines_1h:
                        logger.warning(f"Žádná 1h data pro {symbol}")
                        continue
                    
                    # Získání dat - 15m timeframe
                    klines_15m = get_futures_data_with_retry(symbol, Client.KLINE_INTERVAL_15MINUTE)
                    if not klines_15m:
                        logger.warning(f"Žádná 15m data pro {symbol}")
                        continue
                    
                    # Získání dat - 1d timeframe
                    klines_1d = get_futures_data_with_retry(symbol, Client.KLINE_INTERVAL_1DAY)
                    if not klines_1d:
                        logger.warning(f"Žádná 1d data pro {symbol}")
                        continue
                    
                    # Zpracování dat - 1h
                    df_1h = pd.DataFrame(klines_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_av', 'trades', 'tb_base_av', 'tb_quote_av', 'ignore'])
                    df_1h['close'] = pd.to_numeric(df_1h['close'])
                    
                    # Zpracování dat - 15m
                    df_15m = pd.DataFrame(klines_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_av', 'trades', 'tb_base_av', 'tb_quote_av', 'ignore'])
                    df_15m['close'] = pd.to_numeric(df_15m['close'])
                    
                    # Zpracování dat - 1d
                    df_1d = pd.DataFrame(klines_1d, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_av', 'trades', 'tb_base_av', 'tb_quote_av', 'ignore'])
                    df_1d['close'] = pd.to_numeric(df_1d['close'])
                    
                    # Výpočet RSI - 1h
                    rsi_1h = calculate_rsi(df_1h)
                    if rsi_1h is None:
                        logger.warning(f"Nelze vypočítat 1h RSI pro {symbol}")
                        continue
                    
                    # Výpočet RSI - 15m
                    rsi_15m = calculate_rsi(df_15m)
                    if rsi_15m is None:
                        logger.warning(f"Nelze vypočítat 15m RSI pro {symbol}")
                        rsi_15m = 0  # Nastavíme na 0, abychom mohli pokračovat
                    
                    # Výpočet RSI - 1d
                    rsi_1d = calculate_rsi(df_1d)
                    if rsi_1d is None:
                        logger.warning(f"Nelze vypočítat 1d RSI pro {symbol}")
                        rsi_1d = 0  # Nastavíme na 0, abychom mohli pokračovat
                    
                    current_price = float(df_1h['close'].iloc[-1])
                    
                    # Kontrola podmínek pro RSI (pouze podle 1h timeframe)
                    if rsi_1h >= 55:  # Signál pro možný SHORT
                        logger.info(f"✓ Nalezen {symbol} s RSI 1h {rsi_1h:.2f}, 15m {rsi_15m:.2f}, 1d {rsi_1d:.2f} (možný SHORT)")
                        high_rsi_results.append({
                            'symbol': symbol,
                            'rsi': round(rsi_1h, 2),
                            'rsi_15m': round(rsi_15m, 2),
                            'rsi_1d': round(rsi_1d, 2),
                            'price': f"${current_price:.4f}"
                        })
                    elif rsi_1h <= 28:  # Signál pro možný LONG
                        logger.info(f"✓ Nalezen {symbol} s RSI 1h {rsi_1h:.2f}, 15m {rsi_15m:.2f}, 1d {rsi_1d:.2f} (možný LONG)")
                        low_rsi_results.append({
                            'symbol': symbol,
                            'rsi': round(rsi_1h, 2),
                            'rsi_15m': round(rsi_15m, 2),
                            'rsi_1d': round(rsi_1d, 2),
                            'price': f"${current_price:.4f}"
                        })
                    
                    # Kratší pauza mezi API calls
                    time.sleep(0.02)  # Zkrátíme pauzu z 0.05 na 0.02
                    
                except Exception as e:
                    logger.error(f"Chyba při zpracování {symbol}: {str(e)}")
                    continue
            
            # Aktualizace cache po každé dokončené skupině párů
            # Seřazení výsledků
            high_rsi_sorted = sorted(high_rsi_results, key=lambda x: x['rsi'], reverse=True)
            low_rsi_sorted = sorted(low_rsi_results, key=lambda x: x['rsi'])
            
            # Aktualizace globální cache
            results_cache['high_rsi'] = high_rsi_sorted
            results_cache['low_rsi'] = low_rsi_sorted
            results_cache['last_update'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # Inkrementace verze dat pro SSE
            data_version += 1
            
            logger.info(f"Cache aktualizována po zpracování skupiny {batch_num}/{len(symbol_batches)} (celkem {processed}/{total_symbols} párů)")
            
            # Krátká pauza mezi skupinami, aby Railway neukončil proces
            time.sleep(1)
        
        logger.info(f"Dokončeno zpracování všech {total_symbols} symbolů")
        logger.info(f"Nalezeno {len(high_rsi_results)} symbolů s RSI >= 55 (možný SHORT)")
        logger.info(f"Nalezeno {len(low_rsi_results)} symbolů s RSI <= 28 (možný LONG)")
        
        # Finální aktualizace cache
        high_rsi_sorted = sorted(high_rsi_results, key=lambda x: x['rsi'], reverse=True)
        low_rsi_sorted = sorted(low_rsi_results, key=lambda x: x['rsi'])
        
        results_cache['high_rsi'] = high_rsi_sorted
        results_cache['low_rsi'] = low_rsi_sorted
        results_cache['last_update'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # Inkrementace verze dat pro SSE při finální aktualizaci
        data_version += 1
        
        return {
            'high_rsi': high_rsi_sorted,  # Pro SHORT
            'low_rsi': low_rsi_sorted     # Pro LONG
        }
    except Exception as e:
        logger.error(f"Hlavní chyba při získávání futures dat: {str(e)}")
        return {'high_rsi': [], 'low_rsi': []}

# Funkce pro spuštění na pozadí
def background_update():
    global results_cache  # Přidáno - globální proměnná musí být deklarována před použitím
    global running
    
    while running:
        try:
            logger.info("Spouštím aktualizaci dat na pozadí")
            get_futures_data()
            logger.info("Aktualizace dat dokončena, čekám 60 sekund")
            
            # Kontrolujeme stav běhu každých 5 sekund
            for _ in range(12):  # 12 x 5 sekund = 60 sekund
                if not running:
                    logger.info("Ukončuji background thread")
                    break
                time.sleep(5)
                
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
            {'symbol': 'BTCUSDT', 'rsi': 75.25, 'rsi_15m': 68.42, 'rsi_1d': 72.33, 'price': '$65,432.10'},
            {'symbol': 'ETHUSDT', 'rsi': 72.18, 'rsi_15m': 55.67, 'rsi_1d': 60.42, 'price': '$3,245.67'},
            {'symbol': 'ADAUSDT', 'rsi': 68.42, 'rsi_15m': 62.33, 'rsi_1d': 65.78, 'price': '$0.5678'}
        ],
        'low_rsi': [
            {'symbol': 'XRPUSDT', 'rsi': 26.75, 'rsi_15m': 31.48, 'rsi_1d': 28.72, 'price': '$0.4321'},
            {'symbol': 'DOGEUSDT', 'rsi': 22.33, 'rsi_15m': 24.72, 'rsi_1d': 25.48, 'price': '$0.1234'}
        ],
        'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    
    return jsonify(test_data)

@app.route('/diagnostics')
def diagnostics():
    """
    Diagnostická koncová cesta pro zjištění stavu serveru a připojení k Binance API
    """
    logger.info("Požadavek na diagnostická data")
    
    diagnostics_data = {
        'server_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'python_version': sys.version,
        'cache_status': {
            'last_update': results_cache['last_update'],
            'high_rsi_count': len(results_cache['high_rsi']),
            'low_rsi_count': len(results_cache['low_rsi'])
        },
        'app_status': {
            'running': running,
            'data_version': data_version,
            'thread_active': hasattr(app, 'background_thread_started') and app.background_thread_started
        }
    }
    
    # Test připojení k Binance API
    binance_status = {'status': 'unknown', 'message': ''}
    try:
        # Zkusíme jednoduchou operaci
        status = client.get_system_status()
        binance_status = {
            'status': 'ok',
            'message': 'Připojení k Binance API je funkční',
            'details': status
        }
        
        # Zkusíme získat jeden pár pro test
        try:
            klines = client.futures_klines(symbol="BTCUSDT", interval=Client.KLINE_INTERVAL_1HOUR, limit=10)
            binance_status['data_access'] = 'ok'
            binance_status['data_sample'] = {'records': len(klines)} if klines else {'records': 0}
        except Exception as e:
            binance_status['data_access'] = 'failed'
            binance_status['data_error'] = str(e)
            
    except Exception as e:
        binance_status = {
            'status': 'error',
            'message': f'Problém s připojením k Binance API: {str(e)}'
        }
    
    diagnostics_data['binance_api'] = binance_status
    
    return jsonify(diagnostics_data)

@app.route('/sse')
def sse():
    return Response(event_stream(), mimetype="text/event-stream")

def event_stream():
    global data_version
    client_version = 0
    
    # Vytvořím globální aplikační kontext pro použití v této funkci
    app_context = app.app_context()
    app_context.push()
    
    try:
        while True:
            # Pokud se změnila verze dat, pošleme aktualizaci
            if data_version > client_version:
                client_version = data_version
                
                # Vytvoříme zprávu s aktuálním časem aktualizace
                msg = {
                    'update_available': True,
                    'last_update': results_cache['last_update'] if results_cache['last_update'] else datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                
                # Nyní jsme v aplikačním kontextu, takže jsonify bude fungovat
                json_data = jsonify(msg).get_data(as_text=True)
                
                # Formát SSE: "data: {json}\n\n"
                yield f"data: {json_data}\n\n"
            
            # Počkáme 1 sekundu před další kontrolou
            time.sleep(1)
    finally:
        # Uvolníme aplikační kontext při ukončení generátoru
        app_context.pop()

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5002))
    
    # Nastartujeme background thread pro aktualizaci dat
    if not hasattr(app, 'background_thread_started') or not app.background_thread_started:
        logger.info("Spouštím aktualizaci na pozadí")
        background_thread = threading.Thread(target=background_update)
        background_thread.daemon = True
        background_thread.start()
        app.background_thread_started = True
    
    # Nastavit Werkzeug logger na WARNING, abychom omezili výpisy
    werkzeug_logger = logging.getLogger('werkzeug')
    werkzeug_logger.setLevel(logging.WARNING)
    
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True) 