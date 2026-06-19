import re
import io
import unicodedata
import difflib
from pathlib import Path

import pandas as pd
import pdfplumber
import streamlit as st

# ============================================================
#  Catálogo de colonias
# ============================================================

CATALOGO_PATH = Path(__file__).parent / "colonias.csv"


def strip_accents(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))


def normalize_colonia(s):
    s = strip_accents(s or "")
    s = s.upper().replace("�", " ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@st.cache_data
def cargar_catalogo():
    df = pd.read_csv(CATALOGO_PATH, encoding="utf-8-sig")
    nombres = df.iloc[:, 0].dropna().astype(str).str.strip()
    nombres = [n for n in nombres if n]

    catalogo = {}  # normalizado -> nombre original (canónico)
    for nombre in nombres:
        norm = normalize_colonia(nombre)
        if norm and norm not in catalogo:
            catalogo[norm] = nombre

    return catalogo, list(catalogo.keys())


def emparejar_colonia(texto, catalogo, claves_norm, umbral=0.72):
    """Busca la colonia detectada dentro del catálogo oficial.

    El PDF pierde acentos (los reemplaza por "<25>"), así que la
    comparación se hace sobre texto normalizado (sin acentos/puntuación).
    """
    norm = normalize_colonia(texto)
    if not norm:
        return texto, 0.0, False

    if norm in catalogo:
        return catalogo[norm], 1.0, True

    candidatos = difflib.get_close_matches(norm, claves_norm, n=1, cutoff=umbral)
    if candidatos:
        mejor = candidatos[0]
        score = difflib.SequenceMatcher(None, norm, mejor).ratio()
        return catalogo[mejor], round(score, 2), True

    return texto, 0.0, False


# ============================================================
#  Lógica de extracción del PDF de reportes SIAPA
# ============================================================

LABEL_TOKENS = {
    "Direccion", ":", "Motivo", "Inspección", "Inspeccion",
    "Instalación", "Instalacion", "SONDEO", "RED",
    "Clasificación", "Clasificacion", "INSPECCION", "QUEJAS",
    "Observaciones", "Observaciones:"
}

# Bandas de columnas (x0) determinadas a partir del layout real del PDF
COL_FOLIO_X = (35, 115)
COL_FECHA_X = (105, 162)
COL_REPORTO_X = (224, 363)
COL_ENTRECALLE_X = (363, 585)
COL_TELEFONO_X = (585, 690)
COL_DIRECCION_X = (65, 400)
COL_OBSERVACIONES_X = (465, 830)


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def words_to_text(words):
    if not words:
        return ""

    words = sorted(words, key=lambda w: (round(w["top"] / 3) * 3, w["x0"]))
    lines = []
    current = []
    current_y = None

    for w in words:
        y = round(w["top"] / 3) * 3

        if current_y is None or abs(y - current_y) <= 3:
            current.append(w)
            if current_y is None:
                current_y = y
        else:
            lines.append(" ".join(x["text"] for x in sorted(current, key=lambda a: a["x0"])))
            current = [w]
            current_y = y

    if current:
        lines.append(" ".join(x["text"] for x in sorted(current, key=lambda a: a["x0"])))

    return clean(" ".join(lines))


def is_label_token(text):
    return text in LABEL_TOKENS or re.fullmatch(r"[:,-]+", text or "")


def clean_direction(text):
    text = clean(text)
    text = re.sub(r"\b(APP\s+)?CLICK\s+POR\s+TEPIC\b", "", text, flags=re.I)
    text = re.sub(r"\bAPP\b", "", text, flags=re.I)
    text = re.sub(r"\bDireccion\b\s*:?", "", text, flags=re.I)
    text = re.sub(r"\bObservaciones\b\s*:?.*$", "", text, flags=re.I)
    text = re.sub(r"\bMotivo\b.*$", "", text, flags=re.I)
    return clean(text)


def clean_observaciones(text):
    text = clean(text)
    text = re.sub(r"^\s*Observaciones\s*:?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*,\s*$", "", text)
    return clean(text)


def armar_fecha(words_fecha):
    """El PDF envuelve el año en una segunda línea (12/06/20 + 26)."""
    words_fecha = sorted(words_fecha, key=lambda w: w["top"])
    texto = "".join(w["text"] for w in words_fecha)
    m = re.search(r"(\d{2})/(\d{2})/(\d{2,4})", texto)
    if not m:
        return ""
    dd, mm, yyyy = m.groups()
    if len(yyyy) == 2:
        yyyy = "20" + yyyy
    return f"{dd}/{mm}/{yyyy}"


PAGE_OFFSET_STEP = 100_000  # mucho mayor que la altura de cualquier página


def collect_continuous_words(pdf):
    """Encadena las palabras de todas las páginas en un único eje Y continuo,
    descartando el encabezado y pie de página que el reporte repite en cada hoja.

    Esto es necesario porque un registro puede partirse entre dos páginas
    (la Dirección/Observaciones de un folio terminan al inicio de la página
    siguiente); procesar página por página perdía esos datos.
    """
    palabras = []
    total_declarado = None

    for page_idx, page in enumerate(pdf.pages):
        if total_declarado is None:
            m = re.search(r"Total\s+de\s+Registros\s*(\d+)", page.extract_text() or "", re.I)
            if m:
                total_declarado = int(m.group(1))

        page_words = page.extract_words(x_tolerance=1, y_tolerance=3, keep_blank_chars=False)

        header_word = next((w for w in page_words if w["text"] == "Folio" and w["x0"] < 70), None)
        header_bottom = header_word["bottom"] if header_word else 0

        footer_word = next((w for w in page_words if re.fullmatch(r"P.GINA", w["text"], re.I)), None)
        footer_top = footer_word["top"] - 2 if footer_word else page.height

        offset = page_idx * PAGE_OFFSET_STEP
        for w in page_words:
            if header_bottom < w["top"] < footer_top:
                w = dict(w)
                w["top"] += offset
                w["bottom"] += offset
                palabras.append(w)

    return palabras, total_declarado


def extract_records(pdf_file):
    records = []
    last_colonia = "SIN COLONIA"

    with pdfplumber.open(pdf_file) as pdf:
        words, total_declarado = collect_continuous_words(pdf)

        colonias = []
        for w in words:
            if w["text"].upper() == "COLONIA":
                y = w["top"]
                line_words = [
                    x for x in words
                    if abs(x["top"] - y) < 4 and x["x0"] >= w["x0"] and x["x0"] < 500
                ]
                txt = words_to_text(line_words)
                m = re.search(r"COLONIA\s*: ?(.+)", txt, re.I)

                if m:
                    colonia = clean(m.group(1))
                    if colonia:
                        colonias.append((y, colonia))

        folios = []
        for w in words:
            if re.fullmatch(r"\d{6}", w["text"]) and COL_FOLIO_X[0] <= w["x0"] <= COL_FOLIO_X[1]:
                folios.append({
                    "folio": w["text"],
                    "top": w["top"],
                    "bottom": w["bottom"]
                })

        folios = sorted(folios, key=lambda f: f["top"])
        fin_documento = max((w["top"] for w in words), default=0) + 30
        colonia_row_tops = [cy for cy, _ in colonias]

        def es_fila_de_colonia(top):
            return any(abs(top - cy) < 4 for cy in colonia_row_tops)

        for idx, folio_info in enumerate(folios):
            y0 = folio_info["top"]
            y1 = folios[idx + 1]["top"] if idx + 1 < len(folios) else fin_documento

            for cy, colonia in colonias:
                if cy < y0:
                    last_colonia = colonia

            colonia = last_colonia

            # --- Renglón de cabecera de la fila (fecha / reportó / entre calles / teléfono) ---
            dir_label = next(
                (w for w in words if re.fullmatch(r"Direccion", w["text"], re.I)
                 and y0 <= w["top"] < y1 and w["x0"] < 70),
                None
            )
            y_dir = dir_label["top"] if dir_label else y1
            # La fila de "Direccion :"/"Observaciones :" suele renderizarse 2-3pt
            # más arriba que la etiqueta misma; este margen evita que ese texto se
            # mezcle con las columnas de la cabecera (Reportó / Entre calle).
            y_header_end = y_dir - 4

            fecha_words = [
                w for w in words
                if y0 - 5 <= w["top"] < y_header_end and COL_FECHA_X[0] <= w["x0"] <= COL_FECHA_X[1]
            ]
            fecha = armar_fecha(fecha_words)

            reporto_words = [
                w for w in words
                if y0 - 5 <= w["top"] < y_header_end and COL_REPORTO_X[0] <= w["x0"] <= COL_REPORTO_X[1]
            ]
            reporto_text = words_to_text(reporto_words)
            click = "SI" if re.search(r"CLICK", reporto_text, re.I) and re.search(r"TEPIC", reporto_text, re.I) else "NO"

            entrecalle_words = [
                w for w in words
                if y0 - 5 <= w["top"] < y_header_end and COL_ENTRECALLE_X[0] <= w["x0"] <= COL_ENTRECALLE_X[1]
            ]
            entrecalle = clean(words_to_text(entrecalle_words))

            phone_words = [
                w for w in words
                if y0 - 5 <= w["top"] < y_header_end and COL_TELEFONO_X[0] <= w["x0"] <= COL_TELEFONO_X[1]
            ]
            phone_text = words_to_text(phone_words)
            pm = re.search(r"\(\d{3}\)\s*\d{3}-\d{4}", phone_text)
            phone = pm.group(0) if pm else ""

            # --- Dirección + Observaciones ---
            motivo_tops = [
                w["top"] for w in words
                if y_dir < w["top"] < y1 and re.match(r"Motivo", w["text"], re.I) and w["x0"] < 70
            ]
            y_stop = min(motivo_tops) if motivo_tops else y1

            dir_words = [
                w for w in words
                if y_dir - 4 <= w["top"] < y_stop and COL_DIRECCION_X[0] <= w["x0"] <= COL_DIRECCION_X[1]
                and not is_label_token(w["text"]) and not es_fila_de_colonia(w["top"])
            ]
            direccion = clean_direction(words_to_text(dir_words)) or "SIN DIRECCIÓN"

            obs_words = [
                w for w in words
                if y_dir - 4 <= w["top"] < y_stop and COL_OBSERVACIONES_X[0] <= w["x0"] <= COL_OBSERVACIONES_X[1]
                and not is_label_token(w["text"]) and not es_fila_de_colonia(w["top"])
            ]
            observaciones = clean_observaciones(words_to_text(obs_words))

            domicilio = direccion
            if entrecalle and entrecalle.upper() not in domicilio.upper():
                domicilio = f"{domicilio} ENTRE {entrecalle}"

            records.append({
                "Folio": folio_info["folio"],
                "Fecha": fecha,
                "Domicilio": domicilio,
                "COLONIA_DETECTADA": colonia,
                "Teléfono": phone,
                "Observaciones": observaciones,
                "Click por Tepic": click,
            })

    return records, total_declarado


# ============================================================
#  Interfaz Streamlit
# ============================================================

st.set_page_config(page_title="Extractor SIAPA", page_icon="📄", layout="centered")

st.title("📄 Extractor de reportes SIAPA")
st.write("Arrastra o selecciona el PDF de reportes para extraer los datos a Excel/CSV.")

catalogo, claves_norm = cargar_catalogo()

uploaded_file = st.file_uploader("Sube el PDF", type=["pdf"])

if uploaded_file is not None:
    with st.spinner("Procesando PDF..."):
        try:
            records, total_declarado = extract_records(uploaded_file)
        except Exception as e:
            st.error(f"Ocurrió un error al procesar el PDF: {e}")
            records, total_declarado = [], None

    if records:
        df = pd.DataFrame(records)

        # Cotejo de colonia contra catálogo oficial
        coincidencias = df["COLONIA_DETECTADA"].apply(
            lambda c: emparejar_colonia(c, catalogo, claves_norm)
        )
        df["Colonia"] = coincidencias.apply(lambda r: r[0])
        df["¿Coincide con catálogo?"] = coincidencias.apply(lambda r: r[2])

        if total_declarado is not None and total_declarado != len(df):
            st.error(
                f"⚠️ El PDF declara **{total_declarado}** registros, pero se extrajeron "
                f"**{len(df)}**. Revisa el PDF, podrían faltar folios."
            )
        elif total_declarado is not None:
            st.success(f"✅ Registros extraídos: {len(df)} (coincide con el total declarado en el PDF)")
        else:
            st.success(f"✅ Registros extraídos: {len(df)}")

        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Sin teléfono", int((df["Teléfono"] == "").sum()))
        with col2:
            st.metric("Sin dirección", int((df["Domicilio"] == "SIN DIRECCIÓN").sum()))
        with col3:
            st.metric("Colonias a revisar", int((~df["¿Coincide con catálogo?"]).sum()))

        columnas_finales = [
            "Folio", "Fecha", "Domicilio", "Colonia", "Teléfono",
            "Observaciones", "Click por Tepic", "¿Coincide con catálogo?",
        ]

        st.caption(
            "La columna **Colonia** ya quedó corregida contra el catálogo oficial. "
            "Las marcadas como “Colonia a revisar” no encontraron una coincidencia confiable "
            "(la celda es editable abajo antes de descargar)."
        )

        df_editado = st.data_editor(
            df[columnas_finales],
            use_container_width=True,
            disabled=["Folio", "Fecha", "Domicilio", "Teléfono", "Observaciones", "Click por Tepic", "¿Coincide con catálogo?"],
            key="editor_resultados",
        )

        df_final = df_editado.drop(columns=["¿Coincide con catálogo?"])

        # Generar Excel en memoria
        excel_buffer = io.BytesIO()
        df_final.to_excel(excel_buffer, index=False, engine="openpyxl")
        excel_buffer.seek(0)

        # Generar CSV en memoria
        csv_data = df_final.to_csv(index=False).encode("utf-8-sig")

        base_name = uploaded_file.name.rsplit(".", 1)[0]

        col1, col2 = st.columns(2)
        with col1:
            st.download_button(
                label="⬇️ Descargar Excel",
                data=excel_buffer,
                file_name=f"{base_name}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        with col2:
            st.download_button(
                label="⬇️ Descargar CSV",
                data=csv_data,
                file_name=f"{base_name}.csv",
                mime="text/csv",
                use_container_width=True,
            )
    else:
        st.warning("No se encontraron registros en el PDF.")
else:
    st.info("Esperando un archivo PDF...")
