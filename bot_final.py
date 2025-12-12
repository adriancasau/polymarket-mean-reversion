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

# VARIABLES A TOCAR SI ES NECESARIO
shares = 22.1
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
        msg = f"[Price] Error HTTP para token_id={token_id}, side={side}: {e}"
        print(msg)
        return None

    price = data.get("price")
    if price is None:
        msg = f"[Price] Respuesta sin 'price' para token_id={token_id}, side={side}: {data}"
        print(msg)
        send_telegram_message(msg)
        return None

    try:
        return float(price)
    except (TypeError, ValueError) as e:
        msg = f"[Price] 'price' no convertible a float ({price}) para token_id={token_id}, side={side}: {e}"
        print(msg)
        send_telegram_message(msg)
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

    msg = f"[Gamma] Max reintentos alcanzado para slug={slug}, devolviendo None"
    print(msg)
    send_telegram_message(msg)
    return None


def get_tokens(unix_time: int) -> tuple[str, str]:
    """
    Devuelve (TokenUp, TokenDown) o (None, None) si hay problemas.
    """
    market = fetch_gamma_market(unix_time)
    if market is None:
        return None, None

    lista = json.loads(market['clobTokenIds'])
    return (lista[0], lista[1])


def get_resolution(unix_time: int) -> int:
    """
    Devuelve 1 (Up), -1 (Down) o 0 (no finalizado / no determinable).
    Usa fetch_gamma_market para evitar errores de red.
    """
    market = fetch_gamma_market(unix_time)
    if market is None:
        return 0

    closed = market.get('closed', False)
    if closed:
        try:
            outcome_raw = market['outcomePrices']
            prices = json.loads(outcome_raw)  # p.ej. ["1", "0"] o ["0","1"]
            first_price = float(prices[0])
        except (KeyError, json.JSONDecodeError, TypeError, ValueError, IndexError) as e:
            msg = f"[Resolution] Error parseando outcomePrices para unix={unix_time}: {e}, data={market}"
            print(msg)
            send_telegram_message(msg)
            return 0

        # Imitamos la lógica antigua: 1 → Up, 0 → Down
        return 1 if int(first_price) == 1 else -1
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
        except Exception as e:
            msg = f"[Cancel] Error al cancelar orden {oid}: {type(e).__name__}: {e}"
            print(msg)
            send_telegram_message(msg)
        finally:
            open_orders.pop(oid, None)


def run_signal_watcher():
    last_alert_slot = None  # para no repetir alerta en la misma vela

    while True:
        try:
            now_es = datetime.now(ZoneInfo('Europe/Madrid'))
            unix_now = int(now_es.timestamp())

            # Antes de nada, cancelar órdenes expiradas
            cancel_expired_orders(unix_now)

            slot_actual = unix_now - (unix_now % 900)  # inicio vela actual
            slot_cierre = slot_actual + 900            # fin vela actual
            segundos_para_cierre = slot_cierre - unix_now

            # 1) Si faltan más de X segundos, duerme hasta X segundos antes
            if segundos_para_cierre > tiempo_antes_cerrar:
                dormir = segundos_para_cierre - tiempo_antes_cerrar
                print(f"Faltan {segundos_para_cierre}s para el cierre, duermo {dormir}s hasta la ventana de señal...")
                time.sleep(dormir)
                continue

            # 2) Estamos en los últimos X segundos de la vela → mirar señales
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
                            token = upToken if direccion == 'UP' else downToken
                            resp = buy_with_price_cap(token, 0.51, shares)

                            if resp is not None and isinstance(resp, dict) and resp.get("orderID"):
                                order_id = resp["orderID"]
                                open_orders[order_id] = expiration_ts

                                msg = (
                                    f"Señal para la PRÓXIMA vela de 15m.\n"
                                    f"✅ GTC colocada, Dirección: {direccion}\n"
                                    f"Slot actual: {slot_actual} (cierra en {segundos_para_cierre} s)\n"
                                    f"Próxima vela (slot): {slot_siguiente}\n"
                                    f"OrderID: {order_id}"
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

        except Exception as e:
            # Cualquier error inesperado de la loop entera se captura aquí
            msg = f"[Loop] Error inesperado en run_signal_watcher: {type(e).__name__}: {e}"
            print(msg)
            send_telegram_message(msg)
            time.sleep(5)  # pequeño respiro para evitar bucles locos


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
        # Aquí no relanzamos nada para no crear bucles de error infinitos
        print(f"Error enviando mensaje de Telegram: {e}")


def buy_with_price_cap(token_id: str, max_price: float, max_size: float):
    """
    Compra limit con tope max_price (GTC) con reintentos ante errores transitorios (p.ej. PolyApiException 500).
    - Nunca pagarás más de max_price.
    - Reintenta con backoff exponencial.
    """
    order = OrderArgs(
        token_id=token_id,
        price=max_price,
        size=max_size,
        side=BUY,
    )

    MAX_RETRIES = 6
    BASE_SLEEP = 1.0
    CAP_SLEEP = 20.0

    # Creamos la orden firmada una vez (es más estable y evita trabajo repetido)
    signed = client.create_order(order)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return client.post_order(signed, OrderType.GTC)

        except PolyApiException as e:
            # Reintentar solo si parece error transitorio del servidor (500 / execution)
            status = getattr(e, "status_code", None)
            msg_e = str(e)
            retryable = (
                status == 500
                or "status_code=500" in msg_e
                or "could not run the execution" in msg_e
            )

            if retryable and attempt < MAX_RETRIES:
                sleep_s = min(CAP_SLEEP, BASE_SLEEP * (2 ** (attempt - 1)))
                time.sleep(sleep_s)
                continue

            msg = (
                f"No se pudo colocar GTC a {max_price} para token {token_id}: "
                f"{type(e).__name__}: {e}"
            )
            print(msg)
            send_telegram_message(msg)
            return None

        except (RequestException, TimeoutError, ConnectionError) as e:
            # Errores de red: reintentar también
            if attempt < MAX_RETRIES:
                sleep_s = min(CAP_SLEEP, BASE_SLEEP * (2 ** (attempt - 1)))
                time.sleep(sleep_s)
                continue

            msg = (
                f"No se pudo colocar GTC a {max_price} para token {token_id}: "
                f"{type(e).__name__}: {e}"
            )
            print(msg)
            send_telegram_message(msg)
            return None

        except Exception as e:
            # Otros errores: no asumimos que sean retryables
            msg = (
                f"No se pudo colocar GTC a {max_price} para token {token_id}: "
                f"{type(e).__name__}: {e}"
            )
            print(msg)
            send_telegram_message(msg)
            return None

if __name__ == "__main__":
    run_signal_watcher()
