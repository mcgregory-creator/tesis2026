// Paginacion en el navegador para las tablas largas.
// Cualquier tabla con la clase "tabla-paginada" recibe solo el selector de
// cantidad (25/50/100) y los botones de pagina; no hace falta tocar el html
// de cada tabla. Las filas marcadas con data-vacio (el "sin registros") se
// dejan fuera del conteo.
(function () {
    "use strict";

    var OPCIONES = [25, 50, 100];
    var POR_DEFECTO = 25;

    function paginar(tabla) {
        var cuerpo = tabla.querySelector("tbody");
        if (!cuerpo) return;

        var filas = Array.prototype.filter.call(
            cuerpo.querySelectorAll("tr"),
            function (f) { return !f.hasAttribute("data-vacio"); });

        var contenedor = tabla.closest(".table-responsive") || tabla;
        var porPagina = POR_DEFECTO;
        var actual = 1;

        // barra de arriba: cuantos registros mostrar y el resumen
        var barra = document.createElement("div");
        barra.className = "d-flex justify-content-between align-items-center flex-wrap gap-2 mb-2";
        var izquierda = document.createElement("div");
        izquierda.className = "d-flex align-items-center gap-2";
        izquierda.innerHTML = '<span class="small text-muted">Mostrar</span>';
        var selector = document.createElement("select");
        selector.className = "form-select form-select-sm w-auto";
        OPCIONES.forEach(function (n) {
            var o = document.createElement("option");
            o.value = String(n);
            o.textContent = String(n);
            if (n === POR_DEFECTO) o.selected = true;
            selector.appendChild(o);
        });
        izquierda.appendChild(selector);
        var etiqueta = document.createElement("span");
        etiqueta.className = "small text-muted";
        etiqueta.textContent = "registros";
        izquierda.appendChild(etiqueta);
        var resumen = document.createElement("span");
        resumen.className = "small text-muted";
        barra.appendChild(izquierda);
        barra.appendChild(resumen);
        contenedor.parentNode.insertBefore(barra, contenedor);

        // barra de abajo: los botones de pagina
        var nav = document.createElement("nav");
        nav.className = "mt-2";
        var lista = document.createElement("ul");
        lista.className = "pagination pagination-sm mb-0 flex-wrap";
        nav.appendChild(lista);
        contenedor.parentNode.insertBefore(nav, contenedor.nextSibling);

        function boton(texto, pagina, activo, apagado) {
            var li = document.createElement("li");
            li.className = "page-item" + (activo ? " active" : "") + (apagado ? " disabled" : "");
            var a = document.createElement("a");
            a.className = "page-link";
            a.href = "#";
            a.textContent = texto;
            a.addEventListener("click", function (e) {
                e.preventDefault();
                if (apagado || activo) return;
                actual = pagina;
                pintar();
            });
            li.appendChild(a);
            lista.appendChild(li);
        }

        function pintar() {
            var total = filas.length;
            var paginas = Math.max(1, Math.ceil(total / porPagina));
            if (actual > paginas) actual = paginas;

            var desde = (actual - 1) * porPagina;
            var hasta = Math.min(desde + porPagina, total);
            filas.forEach(function (f, i) {
                f.style.display = (i >= desde && i < hasta) ? "" : "none";
            });
            resumen.textContent = total
                ? "Mostrando " + (desde + 1) + " a " + hasta + " de " + total + " registros"
                : "Sin registros";

            lista.innerHTML = "";
            if (paginas <= 1) { nav.style.display = "none"; return; }
            nav.style.display = "";

            boton("«", actual - 1, false, actual === 1);
            // ventana de 5 paginas alrededor de la actual, con atajos al inicio y al final
            var fin = Math.min(paginas, Math.max(1, actual - 2) + 4);
            var ini = Math.max(1, fin - 4);
            if (ini > 1) boton("1", 1, false, false);
            if (ini > 2) boton("…", 0, false, true);
            for (var i = ini; i <= fin; i++) boton(String(i), i, i === actual, false);
            if (fin < paginas - 1) boton("…", 0, false, true);
            if (fin < paginas) boton(String(paginas), paginas, false, false);
            boton("»", actual + 1, false, actual === paginas);
        }

        selector.addEventListener("change", function () {
            porPagina = parseInt(selector.value, 10) || POR_DEFECTO;
            actual = 1;
            pintar();
        });

        pintar();
    }

    function iniciar() {
        document.querySelectorAll("table.tabla-paginada").forEach(paginar);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", iniciar);
    } else {
        iniciar();
    }
})();
