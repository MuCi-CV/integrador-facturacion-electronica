from threading import Thread
from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework.response import Response
from core.woocommerce import wc_api
from core.bims import bims
from rest_framework import status
import re


class SalesView(APIView):
    def post(self, request):

        # Traemos la información del pedido con la api de WC
        order_id = request.data.get("arg")
        order = wc_api.get_order(order_id)
        meta_data = order.get("meta_data")

        # Verificamos si no tiene un descuento del 100%

        total = int(order.get("total"))
        discount = int(order.get("discount_total"))

        if total == 0 and discount > 0:
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
            payment_method_id = 28
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
            if order.get("payment_method_title") == "Efectivo":
                payment_method_id = 21
            elif order.get("payment_method_title") == "Bancard":
                payment_method_id = 27
            elif order.get("payment_method_title") == "Transferencia Bancaria directa":
                payment_method_id = 26
            elif order.get("payment_method_title") == "Cortesía":
                return Response(data={"status": "Cortesía"})

        if not user_id:
            ruc = next(
                (element for element in meta_data if element["key"] == "_billing_ruc"),
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
        try:
            if (
                first_name == ""
                and last_name == ""
                and (social_reason == None or social_reason == "")
                or document_id == ""
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
        except Exception:
            return Response(
                data={"status": "fail", "error": "Error al crear el contacto en BIMS."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        line_items = order.get("line_items")

        sale_products = []
        for item in line_items:
            if "variation_id" in item:
                search = item.get("variation_id")
            else:
                search = item.get("product_id")
            if search == 0:
                search = item.get("product_id")
            product = wc_api.get_product(search)
            if product.get("sku") == "":
                return Response(data={"status": "No procesado por falta de sku."})
            bims_id = int(product.get("sku", 0))
            if bims_id != 0:

                # verificamos si hay descuentos por cupon o descuentos de entradas online

                if discount > 0 or search == 10648 or search == 14369:
                    sale_products.append(
                        {
                            "product_id": bims_id,
                            "quantity": item.get("quantity"),
                            "price": (
                                int(item.get("total")) / int(item.get("quantity"))
                            )
                            + int(item.get("total_tax")),
                        }
                    )
                elif (
                    search == 19657  # entrada grupal sc
                    or search == 14372  # entrada grupal ttklab
                    or search == 8421  # donación en caja
                    or search == 3681  # paquete de cumple
                    or search == 24482  # entrada online sc
                ):
                    sale_products.append(
                        {
                            "product_id": bims_id,
                            "quantity": 1.00,
                            "price": float(item.get("total")),
                        }
                    )
                else:
                    sale_products.append(
                        {
                            "product_id": bims_id,
                            "quantity": item.get("quantity"),
                            "price": product.get("price"),
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
        try:
            if contact_id == None and contact != None:
                sale_id = bims.create_sale(
                    contact_id=contact,
                    sale_products=sale_products,
                    posale_id=posale_id,
                    payment_method_id=payment_method_id,
                    amount=order.get("total"),
                    contact_emails=contact_emails,
                    order=order_id,
                )
            else:
                sale_id = bims.create_sale(
                    contact_id=contact_id,
                    sale_products=sale_products,
                    posale_id=posale_id,
                    payment_method_id=payment_method_id,
                    amount=order.get("total"),
                    contact_emails=contact_emails,
                    order=order_id,
                )
        except Exception:
            return Response(
                data={"status": "fail", "error": "Error al crear la venta en BIMS."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        # Thread(target=bims.send_invoice, args=[sale_id]).start()

        return Response(data={"status": "ok"})


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
