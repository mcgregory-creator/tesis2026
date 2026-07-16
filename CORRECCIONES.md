# Correcciones aplicadas al sistema de gestión de envíos

Documento de los errores encontrados tras analizar `app.py`, las plantillas y el
esquema real de PostgreSQL (`schema.sql`), con la solución implementada en cada caso.

---

## 1. CRÍTICO — La API de bitácora insertaba en una columna inexistente

**Problema.** El endpoint `/api/incidencia` hacía:

```sql
INSERT INTO Bitacora_Rutas (id_envio, tipo_evento, cantidad_combustible, precio_unitario)
```

pero en el esquema real la tabla `bitacora_rutas` **no tiene** `precio_unitario`; la
columna se llama `monto_valor` y además existe `descripcion`. Cada reporte del chofer
(gasolina, peaje, novedad, salida, llegada) fallaba con un error de columna inexistente.
En la práctica, la funcionalidad principal del móvil no guardaba nada.

**Solución.** El INSERT ahora usa las columnas reales:

```sql
INSERT INTO bitacora_rutas (id_envio, tipo_evento, descripcion, cantidad_combustible, monto_valor)
```

Se rellena `descripcion` automáticamente según el tipo de evento, `cantidad_combustible`
solo para "Gasolina" y `monto_valor` solo para eventos de gasto.

---

## 2. CRÍTICO — Los formularios de administración no guardaban nada

**Problema.** `agregar_chofer`, `agregar_vehiculo` y `crear_envio` solo mostraban un
mensaje de éxito (`flash`) sin ejecutar ningún `INSERT`. Era imposible dar de alta
choferes, vehículos o rutas de verdad.

**Solución.** Se implementaron los `INSERT` reales contra el esquema:

- `agregar_chofer`: inserta en `usuarios` con `rol = 'Chofer'`, contraseña cifrada
  (`generate_password_hash`) y las tres fechas de vencimiento.
- `agregar_vehiculo`: inserta en `vehiculos` con todos sus parámetros técnicos.
- `crear_envio`: inserta en `envios` resolviendo las claves foráneas y **calculando**
  los costos estimados (ver punto 4).
- Se añadió `agregar_cliente`, necesario porque `envios.id_cliente` es obligatorio.

Todos usan transacciones (`engine.begin()`) que confirman o revierten automáticamente,
y capturan `IntegrityError` para informar de duplicados (usuario/placa repetidos).

---

## 3. CRÍTICO — El estado "En Ruta" nunca se asignaba

**Problema.** El panel del administrador cuenta `envios WHERE estado_envio = 'En Ruta'`,
pero ningún punto del código ponía un envío en ese estado. El envío nacía "Pendiente" y
solo pasaba a "Entregado". El indicador "Rutas en Tránsito" habría mostrado siempre 0.

**Solución.** En `/api/incidencia`:

- Evento **"Salida"** → el envío pasa de `Pendiente` a `En Ruta`.
- Evento **"Llegada"** → el envío pasa a `Entregado` y el vehículo se libera
  (`estado = 'Disponible'`).

Al crear un envío el vehículo pasa a `Asignado`, de modo que "Vehículos en Base"
(disponibles) refleja la realidad de la flota.

---

## 4. Envíos: faltaban cliente y cálculo de costos obligatorios

**Problema.** La tabla `envios` exige `id_cliente`, `id_vehiculo`, `id_chofer`,
`costo_estimado_combustible` y `costo_estimado_mantenimiento` como **NOT NULL**, pero el
modal solo pedía destino, distancia y un campo de texto libre "ID Vehículo/Chofer".
Nunca habría cumplido las restricciones de la base de datos.

**Solución.**

- El modal ahora tiene desplegables poblados desde la BD: clientes, vehículos
  **disponibles** y choferes activos.
- Los costos se calculan automáticamente al crear la ruta:
  - `costo_estimado_combustible = (distancia / km_por_litro) * PRECIO_COMBUSTIBLE`
  - `costo_estimado_mantenimiento = distancia * costo_mantenimiento_km`
- `PRECIO_COMBUSTIBLE` es un parámetro configurable (variable de entorno).

---

## 5. Seguridad — Secretos escritos en el código

**Problema.** La `secret_key` (`'Clave_Secreta_Tesis_2026'`) y las credenciales de la
base de datos estaban escritas directamente en `app.py`. Cualquiera con acceso al código
podía falsificar cookies de sesión y conocer la contraseña de la BD.

**Solución.** Todo se lee de variables de entorno (`.env`): `FLASK_SECRET_KEY`,
`DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `DB_NAME`, `ADMIN_PASSWORD`. Se incluye
`.env.example` como plantilla. Si no hay clave definida, se genera una aleatoria.

---

## 6. Seguridad — `debug=True` expuesto en la LAN

**Problema.** El servidor arrancaba con `debug=True` en `0.0.0.0`, lo que expone el
depurador de Werkzeug (posible ejecución remota de código) a toda la red local.

**Solución.** El modo debug se controla con la variable `FLASK_DEBUG` (0 por defecto).
En el despliegue real queda apagado.

---

## 7. Seguridad — Sin protección CSRF y CORS abierto

**Problema.** Ni los formularios ni la API tenían token CSRF, y `CORS(origins:"*")`
permitía peticiones desde cualquier origen. Combinado con sesiones por cookie, era
vulnerable a peticiones falsificadas.

**Solución.**

- Protección CSRF propia (sin dependencias nuevas): se genera un token por sesión que
  los formularios envían en un campo oculto y el móvil en la cabecera `X-CSRFToken`.
  El middleware valida todo método que modifica datos (POST/PUT/PATCH/DELETE).
- Cookies endurecidas: `HttpOnly` y `SameSite=Lax`.
- CORS deshabilitado por defecto (se activa solo si defines `CORS_ORIGINS`).
- Se regenera la sesión al iniciar sesión (evita fijación de sesión).

---

## 8. Robustez — Sin validación de entrada en la API

**Problema.** La API confiaba en los datos del móvil. Con `parseFloat` sobre un texto no
numérico se enviaba `NaN` a columnas `numeric`, provocando errores. Tampoco verificaba
que la ruta perteneciera al chofer que reportaba.

**Solución.** La API valida el tipo de evento contra una lista blanca, convierte y valida
números, exige monto positivo en gastos, y comprueba que el envío exista **y esté
asignado al chofer en sesión** antes de insertar. El front también valida el monto.

---

## 9. Vistas incompletas y datos "quemados"

**Problema.** `/empleados` y `/choferes` devolvían texto plano (`return f"..."`);
`gestion_tablas.html` mostraba filas de ejemplo escritas a mano y no estaba conectada a
ninguna ruta; `styles.css` existía pero ninguna plantilla lo cargaba.

**Solución.**

- Nueva ruta `/gestion` que renderiza `gestion_tablas.html` con datos reales
  (choferes, vehículos y envíos con sus JOINs).
- `/empleados` muestra una tabla real (`lista_usuarios.html`).
- `/choferes` redirige a `/gestion`.
- Nueva ruta `/configuracion` con plantilla propia.
- `styles.css` ahora se carga desde `base.html`.

---

## 10. Despliegue offline — Bootstrap dependía de internet (CDN)

**Problema.** Todas las plantillas cargaban Bootstrap desde `cdn.jsdelivr.net`. En una
LAN sin internet, la interfaz quedaba sin estilos y —peor— sin el JavaScript de Bootstrap
los modales del administrador **no abrían**. Contradecía el objetivo de funcionar en red
local aislada.

**Solución.** Las plantillas ahora cargan Bootstrap desde `static/vendor/bootstrap/`.
Se incluye el script `descargar_assets.py`, que se ejecuta **una sola vez con internet**
para dejar los archivos locales. A partir de ahí el sistema funciona 100% offline.

---

## Archivos añadidos o modificados

| Archivo | Cambio |
|---|---|
| `app.py` | Reescrito: bugs corregidos, INSERT reales, seguridad, validación |
| `templates/base.html` | Assets locales, meta CSRF, carga de `styles.css` |
| `templates/login.html` | Assets locales + token CSRF |
| `templates/dashboard_chofer.html` | Assets locales, CSRF por cabecera, validación |
| `templates/dashboard_admin.html` | Tokens CSRF, desplegables dinámicos, modal de cliente |
| `templates/gestion_tablas.html` | Conectada a datos reales |
| `templates/lista_usuarios.html` | **Nuevo** — listado de empleados |
| `templates/configuracion.html` | **Nuevo** — parámetros financieros |
| `requirements.txt` | **Nuevo** — dependencias fijadas |
| `.env.example` | **Nuevo** — plantilla de configuración |
| `descargar_assets.py` | **Nuevo** — vendoriza Bootstrap para uso offline |
| `README.md` | **Nuevo** — instrucciones de arranque |

---

## Pendientes recomendados (fuera del alcance de esta corrección)

- Botones "Editar"/"Desactivar" de las tablas (hoy son solo visuales).
- Exportación real de reportes PDF/Excel (el menú existe pero no genera archivos).
- Servir con un servidor WSGI de producción (waitress/gunicorn) en lugar del de
  desarrollo de Flask, incluso en la laptop.
- HTTPS en la LAN si se maneja información sensible.

---

# Segunda revisión (2026-07-11)

Nueva pasada completa sobre el proyecto (`app.py`, plantillas, `requirements.txt` y el
entorno virtual) en busca de errores adicionales.

## 11. Entorno — `python-dotenv` faltaba en el `venv` a pesar de estar en `requirements.txt`

**Problema.** El `venv` del proyecto tenía instaladas todas las dependencias de
`requirements.txt` **excepto** `python-dotenv`. Como `app.py` importa `dotenv` dentro de
un `try/except ImportError` (para no romper si falta), el fallo era silencioso: el
servidor arrancaba sin errores pero **nunca leía el archivo `.env`**. En la práctica,
todas las variables de entorno documentadas (`FLASK_SECRET_KEY`, `DB_PASSWORD`,
`ADMIN_PASSWORD`, `PRECIO_COMBUSTIBLE`, etc.) quedaban ignoradas y el sistema corría
siempre con los valores por defecto inseguros, deshaciendo en la práctica la corrección
de seguridad del punto 5 de la revisión anterior.

**Solución.** Se ejecutó `pip install -r requirements.txt` sobre el `venv` existente
para instalar `python-dotenv==1.0.1` y sincronizarlo con lo declarado en el proyecto.
Se recomienda, tras cualquier cambio en `requirements.txt`, volver a correr ese comando
en el entorno real antes de desplegar.

---

## 12. Robustez — Altas de cliente/chofer/vehículo podían devolver un Error 500

**Problema.** `agregar_cliente` y `agregar_chofer` leían campos obligatorios con
`request.form["campo"]` **dentro** del bloque `try`, pero ese bloque solo capturaba
`IntegrityError`/`SQLAlchemyError`. Si un campo requerido llegaba ausente (formulario
manipulado, petición directa sin pasar por el HTML), se lanzaba un `KeyError` no
capturado y Flask respondía con un Error 500 genérico en lugar del mensaje `flash`
esperado. `crear_envio` ya seguía el patrón correcto (validar primero, insertar
después); los otros tres endpoints de alta no lo seguían.

**Solución.** Se separó cada endpoint en dos fases, igual que `crear_envio`:

1. Una fase de validación que lee y normaliza los campos del formulario, capturando
   `KeyError` y `ValueError` para mostrar un `flash` de error y redirigir.
2. Una fase de inserción que solo maneja errores de base de datos
   (`IntegrityError`/`SQLAlchemyError`).

Además se añadió una comprobación explícita de que los campos de texto obligatorios
(razón social, dirección, nombre, usuario, contraseña, placa, modelo) no queden vacíos
tras `strip()`, algo que el atributo `required` del HTML no garantiza si la petición no
pasa por el formulario.

---

## 13. Robustez — Datos numéricos de vehículos sin validar antes del INSERT

**Problema.** `agregar_vehiculo` insertaba `km_por_litro`, `costo_mantenimiento_km` y
`km_para_mantenimiento` tal cual llegaban del formulario, sin convertir ni validar su
rango. Un valor de `km_por_litro` en cero o negativo no rompía el alta (Postgres lo
acepta, la columna no tiene `CHECK`), pero sí producía costos estimados absurdos o
divisiones por cero silenciosamente "corregidas" a `1.0` más adelante en `crear_envio`
(línea `km_por_litro = float(vehiculo["km_por_litro"]) or 1.0`, pensada solo para el
caso `0`, no para negativos).

**Solución.** `agregar_vehiculo` ahora convierte y valida explícitamente:

- `km_por_litro` debe ser mayor que cero (evita el caso que rompía el cálculo de costo
  de combustible).
- `costo_mantenimiento_km` no puede ser negativo.
- `km_para_mantenimiento` debe ser un entero mayor que cero.
- `capacidad_carga`, si se informa, no puede ser negativa.

Cualquier valor fuera de rango se rechaza con un `flash` claro en vez de guardarse en
la base de datos.

---

## 14. CRÍTICO — Condición de carrera al asignar un vehículo a dos envíos

**Problema.** En `crear_envio`, la disponibilidad del vehículo se comprobaba con un
`SELECT` normal y, más abajo en la misma transacción, se hacía el `INSERT` del envío y
el `UPDATE` de `vehiculos.estado`. Bajo el nivel de aislamiento por defecto de
PostgreSQL (`READ COMMITTED`), dos peticiones `POST /crear_envio` simultáneas para el
mismo vehículo podían **ambas** leer `estado = 'Disponible'` antes de que ninguna
confirmara su transacción, resultando en dos envíos activos para el mismo camión
físico. Es un escenario realista si dos administradores usan el panel a la vez.

**Solución.** El `SELECT` del vehículo ahora usa `FOR UPDATE`:

```sql
SELECT km_por_litro, costo_mantenimiento_km, estado
FROM vehiculos WHERE id_vehiculo = :id FOR UPDATE
```

Esto bloquea la fila del vehículo hasta que la transacción termine (commit o
rollback), de modo que una segunda petición para el mismo vehículo debe esperar y,
al reintentar la lectura, ya verá `estado = 'Asignado'` y será rechazada correctamente
en vez de crear un doble despacho.

---

## Archivos modificados en esta segunda revisión

| Archivo | Cambio |
|---|---|
| `venv/` (entorno local) | Se instaló `python-dotenv` para que coincida con `requirements.txt` |
| `app.py` | Validación robusta en `agregar_cliente`, `agregar_chofer`, `agregar_vehiculo`; bloqueo de fila (`FOR UPDATE`) en `crear_envio` para evitar la doble asignación de vehículos |
| `CORRECCIONES.md` | Documentación de los hallazgos de esta segunda revisión |

---

# Tercera revisión (2026-07-14)

Pasada adicional con motivo de la nueva funcionalidad de ganancia neta (ver
`MEJORAS.md`), que llevó a revisar de nuevo `app.py`, `reportes.py`, las plantillas y
el esquema completo en busca de fallas.

## 15. Inconsistencia — El reporte mensual y el gráfico de productividad usaban
    criterios de fecha distintos para decidir a qué mes pertenece una ruta

**Problema.** `/api/estadisticas` (el gráfico "Viajes entregados por mes" del panel de
administrador) agrupa las rutas por
`COALESCE(fecha_llegada, fecha_creacion)`. Pero `/reportes/mes.pdf` filtraba
estrictamente por `fecha_creacion`. Una ruta creada el 30 de junio y entregada el 2 de
julio contaba para junio en el gráfico del panel (por `fecha_creacion`, ya que
`fecha_llegada` existe pero ambos apuntan a meses distintos — en realidad el gráfico la
contaría en julio porque usa `fecha_llegada` cuando existe) pero para junio en el PDF
mensual (que solo mira `fecha_creacion`). Es decir, un mismo viaje podía aparecer en el
reporte financiero de un mes y en las estadísticas de productividad de otro,
desalineando el "resumen del mes" recién agregado (costo del flete, gastos y ganancia
neta) respecto al resto del sistema.

**Solución.** `reporte_pdf_mes` ahora agrupa por el mismo criterio que
`/api/estadisticas`: `COALESCE(fecha_llegada, fecha_creacion)`. Una ruta ya entregada
se atribuye al mes en que efectivamente generó el gasto/ingreso; una ruta aún pendiente
o en curso (sin `fecha_llegada`) se sigue mostrando en el mes en que fue creada, para no
perderla de los reportes mientras no se complete.

## 16. Rendimiento — Ninguna columna de llave foránea tenía índice

**Problema.** PostgreSQL solo crea un índice automático para la llave primaria de una
tabla; las llaves foráneas (`envios.id_cliente`, `id_vehiculo`, `id_chofer`,
`bitacora_rutas.id_envio`) no tenían ningún índice. Cada vista de detalle de ruta,
cada reporte PDF y cada suma de gastos de bitácora (la nueva base de la ganancia neta)
dependen de filtrar/unir por esas columnas; sin índice, cada una implica un escaneo
secuencial completo de la tabla, que se vuelve más lento a medida que crece el
historial de rutas y bitácora. Tampoco había índice sobre `envios.estado_envio`
(consultado en casi cada pantalla) ni sobre las fechas usadas para agrupar por mes.

**Solución.** Se agregaron índices en `schema.sql`:
`bitacora_rutas(id_envio)`, `envios(id_chofer)`, `envios(id_vehiculo)`,
`envios(estado_envio)`, `envios(fecha_creacion)`, `envios(fecha_llegada)`.
**Importante:** esto solo actualiza la fuente de verdad del esquema (`schema.sql`); si
ya existe una base de datos con datos, hay que aplicar el `ALTER TABLE`/`CREATE INDEX`
correspondiente sobre ella directamente (ver instrucciones de migración entregadas
junto con este cambio).

## 17. Observación de seguridad — Configuración de desarrollo activa en `.env`

**Problema (no corregido, solo señalado).** El `.env` actual tiene `FLASK_DEBUG=1` y
`ADMIN_PASSWORD=admin`. Esto es correcto para desarrollo local, pero si este mismo
archivo se copia tal cual al entorno real de la empresa, quedaría expuesto el
depurador de Werkzeug y la contraseña de administrador por defecto — exactamente lo
que el punto 6 de la primera revisión buscaba evitar. No se modificó porque es
configuración de entorno, no código, pero queda como recordatorio antes de desplegar.

## Archivos modificados en esta tercera revisión

| Archivo | Cambio |
|---|---|
| `app.py` | `reporte_pdf_mes` agrupa por `COALESCE(fecha_llegada, fecha_creacion)`, igual que `/api/estadisticas` |
| `schema.sql` | Índices de rendimiento sobre llaves foráneas, `estado_envio` y fechas |
| `CORRECCIONES.md` | Documentación de esta tercera revisión |
