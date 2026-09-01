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

from core.models import FailedOrder
from core.services import process_order

LOTE = 20


class Command(BaseCommand):
    help = "Procesa la cola de transacciones pendientes."

    def handle(self, *args, **options):
        recuperadas = self._reap_stale()
        if recuperadas:
            self.stdout.write(
                f"Reaper: {recuperadas} fila(s) colgada(s) devuelta(s) a la cola."
            )

        referencias = self._tomar()
        fallidas = 0
        for referencia in referencias:
            try:
                process_order(order_id=referencia)
            except Exception as e:
                # `process_order` ya dejó el FailedOrder en su estado correcto.
                # Tragar acá es deliberado: una orden rota no debe frenar el lote.
                fallidas += 1
                self.stderr.write(f"Orden {referencia} quedó sin facturar: {e}")

        if referencias:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Procesadas {len(referencias) - fallidas} de "
                    f"{len(referencias)} tomada(s)."
                )
            )

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
