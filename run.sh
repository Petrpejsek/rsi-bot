#!/bin/bash

# Skript pro spolehlivé spuštění RSI scanneru
# Automaticky ukončí běžící instanci a spustí novou

# Barvy pro výstup
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

echo -e "${YELLOW}RSI Scanner - Spouštěcí skript${NC}"
echo -e "${YELLOW}===============================${NC}"

echo -e "${GREEN}Kontroluji, zda již neběží instance aplikace...${NC}"
# Ukončení běžící instance aplikace (pokud existuje)
pkill -f "python3 app.py" > /dev/null 2>&1

# Dáme procesům čas na ukončení
sleep 2

# Kontrola, zda je port 5002 volný
if lsof -Pi :5002 -sTCP:LISTEN -t >/dev/null ; then
    echo -e "${RED}Port 5002 je stále obsazen. Pokusím se jej uvolnit...${NC}"
    # Zjištění PID procesu na portu 5002
    PID=$(lsof -Pi :5002 -sTCP:LISTEN -t)
    if [ ! -z "$PID" ]; then
        echo -e "${YELLOW}Ukončuji proces $PID...${NC}"
        kill -9 $PID
        sleep 3
    fi
    
    # Kontrola, zda je port nyní volný
    if lsof -Pi :5002 -sTCP:LISTEN -t >/dev/null ; then
        echo -e "${RED}Nepodařilo se uvolnit port 5002. Zkusím další alternativní porty.${NC}"
        
        # Zkusíme několik portů, dokud nenajdeme volný
        for TEST_PORT in 5003 5004 5005 5006 5007; do
            if ! lsof -Pi :$TEST_PORT -sTCP:LISTEN -t >/dev/null ; then
                echo -e "${GREEN}Použiji alternativní port $TEST_PORT.${NC}"
                PORT=$TEST_PORT
                break
            fi
        done
        
        # Pokud nebyl nalezen žádný volný port, použijeme náhodný vysoký port
        if [ -z "$PORT" ]; then
            PORT=$(( 8000 + RANDOM % 1000 ))
            echo -e "${YELLOW}Všechny standardní porty jsou obsazené. Použiji náhodný port $PORT.${NC}"
        fi
    else
        echo -e "${GREEN}Port 5002 byl úspěšně uvolněn.${NC}"
        PORT=5002
    fi
else
    echo -e "${GREEN}Port 5002 je volný.${NC}"
    PORT=5002
fi

# Krátká pauza pro jistotu
sleep 2

echo -e "${GREEN}Spouštím RSI Scanner na portu $PORT...${NC}"
echo -e "${YELLOW}Sledujte log v terminále pro informace o průběhu.${NC}"
echo -e "${YELLOW}Pro ukončení stiskněte Ctrl+C${NC}"
echo -e "${GREEN}Po spuštění bude aplikace dostupná na adrese: http://localhost:$PORT${NC}"

# Spuštění aplikace s nastavením portu
PORT=$PORT python3 -u app.py

# Tento kód se provede po ukončení aplikace (např. Ctrl+C)
echo -e "${YELLOW}Aplikace byla ukončena.${NC}" 