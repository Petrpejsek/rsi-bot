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

# Kontrola, zda je port 5002 volný
if lsof -Pi :5002 -sTCP:LISTEN -t >/dev/null ; then
    echo -e "${RED}Port 5002 je stále obsazen. Pokusím se jej uvolnit...${NC}"
    # Zjištění PID procesu na portu 5002
    PID=$(lsof -Pi :5002 -sTCP:LISTEN -t)
    if [ ! -z "$PID" ]; then
        echo -e "${YELLOW}Ukončuji proces $PID...${NC}"
        kill -9 $PID
        sleep 2
    fi
    
    # Kontrola, zda je port nyní volný
    if lsof -Pi :5002 -sTCP:LISTEN -t >/dev/null ; then
        echo -e "${RED}Nepodařilo se uvolnit port 5002. Použiji alternativní port 5003.${NC}"
        PORT=5003
    else
        echo -e "${GREEN}Port 5002 byl úspěšně uvolněn.${NC}"
        PORT=5002
    fi
else
    echo -e "${GREEN}Port 5002 je volný.${NC}"
    PORT=5002
fi

# Krátká pauza pro jistotu
sleep 1

echo -e "${GREEN}Spouštím RSI Scanner na portu $PORT...${NC}"
echo -e "${YELLOW}Sledujte log v terminále pro informace o průběhu.${NC}"
echo -e "${YELLOW}Pro ukončení stiskněte Ctrl+C${NC}"

# Spuštění aplikace s nastavením portu
PORT=$PORT python3 -u app.py

# Tento kód se provede po ukončení aplikace (např. Ctrl+C)
echo -e "${YELLOW}Aplikace byla ukončena.${NC}" 