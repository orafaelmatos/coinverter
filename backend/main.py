import time
from datetime import datetime, timedelta
from typing import Dict

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Config ---
CACHE_DURATION = 900
cache_rates: Dict[str, float] = {}
cache_time = 0
cache_history: Dict[str, dict] = {}
cache_history_time: Dict[str, float] = {}
btc_cache = None
btc_cache_time = 0

# --- SGS codes for currencies ---
CURRENCY_CODES = {
    "USD": 1,
    "EUR": 21619,
    "GBP": 21623,
}

# --- Utils ---
def fetch_btc_rate():
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=brl"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data['bitcoin']['brl']
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch BTC rate: {e}")

def fetch_btc_history(days=30):
    url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
    params = {"vs_currency": "brl", "days": days}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        result = {}
        for timestamp, price in data.get("prices", []):
            date = datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d")
            result[date] = {"BRL": price}
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch BTC history: {e}")


def fetch_bacen_rate(series_id=1, days=30):
    end_date = datetime.today()
    start_date = end_date - timedelta(days=days)

    url = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{series_id}/dados"
    params = {
        "formato": "json",
        "dataInicial": start_date.strftime("%d/%m/%Y"),
        "dataFinal": end_date.strftime("%d/%m/%Y"),
    }
    headers = {
        "Accept": "application/json"
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()

        if not data or len(data) == 0:
            raise Exception("No data returned from BACEN.")

        last = data[-1]
        valor_str = last.get("valor")
        if valor_str is None:
            raise Exception("Valor field missing in BACEN data.")

        valor_float = float(valor_str.replace(",", "."))

        if valor_float == -1:
            raise Exception("BACEN returned error value -1.")

        return valor_float
    except requests.exceptions.HTTPError as http_err:
        raise HTTPException(status_code=r.status_code, detail=f"Failed to fetch Bacen rate: {http_err}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch Bacen rate: {e}")



def fetch_bacen_history(code: int, start_date: str, end_date: str):
    url = (
        f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados"
        f"?formato=json&dataInicial={start_date}&dataFinal={end_date}"
    )
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data:
            raise Exception("Empty historical data from Bacen")
        result = {}
        for entry in data:
            date = datetime.strptime(entry['data'], "%d/%m/%Y").strftime("%Y-%m-%d")
            result[date] = {"BRL": float(entry['valor'].replace(",", "."))}
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch Bacen history: {e}")


@app.get("/")
def root():
    return {"status": "Bacen FX API is running"}

def fetch_btc_rate_cached():
    global btc_cache, btc_cache_time
    now = time.time()
    if btc_cache and now - btc_cache_time < CACHE_DURATION:
        return btc_cache
    value = fetch_btc_rate()
    btc_cache = value
    btc_cache_time = now
    return value


@app.get("/rate/{currency}")
def get_rates(currency):
    global cache_time
    currency = currency.upper()
    now = time.time()
    
    now = time.time()
    if cache_rates and now - cache_time < CACHE_DURATION and currency in cache_rates:
        return {currency: cache_rates[currency]}
    
    if currency == "BTC":
        value = fetch_btc_rate_cached()
    elif currency in CURRENCY_CODES:
        value = fetch_bacen_rate(CURRENCY_CODES[currency])
    else:
        raise HTTPException(status_code=400, detail="Unsupported currency")
    
    cache_rates[currency] = value
    cache_time = now

    return {currency: value}


@app.get("/history")
def get_history(base: str = "USD", days: int = 30):
    global cache_history, cache_history_time
    base = base.upper()

    if base != "BTC" and base not in CURRENCY_CODES:
        raise HTTPException(status_code=400, detail="Unsupported currency")
    if not (1 <= days <= 365):
        raise HTTPException(status_code=400, detail="Days must be between 1 and 365")

    key = f"{base}_{days}"
    now = time.time()
    if key in cache_history and (now - cache_history_time.get(key, 0) < CACHE_DURATION):
        return cache_history[key]

    if base == "BTC":
        result = fetch_btc_history(days)
    else:
        end_date = datetime.today().date()
        start_date = end_date - timedelta(days=days)
        result = fetch_bacen_history(
            code=CURRENCY_CODES[base],
            start_date=start_date.strftime("%d/%m/%Y"),
            end_date=end_date.strftime("%d/%m/%Y"),
        )

    cache_history[key] = result
    cache_history_time[key] = now
    return result

from fastapi import Query


@app.get("/convert")
def convert_amount(
    from_currency: str = Query(..., min_length=3, max_length=3),
    to_currency: str = Query(..., min_length=3, max_length=3),
    amount: float = Query(..., gt=0),
):
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()

    supported_currencies = {"USD", "EUR", "GBP", "BTC", "BRL"}
    if from_currency not in supported_currencies or to_currency not in supported_currencies:
        raise HTTPException(status_code=400, detail="Unsupported currency")

    def get_rate(currency):
        if currency == "BRL":
            return 1.0
        if currency == "BTC":
            return fetch_btc_rate_cached()
        elif currency in CURRENCY_CODES:
            return fetch_bacen_rate(CURRENCY_CODES[currency])
        else:
            raise HTTPException(status_code=400, detail="Unsupported currency")

    from_rate = get_rate(from_currency)
    to_rate = get_rate(to_currency)

    if from_rate is None or to_rate is None:
        raise HTTPException(status_code=400, detail="Rate not found")

    amount_in_brl = amount * from_rate
    converted_amount = amount_in_brl / to_rate

    return {"converted_amount": converted_amount}