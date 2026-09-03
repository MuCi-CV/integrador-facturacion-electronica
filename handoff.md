# Handoff — sesión 2026-09-03

**Producción:** `main` @ **`5786b3e`**, desplegado 19:49:35 UTC. Sirviendo tráfico, `NRestarts=0`,
cola en 0 pendientes. La última venta del día fue la **205355 → BIMS Sale 31446**.
**`main` local y remoto:** **`19288fd`** — tres commits más que producción, **sin desplegar**.
**Tests:** **307 OK** en local (3.12 + Django 5.2.17) y en el stack de rollback (3.7 + Django 3.2).
El stack real está verificado hasta `5786b3e`; `19288fd` **falta**.

La sesión fue una sola cosa: correr el barrido de stock en seco, descubrir que el modelo estaba mal,
y arreglarlo. **El modo seco se justificó solo** — el bug se vio en una lista en pantalla en lugar
de en la tienda.

---

## 1. Lo que pasó, en orden

**Se desplegó el código de stock en modo seco** (`6bafe9f`, 15:26 UTC) y se corrió el primer barrido
en seco. Encontró un bug bloqueante: `188079` y `188080` (`Ciencias de la tierra`, Online y En
puerta) **no tienen SKU propio**, las dos llegaban con el `575` heredado del padre, y **cada una
recibía las 16 unidades**: 32 publicadas donde hay 16.

**La guarda que debía evitarlo era código muerto.** Contaba "hermanas sin SKU" leyendo el SKU de la
REST, que ya viene heredado, así que `sin_sku` daba 0 y las dos resolvían por la rama de SKU propio.
Sólo podía dispararse cuando el padre **tampoco** tenía SKU, que es justo el caso donde heredar no
sirve de nada.

**Se arregló con el modelo que fijó Carlos:** un producto de BIMS, un destino. Rama
`fix/stock-destino-unico-por-producto`, mergeada a `main`. Y el segundo seco lo validó: **401
vinculados** contra 48, **98 sin contraparte** contra 422, y la guarda de colisión **muda**.

**Después apareció un segundo bug, mío**, al no cerrar un número: el reporte decía 57 variaciones
autogestionadas y las medidas eran 22. Ver la trampa 2 más abajo.

---

## 2. Las tres cosas que el barrido en seco invalidó

Cosas que los documentos daban por establecidas y no lo estaban:

1. **La cobertura.** Los "422 productos inventariables de BIMS sin contraparte en Woo" eran un
   **artefacto** del filtro `status="publish"`, no un hueco del catálogo. **295 de las 318
   variaciones con SKU propio cuelgan de padres `private`**, porque así vive el catálogo del POS de
   FooEvents (`fooeventspos_variation_show_in_pos = yes`). **Un producto `private` se vende.**
2. **Los depósitos.** La lectura de "44 de 48 ya coinciden" era un mal razonamiento:
   `calcular_cambios` saltea **sin distinguirlo** todo producto que BIMS no devolvió, así que "no hay
   dato" y "coincide exacto" se ven iguales en esa resta. Quedó abierto y se cerró después (§3).
3. **El caso de control.** Se había dicho que `CARTAS INFANTILES` no aparecía y que la predicción de
   "16 en la web, 71 en BIMS" había fallado. **No falló:** en el segundo seco salió
   `woo 85226 (bims 109): 15 -> 70`. El barrido era ciego porque el padre (`17373`) es `private`. La
   medición estaba bien; el código estaba mal.

---

## 3. ✅ Los depósitos `6,7` quedaron confirmados

Era lo único que bloqueaba prender el flag. Los 5 destinos que van a 0 son **correctos**: Carlos
confirmó que **BIMS es la fuente de la verdad**, y que `NESCAFÉ` (533) y `JUGO` (530) se dieron de
baja hace meses.

```
APAGA  bims 533   2 -> 0   BOTELLA NESCAFÉ      APAGA  bims 232   1 -> 0   LIBRO ESTRELLA Y PLANETAS
APAGA  bims 530  10 -> 0   JUGO                 APAGA  bims 155   8 -> 0   REVISTA LOS VIAJES DE APOLO
APAGA  bims 207  43 -> 0   Construyendo Ecosistemas con Insectos
```

Los 25 cambios restantes traen desglose coherente de los depósitos 6 y 7, repartidos por todo el
catálogo. **Eso** es la evidencia que la resta del primer seco no daba.

---

## 4. Las dos trampas de la REST de WooCommerce

Las dos costaron un bug, y las dos son del mismo tipo: la API devuelve un valor **derivado** donde
uno espera el crudo.

**1. `get_sku()` devuelve el SKU efectivo.** Cuando la variación no tiene SKU propio, la REST manda
el del padre. "Tiene el 575" y "hereda el 575" llegan **idénticos** y no hay campo que los separe.
Se resuelve comparando con el del padre, y eso vale sólo por un invariante **medido**: no hay ningún
SKU propio numérico repetido en el catálogo (0 filas). **Si ese invariante se rompe, la regla deja de
valer** — para eso está la guarda de colisión.

**2. `manage_stock` de una variación tiene TRES valores:** `true`, `false` y el string
**`"parent"`**, que significa "no gestiono, lo hace mi padre" y aparece **sólo cuando el padre sí
gestiona**. `bool("parent")` es `True`, así que leerlo crudo **invierte el sentido**: marcaba como
problema justamente las variaciones que están bien. Comprobado contra la base:

| variación | `_manage_stock` | padre | lo que dice la REST |
|---|---|---|---|
| `204800/01/02` | `no` | `yes` | **`"parent"`** |
| `192638/39` | `no` | `no` | `false` |
| `1739` | `yes` | `no` | `true` |

Mismo valor en la base, distinta respuesta de la API **según lo que haga el padre**. Costo: 57
reportadas contra 22 reales. Arreglado en `eec60a3` con `gestiona_su_stock()`.

---

## 5. 📌 Regla de los hijos: se escribe el padre, no se tocan los hijos

Aclarado por Carlos el 2026-09-03, y **no hay nada que arreglar**: los contadores por variación
**los mantiene la cajería**. BIMS tiene un producto por **tipo** de taza y no por diseño — `bims 27`
es "TAZA PEQUEÑA - TTKLAB" y no conoce "Newton", "Pato" ni "Muci rosa" —, así que el detalle por
diseño **sólo existe del lado de Woo** y nadie más lo puede llevar.

⚠️ **El reporte decía "hay que apagarles `manage_stock` en Woo"**, que es exactamente lo que **no**
hay que hacer. Corregido en `19288fd`: dejarlo escrito mandaba al próximo lector a romper lo que la
cajería mantiene a mano. Ahora la lista se presenta por su motivo real — que para esas variaciones
el número del padre **no gobierna** la venta, así que no hay que leerlo como lo vendible.

**Consecuencia asumida:** para esos productos, publicar el stock del padre **no cambia lo que la web
vende**. El caso más grande es `woo 11315 (bims 162) 0 -> 192` (`Tazas Pequeñas SC`): sus cuatro
variaciones llevan su propio contador, una con 88 unidades y venta el 30/08.

---

## 6. Para la próxima sesión

**Lo primero:** verificar `19288fd` sobre el stack real. Necesita root, así que lo corre Carlos:

```bash
PYTHON=/root/venv-integrador-52/bin/python SERVIDOR=root@muci.org REMOTO=wt-verificacion-52 ./verificar-en-stack-produccion.sh
```

Después: deploy (`git pull origin main` + `systemctl restart`, sin migraciones) y **tercer barrido en
seco**. Los 30 cambios no se van a mover; lo que cambia es que la lista de autogestionadas baja a
~22 reales y **pueden aparecer flips que el bug ocultaba** — la lista de 5 que se vio está
incompleta. Aprobar esa lista es lo único que queda antes del cron y del flag.

⚠️ **Respaldar `bims_api.log*` antes de cada seco.** Cada corrida escribe varios MB y **rota el log
tres veces**; con `maxBytes=5 MB` y `backupCount=3` eso quema las cuatro ventanas. Las de las 00:03
del 03/09 se perdieron así. Hay respaldo en `/root/bk/bims_api-2026-09-03/`.

⚠️ **La guarda de radio va a pasar raspando:** 5 apagados con tope 5, y aborta con *más* de 5.
Decidido dejarlo en 5 — un aborto avisa y es reversible.

### Pendiente abierto y sin decidir

**176 productos de Woo sin SKU propio ni en el padre, y se venden.** `124861` lleva **2847 líneas**
vendidas, `192638` vendió el **01/09**. Se facturan contra el producto de BIMS del padre, por la
herencia de la REST. **Para el stock ya está resuelto** (el padre es el destino). Lo que queda por
decidir es si esa imputación contable es la deseada o si a esas variaciones les falta SKU propio. No
es un pendiente de la spec de stock.

### 🔴 Seguridad, sigue igual que ayer

- Usuario y contraseña de BIMS **en texto plano** en `wpzv_options` (opciones del plugin `bimsc`,
  inactivo desde 2025). **Borrar esas opciones y rotar la credencial.**
- El reporte a BIMS por la fuga de credencial **sigue sin enviarse**
  (`docs/reportes/2026-08-27-reporte-a-bims.md`).
- ✅ La clave del MySQL `anthropic_readonly` ya está en `~/.my.cnf` con `600`, así que no vuelve a
  pasar por el chat.

### Cosas del servidor que siguen sin resolver

- ⚠️ **El webhook `Refund order` sigue deshabilitado** (`failure_count 6`) y apunta a una URL de
  **staging**. Mientras siga así, toda cancelación se resuelve a mano en los dos sistemas.
- `logrotate` para `/var/log/process-queue.log`, y el mismo cuidado con `/var/log/sync-stock.log`
  cuando se instale.
- **Ningún disparador detecta que el cron esté muerto.** Se comprobó a mano el 03/09 con
  `grep process-queue /var/log/syslog`, porque el log del worker **no imprime nada** cuando la cola
  está vacía: silencio y cron muerto se ven idénticos. Hace falta un **latido externo**.
- ⚠️ **`runretryfaileds.sh` apunta a `/var/www/integrador.muci.org/backend`, que no existe.**

### Estado de las ramas

`fix/stock-destino-unico-por-producto` quedó pusheada y **completamente mergeada** a `main`. Se puede
borrar con `git branch -d` sin perder nada.

⚠️ Y el detalle que engaña: hay **dos remotos al mismo repo**, `github` y `origin`. **El upstream
real es `github`**; la ref local de `origin` está más atrasada, así que un `git log origin/…` da una
cuenta distinta y equivocada. En el servidor, en cambio, el remoto es `origin` y `git pull origin
main` es lo correcto.
