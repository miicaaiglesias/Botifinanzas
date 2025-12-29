import os
import json
import logging
import datetime
import calendar
import asyncio

import httpx
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
from flask import Flask, request
import threading


# -------------------- LOGGING --------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# CARGAR EL ARCHIVO .env  
load_dotenv()

# -------------------- CONFIG --------------------

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SHEET_ID = os.getenv(
    "GOOGLE_SHEETS_KEY",
    "1knw9LC_BQF2LC-oT0cV6jV_ut0JurTrsB1CcmpSxPds",
)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

MOVIMIENTOS_SHEET_NAME = "Movimientos"
PRESUPUESTOS_SHEET_NAME = "Presupuestos"
OBJETIVOS_SHEET_NAME = "Objetivos"


# -------------------- GOOGLE SHEETS --------------------

def get_gspread_client():
    """Crea el cliente de gspread usando JSON en variable o archivo local."""
    json_content = os.getenv("GOOGLE_SERVICE_ACCOUNT")
    if json_content:
        info = json.loads(json_content)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        # Modo local: usa el archivo service_account.json
        creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
    return gspread.authorize(creds)


def get_or_create_worksheet(sh, name, headers=None):
    """Obtiene una hoja por nombre o la crea si no existe."""
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=name, rows=2000, cols=20)
        if headers:
            ws.append_row(headers, value_input_option="USER_ENTERED")
    return ws


# Inicializamos Google Sheets
gc = get_gspread_client()
sh = gc.open_by_key(SHEET_ID)

# Hoja de movimientos (aseguramos encabezados)
movimientos_headers = [
    "Fecha",      # A
    "Usuario",    # B
    "Tipo",       # C (gasto / ingreso)
    "Categoria",  # D
    "Descripcion",# E
    "Monto",      # F
    "Año",        # G
    "Mes",        # H
    "Moneda",     # I (ARS / USD)
]
mov_ws = get_or_create_worksheet(sh, MOVIMIENTOS_SHEET_NAME, movimientos_headers)

# Hoja de presupuestos
presupuestos_headers = ["Categoria", "PresupuestoMensual"]
pres_ws = get_or_create_worksheet(sh, PRESUPUESTOS_SHEET_NAME, presupuestos_headers)

# Hoja de objetivos
objetivos_headers = ["Nombre", "MontoObjetivo"]
obj_ws = get_or_create_worksheet(sh, OBJETIVOS_SHEET_NAME, objetivos_headers)


# -------------------- FUNCIONES CORE --------------------

def add_movimiento(
    tipo: str,
    categoria: str,
    monto: float,
    descripcion: str,
    fecha: datetime.datetime | None = None,
    moneda: str = "ARS",
    usuario: str = "Mica",
):
    """Agrega un movimiento a la hoja de Movimientos."""
    if fecha is None:
        fecha = datetime.datetime.now()

    year = fecha.year
    month = fecha.month
    fecha_str = fecha.strftime("%Y-%m-%d %H:%M:%S")

    row = [
        fecha_str,
        usuario,
        tipo.lower(),
        categoria,
        descripcion,
        float(monto),
        year,
        month,
        moneda.upper(),
    ]

    mov_ws.append_row(row, value_input_option="USER_ENTERED")
    logger.info("Movimiento agregado: %s", row)


def sumar_movimientos_del_mes(year: int, month: int):
    """Devuelve (ingresos, gastos) del mes/año indicado (solo pesos ARS)."""
    registros = mov_ws.get_all_records()

    total_ingresos = 0.0
    total_gastos = 0.0

    for row in registros:
        try:
            anio = int(row.get("Año") or 0)
            mes = int(row.get("Mes") or 0)
        except ValueError:
            continue

        if anio == year and mes == month and (row.get("Moneda") or "ARS").upper() == "ARS":
            monto = float(row.get("Monto") or 0)
            tipo = (row.get("Tipo") or "").lower()
            if tipo == "ingreso":
                total_ingresos += monto
            elif tipo == "gasto":
                total_gastos += monto

    return total_ingresos, total_gastos


def set_presupuesto(categoria: str, monto: float):
    """Crea o actualiza presupuesto mensual por categoría."""
    registros = pres_ws.get_all_records()
    for idx, row in enumerate(registros, start=2):
        if (row.get("Categoria") or "").lower() == categoria.lower():
            pres_ws.update_cell(idx, 2, float(monto))
            return
    pres_ws.append_row([categoria, float(monto)], value_input_option="USER_ENTERED")


def set_objetivo(nombre: str, monto: float):
    """Crea o actualiza objetivo de ahorro."""
    registros = obj_ws.get_all_records()
    for idx, row in enumerate(registros, start=2):
        if (row.get("Nombre") or "").lower() == nombre.lower():
            obj_ws.update_cell(idx, 2, float(monto))
            return
    obj_ws.append_row([nombre, float(monto)], value_input_option="USER_ENTERED")


def parse_movimiento_args(args):
    """
    Espera al menos: categoria monto [descripcion...]
    Devuelve (categoria, monto, descripcion).
    """
    if len(args) < 2:
        raise ValueError("Faltan datos. Usa: categoria monto descripcion")
    categoria = args[0]
    try:
        monto = float(str(args[1]).replace(",", "."))
    except ValueError:
        raise ValueError("El monto no es válido.")

    descripcion = " ".join(args[2:]) if len(args) > 2 else ""
    return categoria, monto, descripcion


def month_add(base_date: datetime.date, offset_months: int) -> datetime.datetime:
    """Suma meses a una fecha sin usar librerías externas."""
    m = base_date.month - 1 + offset_months
    year = base_date.year + m // 12
    month = m % 12 + 1
    day = min(base_date.day, calendar.monthrange(year, month)[1])
    return datetime.datetime(year, month, day)


# -------------------- TELEGRAM API HELPERS --------------------

async def send_message(client: httpx.AsyncClient, base_url: str, chat_id: int, text: str):
    """Envía un mensaje de texto a un chat."""
    try:
        await client.post(
            f"{base_url}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=20.0,
        )
    except Exception as e:
        logger.error("Error enviando mensaje: %s", e)


# -------------------- HANDLERS DE COMANDOS --------------------

async def cmd_start(client, base_url, chat_id, first_name):
    text = (
        f"Hola {first_name or 'Mica'} 👋\n"
        "Soy tu bot de finanzas.\n\n"
        "Comandos disponibles:\n"
        "/gasto categoria monto descripcion\n"
        "/gasto_usd categoria monto descripcion\n"
        "/ingreso categoria monto descripcion\n"
        "/ingreso_usd categoria monto descripcion\n"
        "/cuotas categoria monto descripcion cantidad\n"
        "/resumen - Resumen del mes actual\n"
        "/saldo - Ingresos - Gastos del mes actual\n"
        "/presupuesto categoria monto\n"
        "/objetivo nombre monto\n"
        "/help - Ver este mensaje otra vez"
    )
    await send_message(client, base_url, chat_id, text)


async def cmd_help(client, base_url, chat_id, first_name):
    await cmd_start(client, base_url, chat_id, first_name)


async def cmd_movimiento(client, base_url, chat_id, first_name, args, tipo, moneda):
    try:
        categoria, monto, descripcion = parse_movimiento_args(args)
    except ValueError as e:
        await send_message(
            client,
            base_url,
            chat_id,
            f"❌ {e}\nEjemplo: /{tipo} comida 5000 empanadas",
        )
        return

    add_movimiento(tipo=tipo, categoria=categoria, monto=monto,
                   descripcion=descripcion, moneda=moneda)

    simbolo = "$" if moneda.upper() == "ARS" else "USD "
    await send_message(
        client,
        base_url,
        chat_id,
        f"✅ {tipo.capitalize()} registrado: {simbolo}{monto:.2f} en '{categoria}' ({moneda.upper()}).",
    )


async def cmd_cuotas(client, base_url, chat_id, first_name, args):
    """
    /cuotas categoria monto descripcion cantidad
    Ej: /cuotas hogar 30000 pava electrica 3
    """
    if len(args) < 3:
        await send_message(
            client,
            base_url,
            chat_id,
            "❌ Faltan datos.\nUsa: /cuotas categoria monto descripcion cantidad\n"
            "Ej: /cuotas hogar 30000 pava electrica 3",
        )
        return

    categoria = args[0]

    try:
        monto_total = float(str(args[1]).replace(",", "."))
    except ValueError:
        await send_message(client, base_url, chat_id, "❌ El monto no es válido.")
        return

    try:
        cantidad_cuotas = int(args[-1])
    except ValueError:
        await send_message(
            client,
            base_url,
            chat_id,
            "❌ La cantidad de cuotas debe ser un número entero.\nEj: /cuotas hogar 30000 pava electrica 3",
        )
        return

    if cantidad_cuotas <= 0:
        await send_message(client, base_url, chat_id, "❌ La cantidad de cuotas debe ser mayor a 0.")
        return

    descripcion = " ".join(args[2:-1]) if len(args) > 3 else ""

    monto_cuota = round(monto_total / cantidad_cuotas, 2)
    hoy = datetime.date.today()

    for i in range(cantidad_cuotas):
        fecha_cuota = month_add(hoy, i)
        desc_cuota = f"{descripcion} (cuota {i+1}/{cantidad_cuotas})".strip()
        add_movimiento(
            tipo="gasto",
            categoria=categoria,
            monto=monto_cuota,
            descripcion=desc_cuota,
            fecha=fecha_cuota,
            moneda="ARS",
        )

    await send_message(
        client,
        base_url,
        chat_id,
        f"✅ Compra en cuotas registrada.\n"
        f"Total: ${monto_total:.2f} en {cantidad_cuotas} cuotas de ${monto_cuota:.2f}.",
    )


async def cmd_resumen(client, base_url, chat_id):
    hoy = datetime.date.today()
    ingresos, gastos = sumar_movimientos_del_mes(hoy.year, hoy.month)
    saldo = ingresos - gastos

    texto = (
        f"📅 Resumen de {hoy.month}/{hoy.year} (solo ARS)\n\n"
        f"💰 Ingresos: ${ingresos:,.2f}\n"
        f"💸 Gastos: ${gastos:,.2f}\n"
        f"🧾 Saldo: ${saldo:,.2f}"
    )
    await send_message(client, base_url, chat_id, texto)


async def cmd_saldo(client, base_url, chat_id):
    hoy = datetime.date.today()
    ingresos, gastos = sumar_movimientos_del_mes(hoy.year, hoy.month)
    saldo_valor = ingresos - gastos
    await send_message(
        client,
        base_url,
        chat_id,
        f"💼 Saldo del mes actual (ARS): ${saldo_valor:,.2f}",
    )


async def cmd_presupuesto(client, base_url, chat_id, args):
    if len(args) < 2:
        await send_message(
            client,
            base_url,
            chat_id,
            "❌ Usa: /presupuesto categoria monto\nEj: /presupuesto comida 50000",
        )
        return

    categoria = args[0]
    try:
        monto = float(str(args[1]).replace(",", "."))
    except ValueError:
        await send_message(client, base_url, chat_id, "❌ El monto no es válido.")
        return

    set_presupuesto(categoria, monto)
    await send_message(
        client,
        base_url,
        chat_id,
        f"✅ Presupuesto guardado para '{categoria}': ${monto:.2f} por mes.",
    )


async def cmd_objetivo(client, base_url, chat_id, args):
    if len(args) < 2:
        await send_message(
            client,
            base_url,
            chat_id,
            "❌ Usa: /objetivo nombre monto\nEj: /objetivo viaje_brasil 300000",
        )
        return

    nombre = args[0]
    try:
        monto = float(str(args[1]).replace(",", "."))
    except ValueError:
        await send_message(client, base_url, chat_id, "❌ El monto no es válido.")
        return

    set_objetivo(nombre, monto)
    await send_message(
        client,
        base_url,
        chat_id,
        f"✅ Objetivo '{nombre}' guardado por ${monto:.2f}.",
    )


# -------------------- ROUTER DE UPDATES --------------------

async def handle_update(client: httpx.AsyncClient, base_url: str, update: dict):
    message = update.get("message") or {}
    text = (message.get("text") or "").strip()
    if not text:
        return

    chat_id = message["chat"]["id"]
    user = message.get("from") or {}
    first_name = user.get("first_name", "Mica")

    parts = text.split()
    command = parts[0]
    # Por si viene /comando@NombreDelBot
    if "@" in command:
        command = command.split("@", 1)[0]
    args = parts[1:]

    try:
        if command == "/start":
            await cmd_start(client, base_url, chat_id, first_name)
        elif command == "/help":
            await cmd_help(client, base_url, chat_id, first_name)
        elif command == "/gasto":
            await cmd_movimiento(client, base_url, chat_id, first_name, args, "gasto", "ARS")
        elif command == "/gasto_usd":
            await cmd_movimiento(client, base_url, chat_id, first_name, args, "gasto", "USD")
        elif command == "/ingreso":
            await cmd_movimiento(client, base_url, chat_id, first_name, args, "ingreso", "ARS")
        elif command == "/ingreso_usd":
            await cmd_movimiento(client, base_url, chat_id, first_name, args, "ingreso", "USD")
        elif command == "/cuotas":
            await cmd_cuotas(client, base_url, chat_id, first_name, args)
        elif command == "/resumen":
            await cmd_resumen(client, base_url, chat_id)
        elif command == "/saldo":
            await cmd_saldo(client, base_url, chat_id)
        elif command == "/presupuesto":
            await cmd_presupuesto(client, base_url, chat_id, args)
        elif command == "/objetivo":
            await cmd_objetivo(client, base_url, chat_id, args)
        else:
            await send_message(
                client,
                base_url,
                chat_id,
                "❓ Comando no reconocido. Usa /help para ver las opciones.",
            )
    except Exception as e:
        logger.exception("Error manejando update: %s", e)
        await send_message(
            client,
            base_url,
            chat_id,
            "⚠️ Ocurrió un error procesando el comando. Probá de nuevo.",
        )


# -------------------- MAIN LOOP (LONG POLLING) --------------------

# ... (todo tu código anterior de comandos y handlers queda igual) ...

app = Flask(__name__)

# Creamos un cliente HTTP global para reusar conexiones
async_client = httpx.AsyncClient(timeout=30.0)
base_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

@app.route("/", methods=["GET"])
def home():
    return "Bot de finanzas funcionando OK ✅", 200

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
async def telegram_webhook():
    """Este es el endpoint que Telegram tocará cada vez que haya un mensaje."""
    update = json.loads(request.data)
    
    # Ejecutamos la lógica que ya tenías escrita
    await handle_update(async_client, base_url, update)
    
    return "OK", 200

if __name__ == "__main__":
    # Importante: para Render necesitas que Flask sea asíncrono o manejar el loop
    # Usaremos el servidor de desarrollo de Flask para este ejemplo rápido:
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)