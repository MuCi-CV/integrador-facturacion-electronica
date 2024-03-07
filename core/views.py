from threading import Thread
from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework.response import Response
from core.woocommerce import wcAPI
from core.bims import bims
class SalesView(APIView):
    def post(self, request):
        # line_items = request.data.get("line_items")
        # first_name = request.data.get("billing").get("first_name")
        # last_name = request.data.get("billing").get("last_name")
        # email = request.data.get("billing").get("email")
        # phone = request.data.get("billing").get("phone")
        # ruc = request.data.get("meta_data")[2].get("value")
        # gov_id = request.data.get("meta_data")[0].get("value")
        # #social_reason = request.data.get("meta_data")[2].get("_billing_razon_social")

        # if ruc == "":
        #     document_type = "ci"
        #     document_id = gov_id
        # else:
        #     document_type = "ruc"
        #     document_id = ruc
        # contact_id = bims.create_contact(name=f"{first_name} {last_name}", address="", document_type=document_type, document_id=document_id, emails=email, phones=phone)


        # sale_products = []
        # for item in line_items:
        #     product = wcAPI.get_product(item.get("product_id"))
        #     bims_id =  next((element for element in product.get("meta_data") if element['key'] == "bims_id"), None)
        #     if bims_id != None:
        #         sale_products.append({"product_id": int(bims_id.get("value")), "quantity": item.get("quantity")})
       
        # sale_id = bims.create_sale(contact_id=contact_id, sale_products=sale_products)
        # Thread(target=bims.send_invoice, args=[sale_id]).start()
        
        return Response(data={"status": "ok"})
