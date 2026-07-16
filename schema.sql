-- esquema de la base "logistica_db", exportado con pg_dump --schema-only.
-- no trae datos de negocio, el usuario admin lo crea app.py solo en el
-- primer arranque (inicializar_sistema()). si trae la fila unica de
-- configuracion_financiera porque la tabla la necesita desde el arranque.
--
-- como usarlo:
--   createdb -U postgres logistica_db
--   psql -U postgres -d logistica_db -f schema.sql
--   python app.py   (crea el admin solo la primera vez)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: bitacora_rutas; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bitacora_rutas (
    id_evento integer NOT NULL,
    id_envio integer NOT NULL,
    tipo_evento character varying(50) NOT NULL,
    descripcion text,
    cantidad_combustible numeric(8,2),
    monto_valor numeric(12,2),
    fecha_hora_registro timestamp without time zone DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: bitacora_rutas_id_evento_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.bitacora_rutas ALTER COLUMN id_evento ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.bitacora_rutas_id_evento_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: clientes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.clientes (
    id_cliente integer NOT NULL,
    razon_social character varying(150) NOT NULL,
    direccion_principal text NOT NULL,
    telefono character varying(30)
);


--
-- Name: clientes_id_cliente_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.clientes ALTER COLUMN id_cliente ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.clientes_id_cliente_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: configuracion_financiera; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.configuracion_financiera (
    id smallint DEFAULT 1 NOT NULL,
    precio_combustible numeric(10,4) DEFAULT 0.5 NOT NULL,
    tipo_ganancia character varying(20) DEFAULT 'porcentaje'::character varying NOT NULL,
    valor_ganancia numeric(12,2) DEFAULT 0 NOT NULL,
    CONSTRAINT configuracion_financiera_singleton CHECK ((id = 1))
);


--
-- Name: envios; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.envios (
    id_envio integer NOT NULL,
    fecha_creacion timestamp without time zone DEFAULT CURRENT_TIMESTAMP,
    id_cliente integer NOT NULL,
    id_vehiculo integer NOT NULL,
    id_chofer integer NOT NULL,
    destino text NOT NULL,
    distancia_km numeric(8,2) NOT NULL,
    costo_flete numeric(12,2) DEFAULT 0 NOT NULL,
    costo_estimado_combustible numeric(12,2) NOT NULL,
    costo_estimado_mantenimiento numeric(12,2) NOT NULL,
    estado_envio character varying(30) DEFAULT 'Pendiente'::character varying,
    fecha_salida timestamp without time zone,
    fecha_llegada timestamp without time zone
);


--
-- Name: envios_id_envio_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.envios ALTER COLUMN id_envio ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.envios_id_envio_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: usuarios; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usuarios (
    id_usuario integer NOT NULL,
    nombre_completo character varying(150) NOT NULL,
    usuario character varying(50) NOT NULL,
    password character varying(255) NOT NULL,
    rol character varying(30) NOT NULL,
    estado character varying(20) DEFAULT 'Activo'::character varying,
    debe_cambiar_clave boolean DEFAULT false NOT NULL,
    vencimiento_licencia date,
    vencimiento_certificado_medico date,
    vencimiento_cedula date
);


--
-- Name: usuarios_id_usuario_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.usuarios ALTER COLUMN id_usuario ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.usuarios_id_usuario_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: vehiculos; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vehiculos (
    id_vehiculo integer NOT NULL,
    placa character varying(20) NOT NULL,
    modelo character varying(100) NOT NULL,
    capacidad_carga numeric(8,2),
    km_por_litro numeric(8,2) NOT NULL,
    costo_mantenimiento_km numeric(12,2) NOT NULL,
    km_para_mantenimiento integer NOT NULL,
    estado character varying(30) DEFAULT 'Disponible'::character varying,
    vencimiento_rcv date,
    vencimiento_impuesto_alcaldia date
);


--
-- Name: vehiculos_id_vehiculo_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.vehiculos ALTER COLUMN id_vehiculo ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.vehiculos_id_vehiculo_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: bitacora_rutas bitacora_rutas_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bitacora_rutas
    ADD CONSTRAINT bitacora_rutas_pkey PRIMARY KEY (id_evento);


--
-- Name: clientes clientes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.clientes
    ADD CONSTRAINT clientes_pkey PRIMARY KEY (id_cliente);


--
-- Name: configuracion_financiera configuracion_financiera_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.configuracion_financiera
    ADD CONSTRAINT configuracion_financiera_pkey PRIMARY KEY (id);


--
-- Name: envios envios_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.envios
    ADD CONSTRAINT envios_pkey PRIMARY KEY (id_envio);


--
-- Name: usuarios usuarios_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usuarios
    ADD CONSTRAINT usuarios_pkey PRIMARY KEY (id_usuario);


--
-- Name: usuarios usuarios_usuario_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usuarios
    ADD CONSTRAINT usuarios_usuario_key UNIQUE (usuario);


--
-- Name: vehiculos vehiculos_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vehiculos
    ADD CONSTRAINT vehiculos_pkey PRIMARY KEY (id_vehiculo);


--
-- Name: vehiculos vehiculos_placa_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vehiculos
    ADD CONSTRAINT vehiculos_placa_key UNIQUE (placa);


--
-- Name: bitacora_rutas bitacora_rutas_id_envio_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bitacora_rutas
    ADD CONSTRAINT bitacora_rutas_id_envio_fkey FOREIGN KEY (id_envio) REFERENCES public.envios(id_envio) ON DELETE CASCADE;


--
-- Name: envios envios_id_chofer_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.envios
    ADD CONSTRAINT envios_id_chofer_fkey FOREIGN KEY (id_chofer) REFERENCES public.usuarios(id_usuario) ON DELETE RESTRICT;


--
-- Name: envios envios_id_cliente_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.envios
    ADD CONSTRAINT envios_id_cliente_fkey FOREIGN KEY (id_cliente) REFERENCES public.clientes(id_cliente) ON DELETE RESTRICT;


--
-- Name: envios envios_id_vehiculo_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.envios
    ADD CONSTRAINT envios_id_vehiculo_fkey FOREIGN KEY (id_vehiculo) REFERENCES public.vehiculos(id_vehiculo) ON DELETE RESTRICT;


-- indices de rendimiento: postgres no los crea solo para llaves foraneas
-- (solo para la primaria). sin esto, los join/where por chofer, vehiculo,
-- estado o fecha, y la suma de gastos por ruta, escanean toda la tabla

CREATE INDEX idx_bitacora_rutas_id_envio ON public.bitacora_rutas USING btree (id_envio);
CREATE INDEX idx_envios_id_chofer ON public.envios USING btree (id_chofer);
CREATE INDEX idx_envios_id_vehiculo ON public.envios USING btree (id_vehiculo);
CREATE INDEX idx_envios_estado_envio ON public.envios USING btree (estado_envio);
CREATE INDEX idx_envios_fecha_creacion ON public.envios USING btree (fecha_creacion);
CREATE INDEX idx_envios_fecha_llegada ON public.envios USING btree (fecha_llegada);

-- fila unica de configuracion financiera (valores por defecto)

INSERT INTO public.configuracion_financiera (id, precio_combustible, tipo_ganancia, valor_ganancia)
VALUES (1, 0.5, 'porcentaje', 0);

--
-- PostgreSQL database dump complete
--
