"""
Worker de la cola. Corre por cron cada minuto, envuelto en `flock`.

El reaper corre PRIMERO: si un worker murió a mitad de camino su fila quedó en
`PROCESSING` para siempre. Es seguro porque **BIMS deduplica por `_id`**, así que
reprocesar no emite una segunda factura. Sin esa garantía, un reaper sobre datos
fiscales sería inaceptable.
"""

from datetime import timedelta
from typing import List

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import models, transaction
from django.utils.timezone import now

from core.alerts import notify
from core.models import FailedOrder
from core.services import process_order
from core.states import buscar_fila, upsert_state
from core.woocommerce import meta_confirmada, wc_api

LOTE = 20

# Minutos de espera entre intentos contra BIMS. El primero es rápido para
# atrapar el error transitorio; los siguientes esperan a que alguien arregle
# BIMS. Spec §6.4.
BACKOFF_MINUTES = (1, 5, 15, 60)
MAX_BIMS_ATTEMPTS = 5


class Command(BaseCommand):
    help = "Procesa la cola de transacciones pendientes."

    def handle(self, *args, **options):
        recuperadas = self._reap_stale()
        if recuperadas:
            self.stdout.write(
                f"Reaper: {recuperadas} fila(s) colgada(s) devuelta(s) a la cola."
            )

        # La medición va ANTES de tomar el lote: después, las filas de esta
        # pasada están en `PROCESSING` y la cola se ve vacía justo cuando más
        # cargada está. Medirlo al final daba 0 pendientes con 15 esperando.
        medicion = self._medir_cola()

        referencias = self._tomar()
        fallidas = 0
        agotadas = []
        for referencia in referencias:
            try:
                process_order(order_id=referencia)
            except Exception as e:
                # Tragar acá es deliberado: una orden rota no debe frenar el lote.
                # Pero la fila quedó en `PROCESSING`, así que sin agendarle el
                # reintento la rescataría sólo el reaper, diez minutos después.
                fallidas += 1
                self.stderr.write(f"Orden {referencia} quedó sin facturar: {e}")
                if self._schedule_retry(referencia):
                    agotadas.append(str(referencia))

        if referencias:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Procesadas {len(referencias) - fallidas} de "
                    f"{len(referencias)} tomada(s)."
                )
            )

        reparadas, ya_estaban = self._repair_woo_metas()
        if reparadas or ya_estaban:
            self.stdout.write(
                f"Metas en WooCommerce: {reparadas} anotada(s), "
                f"{ya_estaban} ya estaba(n)."
            )

        self._avisar(agotadas, medicion)

    def _reap_stale(self) -> int:
        """
        Devuelve a la cola las filas que un worker tomó y nunca terminó.

        El umbral existe para no robarle la fila a un worker que todavía está
        trabajando: dos workers llamando a BIMS por la misma orden a la vez es
        justo lo que esto viene a evitar.
        """
        limite = now() - timedelta(minutes=settings.QUEUE_REAPER_MINUTES)
        return FailedOrder.objects.filter(
            status=FailedOrder.PROCESSING, claimed_at__lt=limite
        ).update(status=FailedOrder.PENDING, claimed_at=None)

    def _tomar(self) -> List[str]:
        """
        Marca un lote como `PROCESSING` y devuelve sus referencias.

        La marca va ANTES de llamar a BIMS, y en la misma transacción que la
        selección: si el proceso muere durante la llamada, la fila queda tomada y
        el reaper la encuentra. Si se marcara después, quedaría `PENDING` y otro
        worker la tomaría en paralelo.
        """
        with transaction.atomic():
            pendientes = FailedOrder.objects.filter(
                status=FailedOrder.PENDING
            ).filter(
                models.Q(bims_next_attempt__isnull=True)
                | models.Q(bims_next_attempt__lte=now())
            )
            # `SKIP LOCKED` deja que dos corridas solapadas no se peleen por la
            # misma fila. `flock` debería evitar el solapamiento, pero el
            # cinturón no cuesta. En SQLite (los tests) Django lo ignora solo.
            filas = list(
                pendientes.select_for_update(skip_locked=True).order_by("id")[:LOTE]
            )
            FailedOrder.objects.filter(id__in=[f.id for f in filas]).update(
                status=FailedOrder.PROCESSING, claimed_at=now()
            )

        # `order_id` es el rescate para las filas creadas entre el `migrate` y el
        # `restart` del despliegue, que tienen `external_reference` en NULL.
        return [str(f.external_reference or f.order_id) for f in filas]

    def _schedule_retry(self, referencia: str) -> bool:
        """
        Un fallo contra BIMS espera, y a los cinco intentos deja de insistir.

        El backoff es sólo de esta rama a propósito: emitir una factura ante la
        SET no es idempotente desde el lado de BIMS —depende de que deduplique
        por `_id`— así que martillarlo cada minuto no es gratis. La rama de Woo,
        en cambio, se reintenta en cada pasada sin contador.
        """
        # `buscar_fila` y no un `filter` propio: hace el rescate por `order_id`,
        # sin el cual las filas de la ventana del despliegue (referencia en NULL)
        # no se encontrarían y se quedarían tomadas sin contar el intento.
        fila = buscar_fila(referencia)
        if fila is None:
            return False

        intentos = (fila.bims_attempts or 0) + 1
        if intentos >= MAX_BIMS_ATTEMPTS:
            campos = {"status": FailedOrder.FAILED}
        else:
            espera = BACKOFF_MINUTES[min(intentos - 1, len(BACKOFF_MINUTES) - 1)]
            campos = {
                "status": FailedOrder.PENDING,
                "bims_next_attempt": now() + timedelta(minutes=espera),
            }

        upsert_state(
            fila.external_reference or fila.order_id,
            bims_attempts=intentos,
            claimed_at=None,
            **campos,
        )
        return campos["status"] == FailedOrder.FAILED

    def _repair_woo_metas(self):
        """
        Anota en WooCommerce las ventas facturadas a las que les falte la meta.

        **Lee antes de escribir.** El plan original hacía el PUT directo, pero la
        medición del 2026-09-02 mostró que las 82 filas con `woo_meta_ok=False`
        ya tenían la meta correcta en Woo: el PUT directo habrían sido 82
        escrituras inútiles, y cada una dispara `order.updated`, que despierta al
        bot de WhatsApp por una orden vieja. El GET no tiene ese efecto.

        Sin backoff ni contador: el GET es barato y la escritura es idempotente.
        No hay columna donde llevar la cuenta de intentos de esta rama, y
        agregarla costaría una migración para acotar algo que no duele.
        """
        pendientes = FailedOrder.objects.filter(
            status=FailedOrder.COMPLETED,
            woo_meta_ok=False,
            bims_sale_id__isnull=False,
        )[:LOTE]

        reparadas = ya_estaban = 0
        for fila in pendientes:
            referencia = fila.external_reference or fila.order_id
            meta = {"_bims_sale_id": fila.bims_sale_id}
            if fila.bims_invoice_number:
                meta["_bims_invoice_number"] = fila.bims_invoice_number

            try:
                orden = wc_api.get_order(referencia) or {}
                if all(meta_confirmada(orden, k, v) for k, v in meta.items()):
                    ya_estaban += 1
                else:
                    wc_api.update_order_meta(referencia, meta)
                    reparadas += 1
            except Exception as e:
                # Igual que arriba: una orden que no se puede anotar no debe
                # frenar al resto del lote, y la fila queda para la próxima.
                self.stderr.write(f"Orden {referencia} sigue sin anotar: {e}")
                continue

            upsert_state(referencia, woo_meta_ok=True)

        return reparadas, ya_estaban

    def _medir_cola(self):
        """
        Profundidad de la cola y filas vencidas que no avanzan, al empezar la
        pasada.

        `PROCESSING` cuenta como profundidad: son transacciones que todavía no se
        facturaron. Al medirse antes de `_tomar`, las que están ahí son las que
        dejó una pasada anterior, no las de esta.
        """
        profundidad = FailedOrder.objects.filter(
            status__in=(FailedOrder.PENDING, FailedOrder.PROCESSING)
        ).count()

        limite = now() - timedelta(minutes=settings.QUEUE_SILENCE_MINUTES)
        estancadas = (
            FailedOrder.objects.filter(
                status=FailedOrder.PENDING, updated_at__lt=limite
            )
            .filter(
                models.Q(bims_next_attempt__isnull=True)
                | models.Q(bims_next_attempt__lte=now())
            )
            .count()
        )
        return profundidad, estancadas

    def _avisar(self, agotadas: List[str], medicion) -> None:
        """
        Los tres disparadores, al final de la pasada.

        ⚠️ **Ninguno detecta que el cron esté muerto.** El plan atribuía eso al
        tercero, pero es imposible desde acá: si el cron no corre, este código no
        corre y no avisa nada. Un latido de verdad tiene que vivir afuera del
        cron —un chequeo externo que mire la última corrida— y queda fuera del
        alcance de esta tarea. Lo que sí detecta el tercero es que la cola no
        avanza aunque el worker esté corriendo, que es un problema distinto y
        también real.

        Avisar es lo último de `handle` y va sin `try`: `notify` es fail-safe por
        dentro, así que un Slack caído no puede tocar la facturación.
        """
        if agotadas:
            notify(
                "reintentos_agotados",
                f"⛔ {len(agotadas)} orden(es) agotaron sus {MAX_BIMS_ATTEMPTS} "
                f"intentos contra BIMS y quedaron sin facturar: "
                f"{', '.join(agotadas)}. Ya no se reintentan solas.",
            )

        profundidad, estancadas = medicion

        if profundidad >= settings.QUEUE_ALERT_THRESHOLD:
            notify(
                "cola_larga",
                f"⚠️ La cola de facturación tiene {profundidad} transacción(es) "
                f"pendiente(s), por encima del umbral de "
                f"{settings.QUEUE_ALERT_THRESHOLD}. El worker no da abasto o BIMS "
                f"no está contestando.",
            )

        if estancadas:
            notify(
                "cola_estancada",
                f"⚠️ {estancadas} transacción(es) llevan más de "
                f"{settings.QUEUE_SILENCE_MINUTES} minutos vencidas y sin avanzar. "
                f"La cola no se está vaciando.",
            )
