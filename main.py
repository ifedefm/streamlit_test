from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import requests
import os
from threading import Thread
import logging
from datetime import datetime
import uuid
from funciones_ganamos import carga_ganamos

app = FastAPI()

# Configuración
ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "APP_USR-5177967231468413-032619-a7b3ab70df053bfb323007e57562341f-324622221")
BASE_URL = os.getenv("BASE_URL", "https://streamlit-test-eiu8.onrender.com")

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Base de datos mejorada
payments_db = {}

@app.post("/crear_pago/")
async def crear_pago(request: Request):
    try:
        data = await request.json()
        usuario_id = data.get("usuario_id")
        monto = data.get("monto")
        email = data.get("email")
        
        if not all([usuario_id, monto, email]):
            raise HTTPException(status_code=400, detail="Se requieren usuario_id, monto y email")

        id_pago_unico = str(uuid.uuid4())
        logger.info(f"Creando pago con ID único: {id_pago_unico}")

        preference_data = {
            "items": [{
                "title": f"Recarga saldo - {usuario_id}",
                "quantity": 1,
                "unit_price": float(monto),
                "currency_id": "ARS"
            }],
            "payer": {"email": email},
            "payment_methods": {"excluded_payment_types": [{"id": "atm"}]},
            "back_urls": {
                "success": f"{BASE_URL}/pago_exitoso",
                "failure": f"{BASE_URL}/pago_fallido",
                "pending": f"{BASE_URL}/pago_pendiente"
            },
            "auto_return": "approved",
            "notification_url": f"{BASE_URL}/notificacion/",
            "external_reference": id_pago_unico,
            "binary_mode": True
        }

        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
        response = requests.post(
            "https://api.mercadopago.com/checkout/preferences",
            json=preference_data,
            headers=headers,
            timeout=20
        )

        if response.status_code != 201:
            error_msg = response.json().get("message", "Error desconocido de MercadoPago")
            logger.error(f"Error al crear preferencia: {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)

        preference_id = response.json()["id"]
        
        # Guardamos toda la información relevante
        payments_db[id_pago_unico] = {
            "preference_id": preference_id,
            "usuario_id": usuario_id,
            "monto": monto,
            "email": email,
            "status": "pending",
            "payment_id": None,
            "merchant_order_id": None,
            "fecha_creacion": datetime.now().isoformat()
        }

        return {
            "id_pago_unico": id_pago_unico,
            "preference_id": preference_id,
            "url_pago": response.json()["init_point"]
        }

    except Exception as e:
        logger.error(f"Error al crear pago: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/notificacion/")
async def webhook(request: Request):
    try:
        # Manejar diferentes formatos de notificación
        try:
            content_type = request.headers.get('content-type')
            if content_type == 'application/json':
                data = await request.json()
            else:
                data = await request.form()
                data = dict(data)
        except Exception as e:
            logger.error(f"Error al parsear notificación: {str(e)}")
            return JSONResponse(content={"status": "parse_error"}, status_code=400)

        logger.info(f"Notificación recibida: {data}")

        # Extraer payment_id de diferentes formatos
        payment_id = None
        if 'data' in data and 'id' in data['data']:  # Formato nuevo
            payment_id = data['data']['id']
        elif 'id' in data:  # Formato alternativo
            payment_id = data['id']
        elif 'resource' in data:  # Notificación por query params
            resource = data.get('resource')
            if isinstance(resource, str) and resource.isdigit():
                payment_id = resource
            elif isinstance(resource, str) and 'payments' in resource:
                payment_id = resource.split('/')[-1]

        if not payment_id:
            logger.error("No se pudo extraer payment_id de la notificación")
            return JSONResponse(content={"status": "invalid_data"}, status_code=400)

        # Procesar en segundo plano
        Thread(
            target=process_payment_notification,
            args=(payment_id,),
            daemon=True
        ).start()

        return JSONResponse(content={"status": "received"})

    except Exception as e:
        logger.error(f"Error en webhook: {str(e)}")
        return JSONResponse(content={"status": "error"}, status_code=500)

def process_payment_notification(payment_id: str):
    """Procesa una notificación de pago de forma robusta"""
    try:
        # Extraer el ID real si viene en formato URL
        if 'merchant_orders' in str(payment_id):
            logger.info(f"Ignorando notificación de merchant_order: {payment_id}")
            return
            
        # Asegurarnos de que es solo el ID numérico
        payment_id = str(payment_id).split('/')[-1] if '/' in str(payment_id) else payment_id
        
        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
        
        # Obtener detalles del pago
        try:
            payment_response = requests.get(
                f"https://api.mercadopago.com/v1/payments/{payment_id}",
                headers=headers,
                timeout=10
            )
            payment_response.raise_for_status()
            payment_data = payment_response.json()
        except Exception as e:
            logger.error(f"Error al obtener detalles del pago {payment_id}: {str(e)}")
            return

        # Validar respuesta
        if 'status' not in payment_data:
            logger.error(f"Respuesta inválida de MP para payment_id {payment_id}")
            return

        external_ref = payment_data.get('external_reference')
        if not external_ref:
            logger.error(f"No se encontró external_reference para el pago {payment_id}")
            return

        # Verificar si ya fue procesado
        if external_ref in payments_db and payments_db[external_ref].get('procesado_ganamos'):
            logger.info(f"Pago {external_ref} ya fue procesado anteriormente")
            return

        # Actualizar base de datos
        payment_info = {
            "payment_id": payment_id,
            "status": payment_data.get('status'),
            "monto": payment_data.get('transaction_amount'),
            "fecha_actualizacion": datetime.now().isoformat()
        }

        if external_ref in payments_db:
            payments_db[external_ref].update(payment_info)
        else:
            payments_db[external_ref] = {
                **payment_info,
                "fecha_creacion": datetime.now().isoformat()
            }

        # Solo procesar si el pago está aprobado y no se ha procesado antes
        if payment_data.get('status') == 'approved':
            usuario_id = payments_db[external_ref].get('usuario_id')
            monto = payments_db[external_ref].get('monto')
            
            if usuario_id and monto:
                try:
                    logger.info(f"Intentando cargar saldo en Ganamos para usuario {usuario_id}")
                    success, balance = carga_ganamos(usuario_id, float(monto))
                    
                    # Actualizar estado en la base de datos
                    update_data = {
                        "procesado_ganamos": True,
                        "ganamos_success": success,
                        "ganamos_balance": balance if success else None,
                        "ganamos_last_attempt": datetime.now().isoformat()
                    }
                    payments_db[external_ref].update(update_data)
                    
                    if success:
                        logger.info(f"Carga exitosa en Ganamos. Nuevo balance: {balance}")
                    else:
                        logger.error(f"Fallo en carga a Ganamos para usuario {usuario_id}")
                except Exception as e:
                    logger.error(f"Error al ejecutar carga_ganamos: {str(e)}")
                    payments_db[external_ref].update({
                        "procesado_ganamos": True,
                        "ganamos_success": False,
                        "ganamos_last_attempt": datetime.now().isoformat()
                    })

        logger.info(f"Pago actualizado - ID: {external_ref}, Status: {payment_data.get('status')}")

    except Exception as e:
        logger.error(f"Error inesperado al procesar pago {payment_id}: {str(e)}")

@app.post("/verificar_pago/")
async def verificar_pago(request: Request):
    try:
        data = await request.json()
        id_pago_unico = data.get("id_pago_unico")
        
        if not id_pago_unico:
            raise HTTPException(status_code=400, detail="Se requiere id_pago_unico")

        logger.info(f"Verificando pago para ID: {id_pago_unico}")

        # 1. Buscar en base de datos local
        if id_pago_unico in payments_db:
            pago = payments_db[id_pago_unico]
            if pago.get("payment_id"):
                return pago

        # 2. Si no está completo, consultar a MP
        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
        search_url = f"https://api.mercadopago.com/v1/payments/search?external_reference={id_pago_unico}"
        
        response = requests.get(search_url, headers=headers, timeout=15)
        if response.status_code != 200:
            raise HTTPException(status_code=400, detail="Error al consultar MercadoPago")

        results = response.json().get("results", [])
        if not results:
            return {"status": "pending", "detail": "No se encontraron transacciones"}

        # Tomar el pago más reciente
        latest_payment = max(results, key=lambda x: x["date_created"])
        
        # Actualizar base de datos local
        payment_info = {
            "payment_id": latest_payment["id"],
            "status": latest_payment["status"],
            "monto": latest_payment["transaction_amount"],
            "fecha_actualizacion": datetime.now().isoformat()
        }

        if id_pago_unico in payments_db:
            payments_db[id_pago_unico].update(payment_info)
        else:
            payments_db[id_pago_unico] = {
                **payment_info,
                "fecha_creacion": datetime.now().isoformat()
            }

        return payments_db[id_pago_unico]

    except Exception as e:
        logger.error(f"Error al verificar pago: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/pago_exitoso")
async def pago_exitoso(
    collection_id: str = None,
    collection_status: str = None,
    payment_id: str = None,
    status: str = None,
    external_reference: str = None,
    preference_id: str = None,
    merchant_order_id: str = None
):
    """Endpoint para redirección después de pago exitoso"""
    if external_reference and external_reference in payments_db:
        payments_db[external_reference].update({
            "payment_id": payment_id or collection_id,
            "status": status or collection_status,
            "fecha_actualizacion": datetime.now().isoformat()
        })
    return RedirectResponse(url=f"/?pago=exitoso&id={external_reference}")

@app.get("/pago_fallido")
async def pago_fallido():
    return {"status": "failure"}

@app.get("/pago_pendiente")
async def pago_pendiente():
    return {"status": "pending"}

@app.get("/")
async def health_check():
    return {"status": "API operativa"}

@app.get("/debug/pagos")
async def debug_pagos():
    return {
        "count": len(payments_db),
        "pagos": payments_db
    }
