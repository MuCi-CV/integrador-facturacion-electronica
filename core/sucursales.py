"""
Sucursales: resolución del punto de venta y completado de datos contra WooCommerce.

El mapeo cajero POS → punto de venta de BIMS vivía hardcodeado en
`core/constants.py`, así que agregar una sucursal exigía editar código y
redesplegar. Ahora vive en la tabla `Sucursal` y se edita desde el admin.

**Las constantes quedan como red de seguridad.** Si la tabla está vacía o la
consulta falla, se usan ellas y se loguea el desvío. Es deliberado: una tabla
nueva no puede tener el poder de frenar la facturación.
"""

import logging
from typing import Optional

import requests
from django.db import DatabaseError

from core.constants import (
    POS_DEFAULT_POSALE_ID,
    POS_USER_ID_TO_POSALE,
    WEB_POSALE_ID,
)
from core.models import Sucursal
from core.woocommerce import WooCommerceAPI, wc_api

logger = logging.getLogger(__name__)

ROL_CAJERO = "fooeventspos_cashier"


def resolver_posale_de_cajero(wp_user_id: int) -> Optional[int]:
    """
    Punto de venta de BIMS para un cajero POS.

    `None` significa **no facturar**: o el cajero está registrado sin punto de
    venta (la cuenta de administrador, o uno dado de baja), o la regla
    `pos_sin_mapeo` quedó sin punto de venta. Un cajero no registrado cae en esa
    regla, no en `None`.
    """
    try:
        sucursal = Sucursal.objects.filter(
            tipo=Sucursal.CAJERO, wp_user_id=wp_user_id
        ).first()
        if sucursal is not None:
            return sucursal.bims_posale_id

        regla = Sucursal.objects.filter(tipo=Sucursal.POS_SIN_MAPEO).order_by("id").first()
        if regla is not None:
            logger.info(
                f"Cajero {wp_user_id} sin sucursal registrada: "
                f"usando la regla '{regla.nombre}' (punto de venta {regla.bims_posale_id})."
            )
            return regla.bims_posale_id
    except DatabaseError as e:
        logger.error(
            f"Sucursales: falló la consulta para el cajero {wp_user_id} ({e}). "
            f"Usando el mapeo de core/constants.py."
        )
    else:
        logger.warning(
            "Sucursales: la tabla no tiene filas utilizables. "
            "Usando el mapeo de core/constants.py."
        )
    return _posale_de_constantes(wp_user_id)


def resolver_posale_web() -> Optional[int]:
    """
    Punto de venta para órdenes que llegan sin cajero POS.

    `None` significa no facturar, igual que en `resolver_posale_de_cajero`.
    """
    try:
        regla = Sucursal.objects.filter(tipo=Sucursal.WEB).order_by("id").first()
        if regla is not None:
            return regla.bims_posale_id
    except DatabaseError as e:
        logger.error(
            f"Sucursales: falló la consulta de la regla web ({e}). "
            f"Usando WEB_POSALE_ID de core/constants.py."
        )
    else:
        logger.warning(
            "Sucursales: no hay regla de órdenes web. "
            "Usando WEB_POSALE_ID de core/constants.py."
        )
    return WEB_POSALE_ID


def _posale_de_constantes(wp_user_id: int) -> Optional[int]:
    """
    Respaldo sobre `POS_USER_ID_TO_POSALE`.

    Ojo con el `in`: la clave `2` mapea a `None` (administrador, no facturar).
    Un `.get(wp_user_id, POS_DEFAULT_POSALE_ID)` lo confundiría con "no
    encontrado" y le asignaría el punto de venta por defecto — es decir,
    facturaría lo que no se debe facturar.
    """
    if wp_user_id in POS_USER_ID_TO_POSALE:
        return POS_USER_ID_TO_POSALE[wp_user_id]
    return POS_DEFAULT_POSALE_ID


def completar_desde_woocommerce(sucursal: Sucursal) -> Optional[str]:
    """
    Completa email ↔ `wp_user_id` consultando WooCommerce.

    Devuelve un aviso para mostrarle al usuario, o `None` si todo salió bien.
    **Nunca lanza**: el alta de una sucursal no puede depender de que
    WooCommerce esté arriba. Ante un fallo se guarda lo que se cargó y se avisa.

    Las reglas por defecto (`web`, `pos_sin_mapeo`) no tienen usuario, así que
    se saltean sin tocar la red.
    """
    if sucursal.tipo in Sucursal.TIPOS_SIN_USUARIO:
        return None

    tiene_email = bool(sucursal.email)
    tiene_id = sucursal.wp_user_id is not None

    if tiene_email and tiene_id:
        return None
    if not tiene_email and not tiene_id:
        return "Cargá el email o el ID del cajero para que el integrador complete el otro."

    try:
        if tiene_email:
            cliente = wc_api.find_customer_by_email(sucursal.email)
            if cliente is None:
                return (
                    f"No se encontró ningún usuario de WordPress con el email "
                    f"{sucursal.email}. Se guardó igual, pero sin ID no se van a "
                    f"reconocer sus órdenes."
                )
        else:
            cliente = wc_api.get_customer(sucursal.wp_user_id)
            if cliente is None:
                return (
                    f"No existe el usuario de WordPress con ID {sucursal.wp_user_id}. "
                    f"Se guardó igual; verificá el número."
                )
    except (requests.RequestException, WooCommerceAPI.ServerException) as e:
        return (
            f"No se pudo consultar WooCommerce ({e}). Se guardó lo que cargaste, "
            f"pero quedó incompleto: volvé a guardar cuando WooCommerce responda."
        )

    sucursal.wp_user_id = cliente.get("id") or sucursal.wp_user_id
    sucursal.email = cliente.get("email") or sucursal.email

    rol = cliente.get("role")
    if rol != ROL_CAJERO:
        return (
            f"El usuario {sucursal.email} (ID {sucursal.wp_user_id}) tiene rol "
            f"'{rol}' y no '{ROL_CAJERO}'. Se guardó igual, pero verificá que sea "
            f"el cajero correcto."
        )
    return None
