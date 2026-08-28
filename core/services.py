import json
import logging
import re
from typing import Optional

import requests
import sentry_sdk
from django.db import IntegrityError

from core import deadline
from core.bims import bims, BimsBusinessError
from core.ruc import get_razon_social
from core.models import ContactCache, FailedOrder
from core.woocommerce import wc_api, WooCommerceAPI
from core.constants import (
    DISCOUNT_PRICE_PRODUCT_IDS,
    FLAT_PRICE_PRODUCT_IDS,
    FOOEVENTS_PAYMENT_METHOD_MAP,
    PAYMENT_METHOD_DEFAULT_ID,
    TIP_BIMS_PRODUCT_ID,
)
from core.sucursales import resolver_posale_de_cajero, resolver_posale_web

logger = logging.getLogger(__name__)

_CAMEL_CASE_RE = re.compile(r"(?<=[a-zA-Z])(?=[A-Z])")
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def _camel_to_spaces(text: str) -> str:
    return _CAMEL_CASE_RE.sub(" ", text or "")


def _clean_email(email: str) -> str:
    """Extrae el primer email válido de un string potencialmente corrupto.

    Maneja casos como 'user@mail.com asistencia contable' extrayendo
    solo la parte del email. Retorna cadena vacía si no es válido.
    """
    if not email:
        return ""
    candidate = email.strip().split()[0]
    return candidate.lower() if _EMAIL_RE.match(candidate) else ""


def _search_contact_in_bims(document_id: str, document_type: str) -> Optional[dict]:
    """Busca un contacto en BIMS probando variantes de document_type
    y document_id (con/sin dígito verificador).

    Retorna dict con datos exactos de BIMS (id, name, document_id,
    document_type, emails) o None.
    """
    base_number = document_id.split("-")[0]
    has_verificador = "-" in document_id
    alt_type = "ci" if document_type == "ruc" else "ruc"

    # Construir variantes a probar (la más probable primero)
    variants = [
        (document_id, document_type),
        (document_id, alt_type),
    ]
    # Si tiene dígito verificador, probar también solo el número base
    if has_verificador:
        variants.append((base_number, "ci"))
        variants.append((base_number, "ruc"))

    # Eliminar duplicados preservando orden
    seen = set()
    unique_variants = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            unique_variants.append(v)

    for doc_id, doc_type in unique_variants:
        contact = bims.find_contact(document_id=doc_id, document_type=doc_type)
        if contact:
            logger.info(
                f"Contacto encontrado en BIMS con variante "
                f"(doc={doc_id}, type={doc_type}). BIMS ID: {contact['id']}"
            )
            return contact

    return None


def _get_meta(meta_data: list, key: str):
    return next((item for item in meta_data if item["key"] == key), None)


def resolve_pos_and_payments(
    meta_data: list, total: int, payment_method_title: str
):
    """
    Determina el punto de venta y los métodos de pago para una orden.

    Retorna (posale_id, sales_payment_methods) o None si la orden debe ignorarse
    (cuenta de administrador o pago Cortesía).
    """
    user_id_meta = _get_meta(meta_data, "_fooeventspos_user_id")

    if not user_id_meta:
        # Orden web: pago en línea por el total.
        posale_id = resolver_posale_web()
        if posale_id is None:
            logger.info(
                "Orden web ignorada: la regla de órdenes web no tiene punto de venta."
            )
            return None
        return posale_id, [{"payment_method_id": PAYMENT_METHOD_DEFAULT_ID, "amount": total}]

    user_id_value = int(user_id_meta["value"])

    # `None` = no facturar. Antes era un `if user_id_value == 2` hardcodeado;
    # ahora cualquier cajero se da de baja dejándole el punto de venta vacío.
    posale_id = resolver_posale_de_cajero(user_id_value)
    if posale_id is None:
        logger.info(
            f"Orden ignorada: el cajero {user_id_value} no tiene punto de venta asignado."
        )
        return None

    sales_payment_methods = _parse_pos_payments(meta_data, total)
    return posale_id, sales_payment_methods


def _parse_pos_payments(meta_data: list, total: int) -> list:
    """
    Parsea los métodos de pago desde el metadata de FooEvents POS.

    Fix: versiones recientes de FooEvents POS envían "amount": 0 para pagos únicos
    (el monto real está en el campo "pa"). Para pagos únicos con amount == 0,
    se usa el total de la orden — comportamiento equivalente al de versiones anteriores
    que directamente no enviaban el campo "amount".
    """
    payment_meta = _get_meta(meta_data, "_fooeventspos_payments")
    if not payment_meta:
        return []

    payments = json.loads(payment_meta["value"])

    if len(payments) == 1 and float(payments[0].get("amount", 0)) == 0:
        return [
            {
                "payment_method_id": _payment_method_id(payments[0]["opmk"]),
                "amount": total,
            }
        ]

    return [
        {
            "payment_method_id": _payment_method_id(p["opmk"]),
            "amount": float(p["amount"]),
        }
        for p in payments
    ]


def _payment_method_id(opmk: str) -> int:
    """
    Traduce un método de pago de FooEvents POS al de BIMS.

    El fallback se queda —una orden no debe caerse por un método nuevo— pero **avisa**.
    Un `opmk` desconocido se factura como "En línea", o sea como cobrada: es
    exactamente el bug que tuvo Cortesía durante 3 años, porque FooEvents reetiqueta
    un slot y del lado de BIMS nadie se entera.
    """
    method_id = FOOEVENTS_PAYMENT_METHOD_MAP.get(opmk)
    if method_id is None:
        logger.warning(
            f"Método de pago de FooEvents sin mapear: {opmk}. "
            f"Se factura como {PAYMENT_METHOD_DEFAULT_ID} (En línea)."
        )
        return PAYMENT_METHOD_DEFAULT_ID
    return method_id


def resolve_contact_id(
    order_id: int,
    meta_data: list,
    billing: dict,
    shipping: dict,
    is_pos: bool,
):
    """
    Encuentra o crea un contacto en BIMS a partir de los datos de la orden.
    Usa ContactCache para evitar búsquedas repetidas por email.

    Retorna (contact_id, contact_emails).
    - contact_id: ID del contacto en BIMS (None si no hay datos suficientes).
    - contact_emails: email a asociar a la venta cuando no hay contact_id.
    """
    first_name = _camel_to_spaces(billing.get("first_name", ""))
    last_name = _camel_to_spaces(billing.get("last_name", ""))
    email = billing.get("email", "")
    phone = billing.get("phone", "")

    if is_pos:
        company = _camel_to_spaces(shipping.get("company") or "")
        document_id = company
        document_type = "ruc" if "-" in company else "ci"
        # FooEvents POS envía nombre y apellido separados en billing (igual que la web).
        # No usar shipping.last_name como nombre: contiene solo el apellido y
        # descartaría el nombre de pila.
        name = f"{first_name} {last_name}".strip()
    else:
        ruc_meta = _get_meta(meta_data, "_billing_ruc")
        gov_id_meta = _get_meta(meta_data, "_billing_documento")
        social_reason_meta = _get_meta(meta_data, "_billing_razon_social")

        ruc_value = (ruc_meta or {}).get("value", "")
        gov_id_value = (gov_id_meta or {}).get("value", "")
        if ruc_value:
            # Un RUC real tiene guión verificador (ej: "1234567-8").
            # Si no tiene guión, es una CI puesta en campo equivocado.
            if "-" in ruc_value:
                document_type = "ruc"
            else:
                document_type = "ci"
            document_id = ruc_value
        elif gov_id_value:
            document_type = "ci"
            document_id = gov_id_value
        else:
            document_type = "ci"
            document_id = ""

        social_reason_value = (social_reason_meta or {}).get("value", "")
        name = _camel_to_spaces(social_reason_value) if social_reason_value else f"{first_name} {last_name}"

        # Corrección autoritativa: si hay RUC, la razón social del padrón manda.
        if document_type == "ruc":
            authoritative = get_razon_social(document_id)
            if authoritative:
                logger.info(
                    f"Order {order_id}: Razón social corregida por RUC {document_id}: "
                    f"'{name}' -> '{authoritative}'"
                )
                name = authoritative

    clean_email = _clean_email(email)

    # Sin datos suficientes para identificar al contacto
    if not first_name and not last_name and not name.strip():
        logger.info(f"Order {order_id}: Sin datos de contacto, se usará email como referencia.")
        return None, (clean_email or None)

    # Preservar document_id completo (con guión si lo tiene) para búsqueda
    # y extraer solo dígitos del número base para limpieza
    raw_document_id = str(document_id).strip() if document_id else ""
    base_number = re.sub(r"\D", "", raw_document_id.split("-")[0])

    if not base_number or not name.strip():
        logger.info(
            f"Order {order_id}: Omitiendo creación de contacto. "
            f"Falta nombre ('{name}') o documento ('{base_number}')."
        )
        return None, (clean_email or None)

    # Reconstruir document_id limpio (base + verificador si existe)
    verificador = raw_document_id.split("-")[1] if "-" in raw_document_id else None
    clean_document_id = f"{base_number}-{verificador}" if verificador else base_number

    # ── 1. Buscar en caché local por document_id (más confiable) ──
    cached_by_doc = ContactCache.objects.filter(document_id=base_number).first()
    if not cached_by_doc and verificador:
        cached_by_doc = ContactCache.objects.filter(document_id=clean_document_id).first()
    if cached_by_doc:
        logger.info(
            f"Order {order_id}: Contacto en caché por documento ({base_number}). "
            f"BIMS ID: {cached_by_doc.bims_id}"
        )
        return cached_by_doc.bims_id, None

    # ── 2. Buscar en caché local por email ──
    if clean_email:
        cached_by_email = ContactCache.objects.filter(email=clean_email).first()
        if cached_by_email:
            logger.info(
                f"Order {order_id}: Contacto en caché por email ({clean_email}). "
                f"BIMS ID: {cached_by_email.bims_id}"
            )
            return cached_by_email.bims_id, None

    # ── 3. Buscar en BIMS con variantes de document_type/id ──
    bims_contact = _search_contact_in_bims(clean_document_id, document_type)
    if bims_contact:
        contact_id = bims_contact["id"]
        logger.info(
            f"Order {order_id}: Contacto encontrado en BIMS por variante. "
            f"ID: {contact_id}, doc={bims_contact['document_id']}, "
            f"type={bims_contact['document_type']}"
        )

        # Intentar actualizar email si cambió (best-effort)
        bims_email = _clean_email(bims_contact.get("emails", ""))
        if clean_email and clean_email != bims_email:
            logger.info(
                f"Order {order_id}: Email difiere "
                f"(BIMS='{bims_email}', orden='{clean_email}'). "
                f"Intentando actualizar..."
            )
            updated = bims.update_contact_email(
                contact_id=contact_id,
                name=bims_contact["name"],
                document_id=bims_contact["document_id"],
                document_type=bims_contact["document_type"],
                new_email=clean_email,
            )
            if updated:
                logger.info(f"Order {order_id}: Email actualizado a '{clean_email}'.")
            else:
                logger.warning(
                    f"Order {order_id}: No se pudo actualizar email "
                    f"(probablemente ya en uso por otro contacto). "
                    f"Usando contacto {contact_id} sin actualizar."
                )

        # Guardar/actualizar caché local
        cache_email = clean_email if clean_email else bims_email
        if cache_email:
            try:
                ContactCache.objects.update_or_create(
                    bims_id=contact_id,
                    defaults={"email": cache_email, "document_id": base_number},
                )
            except IntegrityError:
                # Otro request concurrente ya insertó este registro; sin efecto.
                pass

        return contact_id, None

    # ── 4. Crear contacto nuevo en BIMS ──
    logger.info(f"Order {order_id}: Creando contacto nuevo en BIMS para '{name}'.")
    try:
        contact_id = bims.create_contact(
            id=None,
            name=name,
            address="",
            document_type=document_type,
            document_id=clean_document_id,
            emails=clean_email,
            phones=phone,
        )
    except Exception as e:
        # Si falla con 403, intentar buscar en caché por document_id
        # (pudo haber sido creado por otro request concurrente).
        # Detectamos por tipo (BimsBusinessError) y mantenemos el match de string
        # como respaldo por compatibilidad.
        if isinstance(e, BimsBusinessError) or "403" in str(e):
            logger.warning(
                f"Order {order_id}: 403 al crear contacto. "
                f"Buscando en caché por doc {base_number}..."
            )
            cached = ContactCache.objects.filter(document_id=base_number).first()
            if cached:
                logger.info(
                    f"Order {order_id}: Contacto encontrado en caché tras 403. "
                    f"BIMS ID: {cached.bims_id}"
                )
                return cached.bims_id, None
        raise

    if contact_id and clean_email:
        try:
            ContactCache.objects.update_or_create(
                bims_id=int(contact_id),
                defaults={"email": clean_email, "document_id": base_number},
            )
            logger.info(
                f"Order {order_id}: Contacto guardado en caché. "
                f"BIMS ID: {contact_id}, Email: {clean_email}"
            )
        except IntegrityError:
            # Otro request concurrente ya insertó este registro; sin efecto.
            pass

    return contact_id, None


# Motivo de omisión que no denota un problema de datos: el producto simplemente
# no cobra nada. Se usa como marca para distinguirlo de los omitidos que sí hay
# que revisar (sin SKU, SKU 0, cantidad 0).
ZERO_PRICE_SKIP_REASON = "precio 0"


def _all_skips_are_zero_price(skipped_messages: list) -> bool:
    """True si hubo ítems omitidos y todos fueron por tener precio 0."""
    return bool(skipped_messages) and all(
        ZERO_PRICE_SKIP_REASON in msg for msg in skipped_messages
    )


def build_sale_products(
    order_id: int, line_items: list, fee_lines: list, discount: int
):
    """
    Construye la lista de productos para enviar a BIMS.

    Retorna (sale_products, skipped_messages).
    - sale_products: productos válidos listos para BIMS.
    - skipped_messages: descripciones de ítems omitidos (sin SKU, SKU 0, cantidad 0,
      precio 0). La propina en 0 también se descarta, pero solo se registra en el log.
    """
    sale_products = []
    skipped_messages = []

    for item in line_items:
        search_id = item.get("variation_id") or item.get("product_id")
        if search_id == 0:
            search_id = item.get("product_id")

        product = wc_api.get_product(search_id)

        if product.get("sku") == "":
            msg = f"Producto {search_id} ({item.get('name')}) omitido: sin SKU."
            logger.warning(f"Order {order_id}: {msg}")
            skipped_messages.append(msg)
            continue

        bims_id = int(product.get("sku", 0))
        if bims_id == 0:
            msg = f"Producto {search_id} ({item.get('name')}) omitido: SKU es 0."
            logger.warning(f"Order {order_id}: {msg}")
            skipped_messages.append(msg)
            continue

        quantity = int(item.get("quantity", 0))
        if quantity == 0:
            msg = f"Producto {search_id} ({item.get('name')}) omitido: cantidad 0."
            logger.warning(f"Order {order_id}: {msg}")
            skipped_messages.append(msg)
            continue

        total_with_tax = float(item.get("total", 0)) + float(item.get("total_tax", 0))
        price_per_item = round(total_with_tax / quantity, 2)

        if discount > 0 or search_id in DISCOUNT_PRICE_PRODUCT_IDS:
            sale_product = {"product_id": bims_id, "quantity": quantity, "price": price_per_item}
        elif search_id in FLAT_PRICE_PRODUCT_IDS:
            sale_product = {
                "product_id": bims_id,
                "quantity": 1.0,
                "price": round(float(item.get("total", 0)), 2),
            }
        else:
            sale_product = {"product_id": bims_id, "quantity": quantity, "price": price_per_item}

        # El precio se chequea sobre el dict ya armado: cada rama lo calcula distinto
        # y ninguna debe mandar un producto que la factura cobraría en 0 o en menos.

        # Un negativo suele ser una línea de descuento mal armada. Se omite igual
        # (no puede llegar a BIMS), pero sin la marca de precio 0: así la orden
        # alerta en Sentry y, si no queda nada, cae en FailedOrder para revisar.
        if sale_product["price"] < 0:
            msg = (
                f"Producto {search_id} ({item.get('name')}) omitido: "
                f"precio negativo ({sale_product['price']})."
            )
            logger.warning(f"Order {order_id}: {msg}")
            skipped_messages.append(msg)
            continue

        if sale_product["price"] == 0:
            msg = f"Producto {search_id} ({item.get('name')}) omitido: {ZERO_PRICE_SKIP_REASON}."
            logger.warning(f"Order {order_id}: {msg}")
            skipped_messages.append(msg)
            continue

        sale_products.append(sale_product)

    for fee in fee_lines:
        if fee.get("name") == "Tip":
            tip_price = float(fee.get("total", 0))
            if tip_price < 0:
                msg = f"Propina omitida: monto negativo ({tip_price})."
                logger.warning(f"Order {order_id}: {msg}")
                skipped_messages.append(msg)
                continue
            if tip_price == 0:
                logger.info(f"Order {order_id}: propina omitida por ser 0.")
                continue
            sale_products.append({
                "product_id": TIP_BIMS_PRODUCT_ID,
                "quantity": 1.0,
                "price": tip_price,
            })

    return sale_products, skipped_messages


def process_order(order_id: int) -> dict:
    """
    Orquesta el procesamiento completo de una orden de WooCommerce hacia BIMS.

    Es el único punto donde se fija el presupuesto de la orden. gunicorn corre con
    `--timeout 120` y mata al worker **por señal** al pasarse; un worker matado por
    señal no ejecuta el `except` que graba el `FailedOrder`, así que la orden
    desaparecería sin factura y sin registro. Con el presupuesto, cualquier fallo
    por lentitud termina en un `FailedOrder` reintentable.

    El `finally` no es decorativo: sin él, el presupuesto de esta orden se
    filtraría a la próxima request que atienda el mismo worker.
    """
    token = deadline.iniciar()
    try:
        return _process_order(order_id)
    finally:
        deadline.restaurar(token)


def _process_order(order_id: int) -> dict:
    """
    Orquesta el procesamiento completo de una orden de WooCommerce hacia BIMS.

    Retorna un dict con "status" y opcionalmente "message" o "error".
    Ante cualquier fallo persiste en FailedOrder y re-lanza la excepción.
    """
    try:
        order = wc_api.get_order(order_id)
    except Exception as e:
        FailedOrder.objects.update_or_create(
            order_id=order_id,
            defaults={"message": f"No se pudo obtener la orden. {e}"},
        )
        raise

    meta_data = order.get("meta_data", [])
    # WooCommerce puede enviar montos como string decimal ("0.00", "40000.00").
    # Parsear con float() antes de int() evita ValueError; en PYG (sin centavos)
    # el truncado es lossless.
    total = int(float(order.get("total", 0) or 0))
    discount = int(float(order.get("discount_total", 0) or 0))

    # Ninguna orden con total 0 debe llegar a BIMS: una factura de 0 guaraníes
    # no le sirve a nadie (sea gratis de origen o por un descuento del 100%).
    if total == 0:
        motivo = "descuento del 100%" if discount > 0 else "monto 0"
        logger.info(f"Order {order_id}: ignorada por {motivo}.")
        return {"status": "Descuento 100%" if discount > 0 else "Monto 0"}

    result = resolve_pos_and_payments(meta_data, total, order.get("payment_method_title", ""))
    if result is None:
        return {"status": "No procesado"}
    posale_id, sales_payment_methods = result

    is_pos = _get_meta(meta_data, "_fooeventspos_user_id") is not None

    try:
        contact_id, contact_emails = resolve_contact_id(
            order_id=order_id,
            meta_data=meta_data,
            billing=order.get("billing", {}),
            shipping=order.get("shipping", {}),
            is_pos=is_pos,
        )
    except Exception as e:
        FailedOrder.objects.update_or_create(
            order_id=order_id,
            defaults={"message": f"Error al crear el contacto en BIMS. {e}"},
        )
        raise

    try:
        sale_products, skipped_messages = build_sale_products(
            order_id=order_id,
            line_items=order.get("line_items", []),
            fee_lines=order.get("fee_lines", []),
            discount=discount,
        )
    except Exception as e:
        FailedOrder.objects.update_or_create(
            order_id=order_id,
            defaults={
                "status": FailedOrder.FAILED,
                "message": f"Error al leer los productos en WooCommerce. {e}",
            },
        )
        raise

    if not sale_products:
        # Si lo único que sobró fueron precios en 0, no hay nada que facturar: es un
        # descarte esperado, no un fallo. No se registra FailedOrder ni se reintenta.
        if _all_skips_are_zero_price(skipped_messages):
            logger.info(f"Order {order_id}: ignorada, todos los productos tienen precio 0.")
            return {"status": "Productos en 0"}

        contains_sku_issue = any("sin SKU" in msg for msg in skipped_messages)
        detail = " Detalles: " + "; ".join(skipped_messages) if skipped_messages else ""
        final_message = "No se pudo procesar la orden: todos los productos fueron omitidos." + detail
        db_message = "No procesado por falta de sku." if contains_sku_issue else final_message

        FailedOrder.objects.update_or_create(
            order_id=order_id,
            defaults={"message": db_message, "status": FailedOrder.FAILED},
        )
        sentry_sdk.capture_message(
            f"Order {order_id}: no hay productos válidos para procesar.", level="warning"
        )
        raise ValueError(final_message)

    try:
        sale_id, invoice_number, bims_error = bims.create_sale(
            contact_id=contact_id,
            sale_products=sale_products,
            posale_id=posale_id,
            sales_payment_methods=sales_payment_methods,
            contact_emails=contact_emails,
            order=order_id,
        )
    except Exception as e:
        FailedOrder.objects.update_or_create(
            order_id=order_id,
            defaults={"status": FailedOrder.FAILED, "message": f"Error al crear la venta en BIMS. {e}"},
        )
        raise

    if not sale_id:
        error_message = f"Rechazado por BIMS: {bims_error}"
        FailedOrder.objects.update_or_create(
            order_id=order_id,
            defaults={"status": FailedOrder.FAILED, "message": error_message},
        )
        raise ValueError(error_message)

    status_message = "Procesado con éxito."
    if skipped_messages:
        status_message += " Algunos productos fueron omitidos: " + "; ".join(skipped_messages)
        # Un producto gratis es esperado y no hay nada que revisar; solo alertamos
        # cuando algún omitido responde a un problema de datos.
        if not _all_skips_are_zero_price(skipped_messages):
            sentry_sdk.capture_message(
                f"Order {order_id} procesada con ítems omitidos: {'; '.join(skipped_messages)}",
                level="warning",
            )

    logger.info(f"Order {order_id} procesada. BIMS Sale ID: {sale_id}. {status_message}")
    sale_id_txt = str(sale_id)
    invoice_txt = str(invoice_number) if invoice_number is not None else None
    FailedOrder.objects.update_or_create(
        order_id=order_id,
        defaults={
            "status": FailedOrder.COMPLETED,
            "message": status_message,
            "bims_sale_id": sale_id_txt,
            "bims_invoice_number": invoice_txt,
        },
    )

    # La factura también se anota en la orden de WooCommerce, que es donde
    # trabaja la caja. Es best-effort a propósito: acá la venta ya está
    # facturada y certificada ante la SET, y el FailedOrder ya quedó COMPLETED.
    # Propagar el error daría un 503 que además mentiría, y 5 seguidos apagan el
    # webhook `Venta Entrada` (es como murió `Refund order`).
    meta_factura = {"_bims_sale_id": sale_id_txt}
    if invoice_txt is not None:
        meta_factura["_bims_invoice_number"] = invoice_txt
    try:
        wc_api.update_order_meta(order_id, meta_factura)
    except (
        WooCommerceAPI.ServerException,
        requests.RequestException,
        deadline.PresupuestoOrdenAgotado,
    ) as e:
        logger.warning(
            f"Order {order_id}: venta {sale_id} facturada, pero no se pudo anotar "
            f"en WooCommerce: {e}"
        )

    return {"status": "ok", "message": status_message}
