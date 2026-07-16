# Nuevas funcionalidades — productividad y control operativo

Este documento describe las funcionalidades añadidas al sistema (distinto de
`CORRECCIONES.md`, que solo cubre bugs). Incluye las decisiones de diseño tomadas y
algunas mejoras adicionales incorporadas durante la implementación.

---

## 1. Panel del chofer simplificado: Iniciar / Actualizar / Finalizar

**Antes** había 5 botones (Salida, Gasolina, Peaje, Novedad, Llegada) con `prompt()` de
JavaScript para pedir montos, sin validar nada hasta el envío.

**Ahora** son 3 acciones, habilitadas según el estado de la ruta:

- **Iniciar Ruta** (solo si está `Pendiente`): registra la salida y guarda
  `envios.fecha_salida`.
- **Actualizar** (solo si está `En Ruta`): abre un modal con 3 pestañas:
  - **Gasolina**: litros + (precio por litro **o** precio total). El campo que falta se
    calcula automáticamente con JavaScript a medida que se escribe el otro.
  - **Peaje**: nombre del peaje + monto. El nombre viaja como `descripcion` en el
    evento (antes era un texto fijo "Pago de peaje").
  - **Falla mecánica**: descripción de la falla + monto. Igual que Peaje, la
    descripción ahora es libre en vez de un texto fijo.
- **Finalizar Ruta** (solo si está `En Ruta`): registra la llegada, guarda
  `envios.fecha_llegada`, marca el envío `Entregado` y libera el vehículo.

`dashboard_chofer.html` pasó a extender `base.html` (antes era una página HTML
independiente) para reutilizar la barra de navegación, el token CSRF y el nuevo modal
de cambio de contraseña sin duplicar código.

---

## 2. Panel de productividad para el administrador

En `dashboard_admin.html`:

- Tarjeta KPI adicional: **Rutas Finalizadas** (junto a las ya existentes "En Tránsito"
  y "Vehículos en Base").
- **Gráfico de barras** (Chart.js, vendorizado para uso offline en
  `static/vendor/chartjs/`) con los viajes **entregados** por mes del año actual.
- **Top 5 destinos más frecuentes** y **Top 5 choferes con más viajes**, calculados
  también solo sobre envíos `Entregado` — la idea es medir productividad real, no
  planificación futura. Todo se alimenta desde el nuevo endpoint `GET /api/estadisticas`.

## 3. Exportación de reportes en PDF

Nueva página `/reportes` con tres exportaciones (usa `reportlab`, agregado a
`requirements.txt`; la lógica de maquetado vive en el nuevo módulo `reportes.py`):

- **Por ruta** (`/reportes/ruta/<id>.pdf`): datos generales de la ruta + bitácora
  completa de eventos.
- **Por chofer** (`/reportes/chofer/<id>.pdf`): todas sus rutas + totales (viajes,
  viajes entregados, km recorridos, costo de combustible y mantenimiento).
- **Por mes** (`/reportes/mes.pdf?anio=&mes=`): todas las rutas de ese mes calendario,
  de todos los choferes, con totales.

También se puede ver el resumen de una ruta en pantalla (sin descargar) desde
`/gestion/envio/<id>`, enlazado con un botón "Ver" en la tabla de Envíos/Rutas.

---

## 4. Validación de documentos vigentes al despachar

**Antes**, `crear_envio` solo validaba que el vehículo estuviera "Disponible"; no se
comprobaba si el chofer o el vehículo tenían algún documento vencido.

**Ahora**, tanto al crear una ruta como al editarla, se valida:

- **Chofer**: licencia de conducir, certificado médico y cédula.
- **Vehículo**: RCV e impuesto de alcaldía.

Un documento se considera "vigente" si tiene fecha registrada **y** no ha vencido
(`fecha >= hoy`). Si algo falta o está vencido, la operación se bloquea con un mensaje
indicando exactamente qué documento revisar. Esas fechas se actualizan desde
"Gestión de Tablas" → **Editar** (choferes/vehículos).

### Mejora adicional: un chofer no puede tener dos rutas activas a la vez

El dashboard del chofer solo muestra **una** ruta a la vez (`LIMIT 1`), pero nada
impedía asignarle una segunda ruta mientras la primera seguía activa — quedaría
invisible para él hasta entregar la primera. Se agregó una validación
(`_chofer_con_ruta_activa`) que bloquea crear o reasignar una ruta a un chofer que ya
tiene una `Pendiente` o `En Ruta`.

---

## 5. Edición y anulación de rutas, choferes y vehículos

Todo esto se agregó en "Gestión de Tablas" (`gestion_tablas.html`), con un botón
**Editar** por fila que abre un modal:

- **Choferes**: editar nombre, usuario, estado (Activo/Inactivo), las 3 fechas de
  vencimiento, y opcionalmente una nueva contraseña (dejar en blanco = no cambiar).
- **Vehículos**: editar todos los datos técnicos y fechas de vencimiento. El estado
  solo puede alternarse entre `Disponible` y `Fuera de Servicio` — mientras el
  vehículo está `Asignado` (en ruta activa) el sistema no permite tocar el estado
  manualmente, para no pisar la lógica automática de asignación.
- **Envíos/Rutas**: botones **Editar** y **Anular**, visibles **solo** mientras la
  ruta está `Pendiente` o `En Ruta` (decisión confirmada con el cliente: una vez
  `Entregado`, la ruta queda fija como historial para no alterar retroactivamente los
  reportes de productividad).
  - **Editar** permite reasignar cliente, vehículo y chofer completos, no solo destino
    y distancia — revalida disponibilidad, documentos vigentes y que el nuevo chofer no
    tenga ya otra ruta activa. Si el vehículo cambia, libera el anterior y asigna el
    nuevo dentro de la misma transacción (con `FOR UPDATE` sobre ambas filas para
    evitar condiciones de carrera).
  - **Anular** pone la ruta en estado `Anulado` y libera el vehículo si estaba
    asignado a ella.

En `lista_usuarios.html` (empleados/administradores) se agregó **Resetear Contraseña**
y **Activar/Desactivar** por fila.

---

## 6. Cambio de contraseña

- **Autoservicio**: modal "Mi Cuenta" en la barra de navegación (`base.html`),
  disponible para cualquier rol. Pide la contraseña actual, la nueva y su confirmación
  (mínimo 8 caracteres).
- **Reseteo por el administrador**: botón "Resetear Contraseña" en choferes
  (dentro del modal de edición) y en empleados/administradores
  (`lista_usuarios.html`). No pide la contraseña anterior — es una acción
  administrativa explícita.

---

## 7. Configuración financiera editable: combustible y margen de ganancia

**Antes**, `PRECIO_COMBUSTIBLE` solo se podía cambiar editando `.env` y reiniciando el
servidor, y la pantalla `/configuracion` era de solo lectura (además, no había ningún
botón en el panel principal que llevara a ella).

**Ahora**:

- Nueva tabla `configuracion_financiera` (fila única) guarda el precio del combustible
  y el margen de ganancia. `/configuracion` pasó de página de solo lectura a un
  endpoint `POST` que guarda los cambios al instante en la base de datos, sin
  reiniciar nada.
- **Actualización**: la edición ya no vive en una página propia — se movió a un modal
  ("Configuración Financiera") dentro del panel principal del administrador, con el
  mismo formulario y el mismo ejemplo de cotización en vivo. Se eliminó
  `templates/configuracion.html` y la ruta ya no acepta `GET`.
- Margen de ganancia con dos modos, elegibles desde la misma pantalla:
  - **Porcentaje**: se aplica sobre (costo de combustible + costo de mantenimiento).
  - **Monto fijo**: un monto en dólares que se suma tal cual.
- El "Costo de Estimación de Ruta" del modal Crear Envío ahora tiene una línea de
  **Ganancia** además de Mantenimiento y Combustible, y el "Total a Cotizar" las suma
  las tres — así el estimado que ve el administrador coincide exactamente con lo que
  `crear_envio`/`editar_envio` calculan al guardar la ruta (mismo helper
  `_calcular_costos_ruta`, misma fuente de configuración).
- La ganancia es **solo para cotizar**: no se persiste por ruta ni se resta/suma a
  `envios.costo_estimado_combustible`/`costo_estimado_mantenimiento`, que siguen
  reflejando el costo interno real (por eso los reportes de "Costo Total" de secciones
  anteriores no cambian con el margen de ganancia).
- Se agregó un botón "Configuración Financiera" en el panel principal del
  administrador (antes no existía ningún enlace visible a `/configuracion`).
- `PRECIO_COMBUSTIBLE` se eliminó de `.env`/`.env.example`: ya no se lee del entorno,
  vive en la base de datos y se edita desde la UI.

---

## Cambios de esquema

**Actualización:** el `schema.sql` binario original (respaldo de `pg_restore`) y los
archivos intermedios `schema_v2.sql`, `migracion_v2.sql` y `migracion_v3.sql` ya
**no existen** en el proyecto. Se reemplazaron por un único `schema.sql` nuevo, en
texto plano, exportado con `pg_dump --schema-only` directamente desde la base de
datos ya al día (con las 5 tablas originales + `fecha_salida`/`fecha_llegada` en
`envios` + la tabla `configuracion_financiera`). Se verificó aplicándolo sobre una
base vacía antes de borrar los archivos anteriores.

```sql
CREATE TABLE configuracion_financiera (
    id smallint PRIMARY KEY DEFAULT 1,
    precio_combustible numeric(10,4) NOT NULL DEFAULT 0.5,
    tipo_ganancia character varying(20) NOT NULL DEFAULT 'porcentaje',
    valor_ganancia numeric(12,2) NOT NULL DEFAULT 0,
    CONSTRAINT configuracion_financiera_singleton CHECK (id = 1)
);
```

No se necesitaron columnas nuevas en `bitacora_rutas` (el campo `descripcion`, ya
existente, ahora guarda el texto libre del chofer) ni en `vehiculos.estado` /
`envios.estado_envio` (son `varchar` sin `CHECK`; los valores nuevos —
`'Fuera de Servicio'` y `'Anulado'` — los controla la aplicación).

## Archivos nuevos

| Archivo | Propósito |
|---|---|
| `reportes.py` | Generación de PDFs (reportlab): por ruta, por chofer, por mes |
| `templates/detalle_envio.html` | Vista de resumen de una ruta + su bitácora |
| `templates/reportes.html` | Formularios de exportación PDF |

## Pendientes recomendados (fuera del alcance de esta ampliación)

- Los rankings de "destino más frecuente" agrupan por texto exacto de `destino`; si se
  quiere evitar que "Maracaibo" y "maracaibo " cuenten como destinos distintos,
  convendría normalizar ese campo (catálogo de destinos) en una futura mejora.
- El gráfico de viajes por mes está fijo al año actual; se podría añadir un selector de
  año si se necesita comparar años anteriores.
- No hay auditoría de quién hizo cada edición/anulación (no se registra un log de
  cambios administrativos); si se requiere trazabilidad total, se podría agregar una
  tabla de auditoría.

---

# Ganancia neta real del flete (2026-07-14)

**Antes**, ningún resumen reflejaba la ganancia real de la compañía: `envios` solo
guardaba los costos internos estimados (combustible y mantenimiento), y no existía
ningún campo para lo que realmente se cobraba al cliente por el flete. La "ganancia"
configurable en `/configuracion` era únicamente un margen sugerido para cotizar — el
propio código dejaba explícito que "no se persiste por ruta". Tampoco se restaban los
gastos reales de ruta (gasolina adicional, peajes, fallas mecánicas) registrados en
`bitacora_rutas`.

## Decisión de diseño: qué se persiste y qué se calcula al vuelo

Se evaluaron dos opciones — calcular la ganancia neta en cada resumen, o agregar una
columna que la guarde al finalizar la ruta — y se optó por un enfoque mixto:

- **`envios.costo_flete` (nueva columna, persistida):** lo cobrado al cliente es un
  dato de negocio que el administrador conoce al crear la ruta (igual que
  `distancia_km`); no se puede derivar de nada, así que debe guardarse.
- **`ganancia_neta` (calculada al vuelo, NO persistida):** sí se puede derivar de datos
  que ya existen (`costo_flete` − costos estimados − suma de gastos en
  `bitacora_rutas`). No conviene guardarla como columna aparte porque los gastos de
  bitácora se siguen registrando mientras la ruta está "En Ruta" — una columna
  persistida quedaría desactualizada hasta recalcularla en cada evento del chofer
  (gasolina/peaje/falla), agregando escrituras y una fuente adicional de datos que se
  puede desincronizar del resto. El cálculo (una suma agregada indexada por
  `id_envio`) es barato a la escala de esta operación.

## Cambios

- **Esquema:** `envios.costo_flete numeric(12,2) NOT NULL DEFAULT 0`.
- **`crear_envio` / `editar_envio`:** nuevo campo obligatorio (> 0) para el costo del
  flete.
- **Modal "Crear Envío"** (`dashboard_admin.html`): el campo se autocompleta con el
  "Total a Cotizar" ya calculado (combustible + mantenimiento + margen configurado),
  pero deja de autocompletarse en cuanto el administrador lo edita a mano — así puede
  usar la sugerencia o registrar el precio realmente acordado con el cliente.
- **`_gastos_ruta` / `_SUBCONSULTA_GASTOS_RUTA`** (`app.py`): suma de
  `bitacora_rutas.monto_valor` para eventos `Gasolina`, `Peaje` y `Novedad` (gasolina
  adicional, peajes y fallas mecánicas — no incluye `Salida`/`Llegada`, que no son
  gastos).
- **`_ganancia_neta`** (`app.py`): `costo_flete − costo_estimado_combustible −
  costo_estimado_mantenimiento − gastos_ruta`.
- **Resumen en pantalla** (`/gestion/envio/<id>` → `detalle_envio.html`): agrega Costo
  del Flete, Gastos Reales de Ruta y Ganancia Neta del Flete (en verde o rojo según el
  signo).
- **Los tres reportes en PDF** (`reportes.py`):
  - **Por ruta:** mismo desglose completo que la vista en pantalla.
  - **Por chofer y por mes:** se agregaron las columnas Flete, Costos Totales y
    Ganancia Neta por cada ruta, más los totales agregados (ganancia neta del período).
    Pasaron a orientación horizontal (`landscape`) para que las columnas nuevas quepan
    sin desbordar la página, y las celdas de texto largo (cliente, destino) ahora usan
    `Paragraph` con ancho de columna fijo para que hagan salto de línea en vez de
    desbordar. La ganancia neta negativa se resalta en rojo, fila por fila y en el
    total, para detectar de un vistazo qué rutas dieron pérdida.

## Archivos modificados

| Archivo | Cambio |
|---|---|
| `schema.sql` | Columna `costo_flete`; índices de rendimiento (ver `CORRECCIONES.md`) |
| `app.py` | `costo_flete` en alta/edición de envíos; helpers de gastos de bitácora y ganancia neta; reportes PDF reciben los datos financieros completos |
| `reportes.py` | Ganancia neta en los 3 reportes; tablas en landscape con `Paragraph` y resaltado de pérdidas |
| `templates/dashboard_admin.html` | Campo "Costo del Flete" con autocompletado desde la cotización sugerida |
| `templates/gestion_tablas.html` | Campo "Costo del Flete" en la edición de rutas |
| `templates/detalle_envio.html` | Desglose financiero completo con ganancia neta |

## Ajustes posteriores (2026-07-15)

A partir de la revisión de sugerencias del punto anterior, se implementaron dos
mejoras adicionales (se descartó explícitamente la de auditoría de
ediciones/anulaciones: al ser un proyecto piloto con un único usuario
administrador, todas las altas y ediciones las hace la misma persona, así que un
registro de "quién hizo qué" no aporta nada por ahora):

- **Desglose de gastos por categoría en los reportes "por chofer" y "por mes".**
  Antes, la tabla de Totales de ambos reportes solo mostraba "Gastos de ruta" como
  un único monto agregado. Ahora se desglosa en tres columnas — Gastos Gasolina,
  Gastos Peajes, Gastos Fallas — usando `SUM(monto_valor) FILTER (WHERE
  tipo_evento = ...)` en la subconsulta `_SUBCONSULTA_GASTOS_RUTA` de `app.py`. La
  tabla itemizada por ruta no cambió (sigue mostrando "Costos Totales" combinado);
  el desglose se agregó a nivel de Totales, que es donde tiene sentido responder
  "¿en qué se fue la plata este mes/con este chofer?" sin saturar cada fila con 3
  columnas más.
- **Tarjeta KPI "Ganancia Neta (Mes Actual)"** en el panel principal del
  administrador (`inicio()` en `app.py` + `dashboard_admin.html`), junto a las 3
  tarjetas existentes (Rutas en Tránsito, Vehículos en Base, Rutas Finalizadas).
  Usa exactamente el mismo criterio que `/reportes/mes.pdf` — mismo mes por
  `COALESCE(fecha_llegada, fecha_creacion)`, mismas rutas excluidas (solo
  Anuladas) — a propósito, para que la cifra de la tarjeta y la del PDF del mes
  siempre coincidan. Se pinta en verde o rojo según el signo.
