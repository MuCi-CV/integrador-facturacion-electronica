import sentry_sdk
import logging
import re
import json
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from core.woocommerce import wc_api
from core.bims import bims
from core.models import FailedOrder

logger = logging.getLogger(__name__)

class SalesView(APIView):
    def post(self, request):

        # Traemos la información del pedido con la api de WC
        order_id = request.data.get("arg")

        if not order_id:
            logger.error("No se recibió 'order_id' en la solicitud.")
            return Response(data={"status": "fail", "error": "No se recibió 'order_id'"}, status=status.HTTP_400_BAD_REQUEST)

        with sentry_sdk.configure_scope() as scope:
            scope.set_tag("order_id", order_id)
            scope.set_extra("order_details", {"order_id": order_id})

            try:
                order = wc_api.get_order(order_id)
            except Exception as e:
                error_message = str(e)
                logger.error(f"Error al obtener la orden {order_id} de WooCommerce: {error_message}", exc_info=True)
                FailedOrder.objects.update_or_create(
                    order_id=order_id,
                    defaults={"message": f"No se pudo obtener la orden. {error_message}"},
                )
                return Response(
                    data={
                        "status": "fail",
                        "error": f"No se pudo obtener la orden. {error_message}",
                    },
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )

            try:
                meta_data = order.get("meta_data")

                # Verificamos si no tiene un descuento del 100%

                total = int(order.get("total"))
                discount = int(order.get("discount_total"))
                prices_include_tax = order.get("prices_include_tax", True)

                if total == 0 and discount > 0:
                    logger.info(f"Order {order_id} skipped: 100% discount.")
                    return Response(data={"status": "Descuento 100%"})

                # Creamos el contacto en BIMS

                # Los nombres y apellidos llegan en camel case así que hacemos la transformación para que
                # que estén separados con espacios
                regex = r"(?<=[a-zA-Z])(?=[A-Z])"
                subst = " "
                first_name = re.sub(regex, subst, order.get("billing").get("first_name"), 0)
                last_name = re.sub(regex, subst, order.get("billing").get("last_name"), 0)
                email = order.get("billing").get("email")
                phone = order.get("billing").get("phone")

                # Verificamos si es una compra a través de la web o boletería para asignar el punto de
                # venta correspontiente en BIMS

                user_id = next(
                    (
                        element
                        for element in meta_data
                        if element["key"] == "_fooeventspos_user_id"
                    ),
                    None,
                )
                # MÉTODOS DE PAGO EN BIMS
                # Efectivo -> 21
                # Transferencia -> 26
                # Bancard -> 27
                # En línea -> 28

                # CAJA WEB ->            ID_BIMS: 6          ID_WC: no existe
                # CAJA SAN COSMOS ->     ID_BIMS: 4          ID_WC: 729
                # CAJA TATAKUALAB ->     ID_BIMS: 1          ID_WC: 3
                if not user_id:
                    posale_id = 6
                    sales_payment_methods = [
                        {
                            "payment_method_id": 28,
                            "amount": total,
                        }
                    ]
                else:
                    value = int(user_id.get("value"))
                    if value == 2:
                        return Response(
                            data={
                                "status": "No procesado por ser realizado desde la cuenta de administrador."
                            }
                        )
                    elif value == 729:
                        posale_id = 4
                    elif value == 3:
                        posale_id = 1
                    else:
                        posale_id = 7

                    if order.get("payment_method_title") == "Cortesía":
                        return Response(data={"status": "Cortesía"})

                    # Extraer métodos de pago desde `_fooeventspos_payments`
                    payment_metadata = next(
                        (
                            element
                            for element in meta_data
                            if element["key"] == "_fooeventspos_payments"
                        ),
                        None,
                    )

                    # Mapeo de métodos de pago de FooEvents POS a BIMS
                    method_mapping = {
                        "fooeventspos_check_payment": 34,  # Gift Card
                        "fooeventspos_cash": 21,  # Efectivo
                        "fooeventspos_cash_on_delivery": 26,  # Transferencia Bancaria
                        "fooeventspos_other": 27,  # Bancard
                        "fooeventspos_online": 28,  # Pago en línea
                    }

                    sales_payment_methods = []  # Lista de métodos de pago para BIMS
                    gift_card_amount = 0  # Inicializar el monto pagado con Gift Card

                    if payment_metadata:
                        payments = json.loads(
                            payment_metadata["value"]
                        )  # Convertir JSON a lista de pagos

                        # Si hay solo un método de pago y no tiene "amount", significa que pagó todo el pedido
                        if len(payments) == 1 and "amount" not in payments[0]:
                            single_payment_method = payments[0]["opmk"]
                            payment_method_id = method_mapping.get(
                                single_payment_method, 28
                            )  # Por defecto: En línea
                            sales_payment_methods.append(
                                {
                                    "payment_method_id": payment_method_id,
                                    "amount": total,  # Se pagó el total con este método
                                }
                            )
                        else:
                            # Si hay más de un método de pago, cada uno tendrá "amount"
                            for payment in payments:
                                opmk = payment["opmk"]
                                amount = float(
                                    payment["amount"]
                                )  # Siempre existirá "amount" en este caso
                                payment_method_id = method_mapping.get(opmk, 28)

                                sales_payment_methods.append(
                                    {
                                        "payment_method_id": payment_method_id,
                                        "amount": amount,
                                    }
                                )

                if not user_id:
                    ruc = next(
                        (
                            element
                            for element in meta_data
                            if element["key"] == "_billing_ruc"
                        ),
                        None,
                    )
                    gov_id = next(
                        (
                            element
                            for element in meta_data
                            if element["key"] == "_billing_documento"
                        ),
                        None,
                    )
                    social_reason = next(
                        (
                            element
                            for element in meta_data
                            if element["key"] == "_billing_razon_social"
                        ),
                        None,
                    )
                else:
                    ruc = re.sub(regex, subst, order.get("shipping").get("company"), 0)
                    social_reason = order.get("shipping").get("last_name", None)
                    gov_id = None
                document_type = "ci"
                document_id = ""
                if (ruc or gov_id) and not user_id:
                    if not ruc or ruc.get("value", "") == "":
                        document_type = "ci"
                        document_id = gov_id.get("value")
                    else:
                        document_type = "ruc"
                        document_id = ruc.get("value")

                if social_reason and not user_id:
                    if social_reason.get("value") != "":
                        name = re.sub(regex, subst, social_reason.get("value"), 0)
                    else:
                        name = f"{first_name} {last_name}"
                else:
                    name = social_reason
                    document_id = ruc
                    if "-" in ruc:
                        document_type = "ruc"

                contact_emails = None
                contact = None
                try:
                    if (
                        first_name == ""
                        and last_name == ""
                        and (
                            social_reason == None
                            or social_reason == ""
                            or document_id == ""
                        )
                    ):
                        contact_id = None
                        if email != "":
                            contact_emails = email
                    else:
                        contact = bims.list_contacts(
                            document_id=re.sub(r"\D", "", document_id.split("-")[0]),
                            document_type=document_type,
                        )
                        contact_id = bims.create_contact(
                            id=contact,
                            name=name,
                            address="",
                            document_type=document_type,
                            document_id=re.sub(r"\D", "", document_id.split("-")[0]),
                            emails=email,
                            phones=phone,
                        )
                except Exception as e:
                    error_message = str(e)
                    logger.error(f"Error al crear el contacto en BIMS para orden {order_id}: {error_message}", exc_info=True)
                    FailedOrder.objects.update_or_create(
                        order_id=order_id,
                        defaults={
                            "message": f"Error al crear el contacto en BIMS. {error_message}"
                        },
                    )
                    return Response(
                        data={
                            "status": "fail",
                            "error": "Error al crear el contacto en BIMS.",
                        },
                        status=status.HTTP_503_SERVICE_UNAVAILABLE,
                    )

                line_items = order.get("line_items")

                sale_products = []
                skipped_items_messages = []
                for item in line_items:
                    search_id = item.get("variation_id") or item.get("product_id")
                    if search_id == 0:
                        search_id = item.get("product_id") # Fallback si variation_id es 0

                    product = wc_api.get_product(search_id)

                    if product.get("sku") == "":
                        message = f"Producto {search_id} (nombre: {item.get('name')}) no procesado por falta de SKU."
                        logger.warning(f"Order {order_id}: {message}")
                        skipped_items_messages.append(message)
                        continue # Saltar este ítem y continuar con el siguiente

                    bims_id = int(product.get("sku", 0)) #To do
                    if bims_id == 0: # Si el SKU existe pero es 0, también lo saltamos
                        message = f"Producto {search_id} (nombre: {item.get('name')}) no procesado porque su SKU es 0."
                        logger.warning(f"Order {order_id}: {message}")
                        skipped_items_messages.append(message)
                        continue # Saltar este ítem y continuar con el siguiente

                    item_quantity = int(item.get("quantity", 0))
                    if item_quantity == 0:
                        message = f"Producto {search_id} (nombre: {item.get('name')}) no procesado por tener cantidad 0."
                        logger.warning(f"Order {order_id}: {message}")
                        skipped_items_messages.append(message)
                        continue # Saltar este ítem

                    # Calcular price_per_item antes de las condiciones
                    # Evitar ZeroDivisionError
                    if item_quantity > 0:
                        item_total_with_tax = float(item.get("total", 0)) + float(item.get("total_tax", 0)) # Usar float para precisión
                        price_per_item = item_total_with_tax / item_quantity
                    else:
                        price_per_item = 0.0 # Usar float para consistencia

                    # Lógica para precios basada en descuentos o tipos de producto específicos
                    # (Revisado con tu aclaración de BIMS esperando precios con impuesto)
                    if (discount > 0 or search_id in [10648, 14369]):
                        sale_products.append(
                            {
                                "product_id": bims_id,
                                "quantity": item_quantity, # Usar item_quantity ya convertido
                                "price": round(price_per_item, 2), # Redondear para precisión monetaria
                            }
                        )
                    elif search_id in [19657, 14372, 8421, 3681, 24482]:
                        # **REVISAR ESTA LÍNEA** si estos productos tienen impuestos y BIMS los espera incluidos:
                        # price_for_special_item = float(item.get("total", 0)) + float(item.get("total_tax", 0))
                        # "price": round(price_for_special_item, 2),
                        sale_products.append(
                            {
                                "product_id": bims_id,
                                "quantity": 1.00,
                                "price": round(float(item.get("total", 0)), 2),
                            }
                        )
                    else:
                        sale_products.append(
                            {
                                "product_id": bims_id,
                                "quantity": item_quantity, # Usar item_quantity ya convertido
                                "price": round(price_per_item, 2), # Redondear para precisión monetaria
                            }
                        )

                fee_lines = order.get("fee_lines")
                for fee in fee_lines:
                    if fee.get("name") == "Tip":
                        sale_products.append(
                            {
                                "product_id": 100,
                                "quantity": 1.00,
                                "price": float(fee.get("total")),
                            }
                        )

                if not sale_products:
                    # Si ningún producto se pudo agregar a sale_products (todos fallaron/se saltaron)
                    final_message = "No se pudo procesar la orden: todos los productos fueron omitidos (falta de SKU, SKU cero o cantidad cero)."
                    
                    # Verificamos si alguno de los mensajes de omisión se refiere a falta de SKU
                    # Esto nos ayudará a determinar si el retryfaileds.py es aplicable
                    contains_sku_issue = any("falta de SKU" in msg for msg in skipped_items_messages)
                    
                    if skipped_items_messages:
                        final_message += " Detalles: " + "; ".join(skipped_items_messages)
                    
                    logger.warning(f"Order {order_id}: {final_message}")

                     # Usamos el mensaje específico si es por falta de SKU para el retry
                    message_for_failed_order = "No procesado por falta de sku." if contains_sku_issue else final_message

                    FailedOrder.objects.update_or_create(
                        order_id=order_id,
                        defaults={"message": message_for_failed_order, "status": FailedOrder.FAILED},
                    )
                    sentry_sdk.capture_message(f"Order processing failed for order {order_id}: No valid products to process.", level="warning")
                    return Response(
                        data={"status": "fail", "error": final_message}, # La respuesta externa puede ser más detallada
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                else:
                    try:
                        # Al menos un producto se pudo procesar, entonces intentamos crear la venta
                        final_contact_id_for_sale = contact_id if contact_id is not None else None
                        sale_id = bims.create_sale(
                            contact_id=final_contact_id_for_sale,
                            sale_products=sale_products,
                            posale_id=posale_id,
                            sales_payment_methods=sales_payment_methods,
                            contact_emails=contact_emails,
                            order=order_id,
                        )

                        # Si llegamos aquí, la venta se creó con éxito en BIMS (parcial o completa)
                        final_status_message = "Procesado con éxito."
                        if skipped_items_messages:
                            final_status_message += " Algunos productos fueron omitidos: " + "; ".join(skipped_items_messages)
                            sentry_sdk.capture_message(f"Order {order_id} processed with skipped items: {'; '.join(skipped_items_messages)}", level="warning")

                        logger.info(f"Order {order_id} processed. BIMS Sale ID: {sale_id}. Message: {final_status_message}")
                        
                        FailedOrder.objects.update_or_create(
                            order_id=order_id,
                            defaults={"status": FailedOrder.COMPLETED, "message": final_status_message},
                        )

                        return Response(data={"status": "ok", "message": final_status_message})
                    
                    except Exception as e:
                        error_message = str(e)
                        logger.error(f"Error al crear la venta en BIMS para orden {order_id}: {error_message}", exc_info=True)
                        
                        # Guardamos el mensaje de error real para debugging
                        FailedOrder.objects.update_or_create(
                            order_id=order_id,
                            defaults={"status": FailedOrder.FAILED, "message": f"Error al crear la venta en BIMS: {error_message}"},
                        )

                        return Response(
                            data={
                                "status": "fail",
                                "error": "Error al crear la venta en BIMS.",
                            },
                            status=status.HTTP_503_SERVICE_UNAVAILABLE,
                        )

            except Exception as e:
                error_message = str(e)
                logger.error(f"Error inesperado en SalesView para orden {order_id}: {error_message}", exc_info=True)
                FailedOrder.objects.update_or_create(
                    order_id=order_id,
                    defaults={"message": error_message},
                )
                return Response(
                    data={
                        "status": "fail",
                        "error": error_message,
                    },
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )


class RefundView(APIView):
    def post(self, request):
        # Traemos la información del pedido con la api de WC
        order_id = request.data.get("arg")
        order = wc_api.get_order(order_id)

        line_items = order.get("line_items")

        products = []
        for item in line_items:
            products.append(
                {
                    "product_id": item.get("product_id"),
                    "variation_id": item.get("variation_id"),
                    "quantity": item.get("quantity"),
                }
            )

        data = {"api_refund": False, "api_restock": True, "line_items": products}
        wc_api.refund_order(id=order_id, data=data)
        return Response(data={"status": "ok"})
