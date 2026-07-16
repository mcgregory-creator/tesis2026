"""
genera los pdf (reportlab) de rutas, choferes y resumenes mensuales.
separado de app.py para no mezclar rutas flask con maquetado
"""
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

_ESTILOS = getSampleStyleSheet()
_ESTILO_CELDA = ParagraphStyle("celda", parent=_ESTILOS["Normal"], fontSize=8, leading=9.5)


def _documento(titulo, pagesize=letter):
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=pagesize,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm,
        leftMargin=1.5 * cm, rightMargin=1.5 * cm,
    )
    elementos = [Paragraph(titulo, _ESTILOS["Title"]), Spacer(1, 0.5 * cm)]
    return buffer, doc, elementos


def _tabla(encabezados, filas, anchos=None, resaltar_columna=None):
    """arma una tabla. las celdas de texto van en Paragraph para que un nombre
    largo (cliente, destino) haga salto de linea en vez de desbordar cuando
    hay anchos fijos.

    resaltar_columna, si se pasa, es el indice de la columna que se pinta en
    rojo cuando el valor es negativo (para ver de una las rutas con perdida)
    """
    filas_render = []
    comandos_extra = []
    for fila_idx, fila in enumerate(filas, start=1):
        fila_render = []
        for col_idx, valor in enumerate(fila):
            if isinstance(valor, str):
                fila_render.append(Paragraph(valor, _ESTILO_CELDA))
            else:
                fila_render.append(valor)
        filas_render.append(fila_render)
        if resaltar_columna is not None:
            try:
                if float(fila[resaltar_columna].replace("$", "").replace(" ", "")) < 0:
                    comandos_extra.append((
                        "TEXTCOLOR", (resaltar_columna, fila_idx), (resaltar_columna, fila_idx),
                        colors.HexColor("#b02a37"),
                    ))
            except (ValueError, AttributeError, IndexError):
                pass

    tabla = Table([encabezados] + filas_render, repeatRows=1, colWidths=anchos)
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#212529")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f9")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        *comandos_extra,
    ]))
    return tabla


def _num(valor):
    return f"{float(valor):.2f}" if valor is not None else "0.00"


def _fecha(valor):
    return valor.strftime("%Y-%m-%d %H:%M") if valor else "—"


def pdf_ruta(envio, bitacora, gastos_ruta):
    """resumen completo de una ruta: datos generales + bitacora de eventos.
    incluye costo operativo, gastos reales de bitacora, costo del flete y
    la ganancia neta"""
    buffer, doc, elementos = _documento(f"Ruta #{envio['id_envio']} — {envio['destino']}")

    costo_operativo = float(envio["costo_estimado_combustible"]) + float(envio["costo_estimado_mantenimiento"])
    costo_flete = float(envio["costo_flete"])
    gastos_ruta = float(gastos_ruta)
    ganancia_neta = round(costo_flete - costo_operativo - gastos_ruta, 2)

    datos_generales = [
        ["Cliente", envio["cliente"]],
        ["Vehículo", f"{envio['vehiculo']} — {envio['vehiculo_modelo']}"],
        ["Chofer", envio["chofer"]],
        ["Distancia", f"{_num(envio['distancia_km'])} km"],
        ["Costo estimado combustible", f"$ {_num(envio['costo_estimado_combustible'])}"],
        ["Costo estimado mantenimiento", f"$ {_num(envio['costo_estimado_mantenimiento'])}"],
        ["Costo Operativo Estimado", f"$ {_num(costo_operativo)}"],
        ["Gastos Reales de Ruta (gasolina adicional, peajes, fallas)", f"$ {_num(gastos_ruta)}"],
        ["Costo del Flete (cobrado al cliente)", f"$ {_num(costo_flete)}"],
        ["Ganancia Neta del Flete", f"$ {_num(ganancia_neta)}"],
        ["Estado", envio["estado_envio"]],
        ["Creado", _fecha(envio["fecha_creacion"])],
        ["Salida", _fecha(envio["fecha_salida"])],
        ["Llegada", _fecha(envio["fecha_llegada"])],
    ]
    tabla_general = _tabla(["Campo", "Valor"], datos_generales)
    fila_ganancia = 10  # encabezado + indice de "ganancia neta del flete"
    color_ganancia = colors.HexColor("#b02a37") if ganancia_neta < 0 else colors.HexColor("#146c43")
    tabla_general.setStyle(TableStyle([
        ("FONTNAME", (0, fila_ganancia), (-1, fila_ganancia), "Helvetica-Bold"),
        ("TEXTCOLOR", (1, fila_ganancia), (1, fila_ganancia), color_ganancia),
    ]))
    elementos.append(tabla_general)
    elementos.append(Spacer(1, 1 * cm))
    elementos.append(Paragraph("Bitácora de eventos", _ESTILOS["Heading2"]))

    filas_bitacora = [
        [
            fila["tipo_evento"],
            fila["descripcion"] or "—",
            _num(fila["cantidad_combustible"]) if fila["cantidad_combustible"] is not None else "—",
            f"$ {_num(fila['monto_valor'])}" if fila["monto_valor"] is not None else "—",
            _fecha(fila["fecha_hora_registro"]),
        ]
        for fila in bitacora
    ]
    elementos.append(_tabla(
        ["Evento", "Descripción", "Litros", "Monto", "Fecha/hora"],
        filas_bitacora or [["Sin eventos registrados", "", "", "", ""]],
    ))

    doc.build(elementos)
    buffer.seek(0)
    return buffer


def pdf_chofer(nombre_chofer, envios):
    """todas las rutas de un chofer, con totales de productividad y plata.
    las anuladas se listan pero no cuentan en los totales de km/costos.
    cada envio debe traer costo_flete, gastos_ruta y su desglose
    (gastos_gasolina, gastos_peaje, gastos_falla)"""
    buffer, doc, elementos = _documento(
        f"Reporte de rutas — {nombre_chofer}", pagesize=landscape(letter),
    )

    anchos = [1.3 * cm, 3.2 * cm, 3.2 * cm, 1.8 * cm, 2.2 * cm, 2.3 * cm, 2.5 * cm, 2.5 * cm, 3.2 * cm]
    filas = []
    for e in envios:
        costo_operativo = float(e["costo_estimado_combustible"]) + float(e["costo_estimado_mantenimiento"])
        costo_flete = float(e["costo_flete"])
        gastos_ruta = float(e["gastos_ruta"])
        costos_totales = costo_operativo + gastos_ruta
        ganancia_neta = round(costo_flete - costos_totales, 2)
        filas.append([
            str(e["id_envio"]), e["cliente"], e["destino"],
            _num(e["distancia_km"]), e["estado_envio"],
            f"$ {_num(costo_flete)}", f"$ {_num(costos_totales)}", f"$ {_num(ganancia_neta)}",
            _fecha(e["fecha_creacion"]),
        ])
    elementos.append(_tabla(
        ["ID", "Cliente", "Destino", "Km", "Estado", "Flete", "Costos Totales", "Ganancia Neta", "Fecha"],
        filas or [["Sin rutas registradas", "", "", "", "", "", "", "", ""]],
        anchos=anchos, resaltar_columna=7,
    ))

    contabilizables = [e for e in envios if e["estado_envio"] != "Anulado"]
    total_km = sum(float(e["distancia_km"]) for e in contabilizables)
    total_combustible = sum(float(e["costo_estimado_combustible"]) for e in contabilizables)
    total_mantenimiento = sum(float(e["costo_estimado_mantenimiento"]) for e in contabilizables)
    total_gastos_gasolina = sum(float(e["gastos_gasolina"]) for e in contabilizables)
    total_gastos_peaje = sum(float(e["gastos_peaje"]) for e in contabilizables)
    total_gastos_falla = sum(float(e["gastos_falla"]) for e in contabilizables)
    total_gastos_ruta = total_gastos_gasolina + total_gastos_peaje + total_gastos_falla
    total_flete = sum(float(e["costo_flete"]) for e in contabilizables)
    total_costos = total_combustible + total_mantenimiento + total_gastos_ruta
    total_ganancia_neta = round(total_flete - total_costos, 2)
    entregados = sum(1 for e in envios if e["estado_envio"] == "Entregado")

    elementos.append(Spacer(1, 1 * cm))
    elementos.append(Paragraph("Totales (no incluyen rutas anuladas)", _ESTILOS["Heading2"]))
    elementos.append(_tabla(
        ["Viajes totales", "Viajes entregados", "Km recorridos",
         "Costo combustible", "Costo mantenimiento",
         "Gastos Gasolina", "Gastos Peajes", "Gastos Fallas",
         "Costo del Flete", "Ganancia Neta"],
        [[
            str(len(envios)), str(entregados), _num(total_km),
            f"$ {_num(total_combustible)}", f"$ {_num(total_mantenimiento)}",
            f"$ {_num(total_gastos_gasolina)}", f"$ {_num(total_gastos_peaje)}",
            f"$ {_num(total_gastos_falla)}", f"$ {_num(total_flete)}",
            f"$ {_num(total_ganancia_neta)}",
        ]],
        resaltar_columna=9,
    ))

    doc.build(elementos)
    buffer.seek(0)
    return buffer


def pdf_mes(anio, mes, envios):
    """todas las rutas de un mes calendario (todos los choferes), con la
    ganancia neta del mes incluida.
    las anuladas se listan pero no cuentan en los totales.
    cada envio debe traer costo_flete, gastos_ruta y su desglose"""
    buffer, doc, elementos = _documento(
        f"Reporte mensual — {mes:02d}/{anio}", pagesize=landscape(letter),
    )

    anchos = [1.1 * cm, 2.7 * cm, 2.7 * cm, 2.1 * cm, 2.7 * cm, 1.7 * cm,
              2.1 * cm, 2.2 * cm, 2.3 * cm, 2.3 * cm]
    filas = []
    for e in envios:
        costo_operativo = float(e["costo_estimado_combustible"]) + float(e["costo_estimado_mantenimiento"])
        costo_flete = float(e["costo_flete"])
        gastos_ruta = float(e["gastos_ruta"])
        costos_totales = costo_operativo + gastos_ruta
        ganancia_neta = round(costo_flete - costos_totales, 2)
        filas.append([
            str(e["id_envio"]), e["cliente"], e["chofer"], e["vehiculo"],
            e["destino"], _num(e["distancia_km"]), e["estado_envio"],
            f"$ {_num(costo_flete)}", f"$ {_num(costos_totales)}", f"$ {_num(ganancia_neta)}",
        ])
    elementos.append(_tabla(
        ["ID", "Cliente", "Chofer", "Vehículo", "Destino", "Km", "Estado",
         "Flete", "Costos Totales", "Ganancia Neta"],
        filas or [["Sin rutas registradas en el periodo", "", "", "", "", "", "", "", "", ""]],
        anchos=anchos, resaltar_columna=9,
    ))

    contabilizables = [e for e in envios if e["estado_envio"] != "Anulado"]
    total_km = sum(float(e["distancia_km"]) for e in contabilizables)
    total_operativo = sum(
        float(e["costo_estimado_combustible"]) + float(e["costo_estimado_mantenimiento"])
        for e in contabilizables
    )
    total_gastos_gasolina = sum(float(e["gastos_gasolina"]) for e in contabilizables)
    total_gastos_peaje = sum(float(e["gastos_peaje"]) for e in contabilizables)
    total_gastos_falla = sum(float(e["gastos_falla"]) for e in contabilizables)
    total_gastos_ruta = total_gastos_gasolina + total_gastos_peaje + total_gastos_falla
    total_flete = sum(float(e["costo_flete"]) for e in contabilizables)
    total_costos = total_operativo + total_gastos_ruta
    total_ganancia_neta = round(total_flete - total_costos, 2)
    entregados = sum(1 for e in envios if e["estado_envio"] == "Entregado")

    elementos.append(Spacer(1, 1 * cm))
    elementos.append(Paragraph("Totales del mes (no incluyen rutas anuladas)", _ESTILOS["Heading2"]))
    elementos.append(_tabla(
        ["Rutas totales", "Rutas entregadas", "Km recorridos",
         "Costo Operativo del Mes",
         "Gastos Gasolina", "Gastos Peajes", "Gastos Fallas",
         "Costo del Flete del Mes", "Ganancia Neta del Mes"],
        [[
            str(len(envios)), str(entregados), _num(total_km),
            f"$ {_num(total_operativo)}",
            f"$ {_num(total_gastos_gasolina)}", f"$ {_num(total_gastos_peaje)}",
            f"$ {_num(total_gastos_falla)}",
            f"$ {_num(total_flete)}", f"$ {_num(total_ganancia_neta)}",
        ]],
        resaltar_columna=8,
    ))

    doc.build(elementos)
    buffer.seek(0)
    return buffer
