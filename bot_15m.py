import os 
from requests.exceptions import RequestException
from py_clob_client.exceptions import PolyApiException
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
import json

from dotenv import load_dotenv
load_dotenv()

#VARIABLES A TOCAR SI ES NECESARIO
shares = 13.7
prob_acertar = 0.90
tiempo_antes_cerrar = 600

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet
PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY")
FUNDER = os.getenv("POLY_FUNDER")

client = ClobClient(
    HOST,  # The CLOB API endpoint
    key=PRIVATE_KEY,  # Your wallet's private key
    chain_id=CHAIN_ID,  # Polygon chain ID (137)
    signature_type=2, 
    funder=FUNDER  # Address that holds your funds
    )
client.set_api_creds(client.create_or_derive_api_creds())

open_orders: dict[str, int] = {}

def get_price(token_id: str, side: str) -> float | None:
    """
    Devuelve el mejor precio (float) o None si no se puede obtener.
    """
    url = f"{HOST}/price"
    params = {"token_id": token_id, "side": side}

    try:
        resp = requests.get(url, params=params, timeout=3)
        resp.raise_for_status()
        data = resp.json()
    except RequestException as e:
        print(f"[Price] Error HTTP para token_id={token_id}, side={side}: {e}")
        send_telegram_message(f"[Price] Error HTTP para token_id={token_id}, side={side}: {e}")
        return None

    price = data.get("price")
    if price is None:
        print(f"[Price] Respuesta sin 'price' para token_id={token_id}, side={side}: {data}")
        send_telegram_message(f"[Price] Respuesta sin 'price' para token_id={token_id}, side={side}: {data}")
        return None

    try:
        return float(price)
    except (TypeError, ValueError) as e:
        print(f"[Price] 'price' no convertible a float ({price}) para token_id={token_id}, side={side}: {e}")
        send_telegram_message(f"[Price] 'price' no convertible a float ({price}) para token_id={token_id}, side={side}: {e}")
        return None

def fetch_gamma_market(unix_time: int):
    """
    Devuelve el dict del mercado (response[0]) o None si hay error/red.
    Nunca lanza excepción.
    """
    gamma_url = 'https://gamma-api.polymarket.com/markets'
    slug = f'btc-updown-15m-{unix_time}'
    params = {'slug': slug}

    for attempt in range(3):  # 3 reintentos
        try:
            resp = requests.get(gamma_url, params=params, timeout=3)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                print(f"[Gamma] Mercado vacío para slug={slug}")
                return None
            return data[0]
        except RequestException as e:
            print(f"[Gamma] Error intentando fetch ({attempt+1}/3) slug={slug}: {e}")
            time.sleep(1)

    print(f"[Gamma] Max reintentos alcanzado para slug={slug}, devolviendo None")
    send_telegram_message(f"[Gamma] Max reintentos alcanzado para slug={slug}, devolviendo None")
    return None

def get_tokens(unix_time:int) -> tuple[str, str]: #Devuelve (TokenUp, TokenDown)
    """
    Devuelve (TokenUp, TokenDown) o (None, None) si hay problemas.
    """
    market = fetch_gamma_market(unix_time)
    if market is None:
        return None, None

    lista = json.loads(market['clobTokenIds'])
    return (lista[0], lista[1])

def get_resolution(unix_time:int) -> int: #Devuelve 1(Up), -1 (Down) o 0 (no finalizado)
    gamma_url = 'https://gamma-api.polymarket.com/markets'
    querystring = {'slug':f'btc-updown-15m-{unix_time}'}
    response = requests.get(gamma_url, params=querystring).json()[0]
    closed = response['closed']
    if closed:
        if int(json.loads(response['outcomePrices'])[0]) == 1:
            return 1
        else:
            return -1
    else:
        upToken, downToken = get_tokens(unix_time)
        if upToken is None or downToken is None:
            return 0

        up_price = get_price(upToken, "BUY")
        down_price = get_price(downToken, "BUY")

        if up_price is None or down_price is None:
            # no podemos determinar bien la resolución → no forzamos nada
            return 0

        if up_price >= prob_acertar:
            return 1
        elif down_price >= prob_acertar:
            return -1
        else:
            return 0


def get_signal_for_next_candle(unix_now: int) -> int:
    """
    Mira las 2 últimas velas de 15m.
    Devuelve:
      1  -> señal de ir UP en la siguiente vela
     -1  -> señal de ir DOWN en la siguiente vela
      0  -> sin señal
    """
    slot_actual = unix_now - (unix_now % 900)     # inicio vela actual
    slot_anterior = slot_actual - 900             # inicio vela anterior

    res_actual = get_resolution(slot_actual)
    res_anterior = get_resolution(slot_anterior)

    # Dos velas DOWN seguidas -> ir UP
    if res_actual == -1 and res_anterior == -1:
        return 1

    # Dos velas UP seguidas -> ir DOWN
    if res_actual == 1 and res_anterior == 1:
        return -1

    return 0

def cancel_expired_orders(unix_now: int) -> None:
    """
    Cancela todas las órdenes GTC cuya expiración (fin vela que tradeas) ya haya pasado.
    """
    to_cancel = [oid for oid, exp_ts in open_orders.items() if unix_now >= exp_ts]
    for oid in to_cancel:
        try:
            resp = client.cancel(order_id=oid)
            print(f"[Cancel] Orden {oid} cancelada por expiración. Respuesta: {resp}")
        except PolyApiException as e:
            print(f"[Cancel] Error al cancelar orden {oid}: {e}")
        finally:
            open_orders.pop(oid, None)

def run_signal_watcher():
    last_alert_slot = None  # para no repetir alerta en la misma vela

    while True:
        now_es = datetime.now(ZoneInfo('Europe/Madrid'))
        unix_now = int(now_es.timestamp())

        # Antes de nada, cancelar órdenes expiradas
        cancel_expired_orders(unix_now)

        slot_actual = unix_now - (unix_now % 900)  # inicio vela actual
        slot_cierre = slot_actual + 900            # fin vela actual
        segundos_para_cierre = slot_cierre - unix_now

        # 1) Si faltan más de 5 min, duerme hasta 5 min antes
        if segundos_para_cierre > tiempo_antes_cerrar:
            dormir = segundos_para_cierre - tiempo_antes_cerrar
            print(f"Faltan {segundos_para_cierre}s para el cierre, duermo {dormir}s hasta 5 min antes...")
            time.sleep(dormir)
            continue

        # 2) Estamos en los últimos 5 min de la vela → mirar señales
        if 0 < segundos_para_cierre <= tiempo_antes_cerrar:
            # solo una alerta por vela
            if last_alert_slot != slot_actual:
                signal = get_signal_for_next_candle(unix_now)

                if signal != 0:
                    direccion = "UP" if signal == 1 else "DOWN"

                    # Vela siguiente (la que quieres tradear)
                    slot_siguiente = slot_cierre
                    expiration_ts = slot_siguiente      # timestamp para cancelar orden

                    upToken, downToken = get_tokens(slot_siguiente)
                    if upToken is None or downToken is None:
                        msg = (
                            "Señal para la PRÓXIMA vela de 15m.\n"
                            f"Pero no se han podido obtener tokens para el slot {slot_siguiente}.\n"
                            f"Dirección: {direccion}. No se lanza orden."
                        )
                        print(msg)
                        send_telegram_message(msg)
                    else:
                        if direccion == 'UP':
                            token = upToken
                        else:
                            token = downToken

                        resp = buy_with_price_cap(token, 0.51, shares)

                        if resp is not None and resp.get("orderID"):
                            order_id = resp["orderID"]
                            open_orders[order_id] = expiration_ts

                            msg = (
                                f"Señal para la PRÓXIMA vela de 15m.\n"
                                f"✅ GTC colocada, Dirección: {direccion}\n"
                                f"Slot actual: {slot_actual} (cierra en {segundos_para_cierre} s)\n"
                                f"Próxima vela (slot): {slot_siguiente}\n"
                            )
                        else:
                            msg = (
                                f"Señal para la PRÓXIMA vela de 15m.\n"
                                f"❌ NO se ha podido colocar orden GTC {direccion}.\n"
                                f"Slot actual: {slot_actual} (cierra en {segundos_para_cierre} s)\n"
                                f"Próxima vela (slot): {slot_siguiente}"
                            )

                        print(msg)
                        send_telegram_message(msg)

                    last_alert_slot = slot_actual

            # en esta franja sí interesa checkear a menudo
            time.sleep(0.3)
            continue

        # 3) Si ya ha cerrado la vela (segundos_para_cierre <= 0), pasamos a la siguiente
        if segundos_para_cierre <= 0:
            # pequeño sleep para no quemar CPU mientras cruza de una vela a otra
            time.sleep(1)
            continue

def send_telegram_message(text: str) -> None:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text
    }
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print("Error enviando mensaje de Telegram:", e)

def buy_with_price_cap(token_id: str, max_price: float, max_size: int):
    """
    Compra a mercado con tope max_price.
    - Nunca pagarás más de max_price.
    - La orden puede quedarse en el libro hasta que el bot la cancele.
    """
    order = OrderArgs(
        token_id=token_id,
        price=max_price,   # ← tope de precio (0.51 en tu caso)
        size=max_size,     # nº de shares máximo que quieres
        side=BUY,
    )
    
    signed = client.create_order(order)
    try:
        resp = client.post_order(signed, OrderType.GTC)  #GTD:Good-Till-Date / FOK:Fill-Or-Kill / GTC:Good-Till-Cancelled
        return resp
    except PolyApiException as e:
        print(f"No se pudo colocar GTC a {max_price}: {e}")
        return None

if __name__ == "__main__":
    run_signal_watcher()