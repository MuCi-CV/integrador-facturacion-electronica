"""
CÓDIGO RESCATADO, NO CABLEADO Y NO APTO PARA PRODUCCIÓN TODAVÍA.

Viene del commit `c6a87dd` ("Feature Syncronizar Stock", 2025-11-03), que quedó en
la rama `feature-sync` sin mergear y **nunca corrió con éxito**: la tabla
`wpzv_bimsc_stocks` de su equivalente PHP tiene 0 filas y ningún producto de
WooCommerce tiene el meta `_bims_sync`. Se rescata acá para no perderlo y para
poder diffearlo contra la versión buena, no para desplegarlo.

A propósito **no está en `urls.py`**: mientras siga con los defectos de abajo,
que no sea alcanzable es la única protección.

Defectos conocidos, medidos el 2026-09-02 contra el plugin `bimsc` y la API real:

1. 🔴 **Suma TODOS los almacenes.** `bimsc` filtra por una lista de depósitos
   habilitados (opción `bimsc_warehouses`, hoy `1,4`). Sin ese filtro se publicaría
   online stock que no corresponde vender. Es el defecto más grave.
2. 🔴 **Crawlea el catálogo entero con `offset`.** Es la estrategia VIEJA de
   `bimsc`. La nueva (`receiveFromBimsNew`) pregunta sólo por los `_bims_id` que
   WooCommerce tiene —hoy 265 productos, en chunks de 100— o sea 3 requests por
   barrido en vez de recorrer todo BIMS.
3. 🔴 **`requests.get` sin `timeout`.** El commit es anterior a todo el trabajo de
   presupuesto por orden y timeouts, y no pasa por `_retry_request`, así que no
   tiene reintentos, ni conmutación a `BIMS_FALLBACK_URL`, ni presupuesto.
4. ⚠️ **Usa `bims.sid` directo**, de cuando la autenticación era por sesión.
   Producción usa **API Key por header** desde el 2026-08-21.
5. ⚠️ **El comando le pega por HTTP a su propio Django** (`BASE_URL + "/stock-sync/"`)
   en vez de llamar a la capa de servicio. Un `manage.py` que necesita que gunicorn
   esté vivo para funcionar se cae solo cuando más falta hace.
6. ⚠️ **Busca el producto con `get_products(meta_key=...)`**, que la REST API de
   WooCommerce **no soporta** como filtro. `bimsc` lo hace con `WP_Query` desde
   dentro de WordPress, que es un contexto que nosotros no tenemos.

Ver la memoria `project_sincronizacion_stock_bims` para la mecánica completa que
sí sirve de `bimsc`.
"""

class StockSyncView(APIView):
    def post(self, request):
        """
        Sincroniza stock desde BIMS hacia WooCommerce
        Similar a receiveFromBims() del plugin PHP
        """
        try:
            # Obtener parámetros
            limit = request.data.get('limit', 100)
            offset = request.data.get('offset', 0)
            
            # Obtener productos desde BIMS
            products_data = self.get_products_from_bims(limit, offset)
            
            # Actualizar stock en WooCommerce
            updated_products = self.update_woocommerce_stock(products_data)
            
            return Response({
                "status": "ok",
                "updated": len(updated_products),
                "offset": offset + limit,
                "products": updated_products
            })
            
        except Exception as e:
            logger.error(f"Error sincronizando stock: {str(e)}", exc_info=True)
            return Response(
                {"status": "fail", "error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    def get_products_from_bims(self, limit, offset):
        """Obtiene productos desde BIMS con su stock"""
        try:
            # Usar la API de BIMS similar al plugin PHP
            url = f"{settings.BIMS_URL}/products/"
            params = {
                'sid': bims.sid,
                'recursive': -1,
                'full_images': 1,
                'v_stock': 1,
                'webpos': 1,
                'limit': limit,
                'offset': offset
            }
            
            response = requests.get(url, params=params)
            response.raise_for_status()
            
            return response.json().get('data', [])
            
        except Exception as e:
            logger.error(f"Error obteniendo productos de BIMS: {str(e)}")
            raise
    
    def update_woocommerce_stock(self, products_data):
        """Actualiza stock en WooCommerce"""
        updated = []
        
        for product in products_data:
            try:
                bims_id = product['Product']['id']
                stock_total = 0
                
                # Calcular stock total desde AvailabilityFull
                if product['Product'].get('AvailabilityFull'):
                    for stock in product['Product']['AvailabilityFull']:
                        stock_total += float(stock.get('total', 0))
                
                # Buscar producto en WooCommerce por SKU o meta _bims_id
                wc_product = self.find_woocommerce_product(bims_id)
                
                if wc_product:
                    # Actualizar stock
                    wc_api.update_product(wc_product['id'], {
                        'stock_quantity': int(stock_total),
                        'manage_stock': True,
                        'stock_status': 'instock' if stock_total > 0 else 'outofstock'
                    })
                    
                    updated.append({
                        'bims_id': bims_id,
                        'wc_id': wc_product['id'],
                        'stock': stock_total
                    })
                    
            except Exception as e:
                logger.error(f"Error actualizando producto {bims_id}: {str(e)}")
                continue
        
        return updated
    
    def find_woocommerce_product(self, bims_id):
        """Encuentra un producto en WooCommerce por su BIMS ID"""
        try:
            # Buscar por meta _bims_id
            products = wc_api.get_products(meta_key='_bims_id', meta_value=str(bims_id))
            
            if products:
                return products[0]
            
            return None
            
        except Exception as e:
            logger.error(f"Error buscando producto en WooCommerce: {str(e)}")
            return None
