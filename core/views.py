from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework.response import Response


class SalesView(APIView):
    def post(self, request):
        return Response(data={"status": "ok"})
