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

        # Creamos el contacto en BIMS

        # Los nombres y apellidos llegan en camel case así que hacemos la transformación para que
        # que estén separados con espacios
        regex = r"(?<=[a-zA-Z])(?=[A-Z])"
        subst = " "
        first_name = re.sub(regex, subst, order.get("billing").get("first_name"), 0)
        last_name = re.sub(regex, subst, order.get("billing").get("last_name"), 0)
        email = order.get("billing").get("email")
        phone = order.get("billing").get("phone")

        ruc = next(
            (element for element in meta_data if element["key"] == "_billing_ruc"), None
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

        document_type = "ci"
        document_id = ""
        if ruc or gov_id:
            if not ruc:
                document_type = "ci"
                document_id = gov_id.get("value")
            else:
                document_type = "ruc"
                document_id = ruc.get("value")

        if social_reason:
            name = re.sub(regex, subst, social_reason.get("value"), 0)
        else:
            name = f"{first_name} {last_name}"

        try:
            contact_id = bims.create_contact(
                name=name,
                address="",
                document_type=document_type,
                document_id=document_id,
                emails=email,
                phones=phone,
            )
        except Exception:
            return Response(
                data={"status": "fail", "error": "Error al crear el contacto en BIMS."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

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
        # CAJA WEB ->            ID_BIMS: 6          ID_WC: no existe
        # CAJA SAN COSMOS ->     ID_BIMS: 4          ID_WC: 729
        # CAJA TATAKUALAB ->     ID_BIMS: 1          ID_WC: 3
        if not user_id:
            posale_id = 6
        else:
            value = int(user_id.get("value"))
            if value == 729:
                posale_id = 4
            elif value == 3:
                posale_id = 1
            else:
                posale_id = 7

        line_items = order.get("line_items")

        sale_products = []
        for item in line_items:
            product = wc_api.get_product(item.get("product_id"))
            bims_id = next(
                (
                    element
                    for element in product.get("meta_data")
                    if element["key"] == "bims_id"
                ),
                None,
            )
            if bims_id != None:
                sale_products.append(
                    {
                        "product_id": int(bims_id.get("value")),
                        "quantity": item.get("quantity"),
                    }
                )
        try:
            sale_id = bims.create_sale(
                contact_id=contact_id, sale_products=sale_products, posale_id=posale_id
            )
        except Exception:
            return Response(
                data={"status": "fail", "error": "Error al crear la venta en BIMS."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        # Thread(target=bims.send_invoice, args=[sale_id]).start()

        return Response(data={"status": "ok"})
