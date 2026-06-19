import re
import io
import pandas as pd
import pdfplumber
import streamlit as st

# ============================================================
#  Lógica de extracción (idéntica a extraer_siapa_pdf.py)
# ============================================================

LABEL_TOKENS = {
    "Direccion", ":", "Motivo", "Inspección", "Inspeccion",
    "Instalación", "Instalacion", "SONDEO", "RED",
    "Clasificación", "Clasificacion", "INSPECCION", "QUEJAS",
    "Observaciones", "Observaciones:"
}

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

def extract_records(pdf_file):
    records = []
    last_colonia = "SIN COLONIA"

    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages:
            words = page.extract_words(
                x_tolerance=1,
                y_tolerance=3,
                keep_blank_chars=False
            )

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
                if re.fullmatch(r"\d{6}", w["text"]) and 35 <= w["x0"] <= 115:
                    folios.append({
                        "folio": w["text"],
                        "top": w["top"],
                        "bottom": w["bottom"]
                    })

            folios = sorted(folios, key=lambda f: f["top"])

            for idx, folio_info in enumerate(folios):
                y0 = folio_info["top"]
                y1 = folios[idx + 1]["top"] if idx + 1 < len(folios) else page.height - 25

                for cy, colonia in colonias:
                    if cy < y0:
                        last_colonia = colonia

                colonia = last_colonia

                phone_words = [
                    w for w in words
                    if y0 - 5 <= w["top"] <= y0 + 8 and 560 <= w["x0"] <= 690
                ]
                phone_text = words_to_text(phone_words)
                pm = re.search(r"\(\d{3}\)\s*\d{3}-\d{4}", phone_text)
                phone = pm.group(0) if pm else ""

                header_words = [
                    w for w in words
                    if y0 - 18 <= w["top"] <= min(y0 + 25, y1) and 150 <= w["x0"] <= 460
                ]
                header = words_to_text(header_words)
                click = "SI" if re.search(r"APP\s+CLICK\s+POR\s+TEPIC", header, re.I) else ""

                motivo_tops = [
                    w["top"] for w in words
                    if y0 < w["top"] < y1 and re.match(r"Motivo", w["text"], re.I)
                ]
                y_stop = min(motivo_tops) if motivo_tops else y1

                dir_words = [
                    w for w in words
                    if y0 + 10 <= w["top"] < y_stop and 70 <= w["x0"] <= 395
                ]

                dir_words = [
                    w for w in dir_words
                    if not is_label_token(w["text"])
                ]

                direccion = clean_direction(words_to_text(dir_words)) or "SIN DIRECCIÓN"

                records.append({
                    "Folio": folio_info["folio"],
                    "Dirección": direccion,
                    "COLONIA": colonia,
                    "Número de teléfono": phone,
                    "CLICK": click,
                })

    return records


# ============================================================
#  Interfaz Streamlit
# ============================================================

st.set_page_config(page_title="Extractor SIAPA", page_icon="📄", layout="centered")

st.title("📄 Extractor de reportes SIAPA")
st.write("Arrastra o selecciona el PDF de reportes para extraer los datos a Excel/CSV.")

uploaded_file = st.file_uploader("Sube el PDF", type=["pdf"])

if uploaded_file is not None:
    with st.spinner("Procesando PDF..."):
        try:
            records = extract_records(uploaded_file)
        except Exception as e:
            st.error(f"Ocurrió un error al procesar el PDF: {e}")
            records = []

    if records:
        df = pd.DataFrame(records)

        st.success(f"✅ Registros extraídos: {len(df)}")

        col1, col2 = st.columns(2)
        with col1:
            st.metric("Sin teléfono", int((df["Número de teléfono"] == "").sum()))
        with col2:
            st.metric("Sin dirección", int((df["Dirección"] == "SIN DIRECCIÓN").sum()))

        st.dataframe(df, use_container_width=True)

        # Generar Excel en memoria
        excel_buffer = io.BytesIO()
        df.to_excel(excel_buffer, index=False, engine="openpyxl")
        excel_buffer.seek(0)

        # Generar CSV en memoria
        csv_data = df.to_csv(index=False).encode("utf-8-sig")

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
