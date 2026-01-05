#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# HM/Nov/25/v1.1 - CORRECCIÓN: Se actualiza la expresión regular en parse_mac_table_output para soportar \
#                el formato de tabla MAC de la serie Cisco Catalyst 4500 (con más campos intermedios).

import pandas as pd
import re
from datetime import datetime
import os
import sys
import openpyxl
from openpyxl import load_workbook
from openpyxl.utils.dataframe import dataframe_to_rows
from openpyxl.styles import PatternFill

# ==============================================================================
# 1. FUNCIONES DE UTILIDAD
# ==============================================================================

def standardize_port_name(port_name):
    """Estandariza los nombres de interfaz a un formato corto (e.g., Gi, Te, Fa)."""
    if not isinstance(port_name, str):
        return port_name

    port_name = port_name.strip()
    port_name = re.sub(r'GigabitEthernet', 'Gi', port_name, flags=re.IGNORECASE)
    port_name = re.sub(r'FastEthernet', 'Fa', port_name, flags=re.IGNORECASE)
    port_name = re.sub(r'TenGigabitEthernet', 'Te', port_name, flags=re.IGNORECASE)
    port_name = re.sub(r'TwentyFiveGigE', 'Tw', port_name, flags=re.IGNORECASE)

    # Se añade el 0 en los puertos sin slot (ej: Gi1/1 -> Gi1/0/1)
    match_slot = re.match(r"([A-Za-z]+)(\d+)/(\d+)$", port_name)
    if match_slot:
        prefix, unit, port = match_slot.groups()
        port_name = f"{prefix}{unit}/0/{port}"

    return port_name

def extract_port_components(port_name):
    """Devuelve (prefijo, unidad, slot, puerto) para ordenar los puertos."""
    if not isinstance(port_name, str):
        return None

    match_full = re.match(r"([A-Za-z]+)(\d+)/(\d+)/(\d+)", port_name.strip())
    if not match_full:
        return None

    prefix, unit, slot, port = match_full.groups()
    return prefix, int(unit), int(slot), int(port)

def port_sort_key(port_name):
    """Clave de ordenación natural para interfaces (Gi1/0/1 -> Gi1/0/2 -> ...)."""
    components = extract_port_components(port_name)
    if not components:
        return (float('inf'),)

    prefix, unit, slot, port = components
    prefix_order = {'Fa': 0, 'Gi': 1, 'Te': 2, 'Tw': 3}
    return (prefix_order.get(prefix, len(prefix_order)), unit, slot, port)

def infer_full_port_list(ports):
    """
    A partir de los puertos conocidos (esperados o vistos en la auditoría), genera el rango completo
    por cada stack/slot hasta el puerto máximo detectado. Ej: si existe Gi1/0/24, genera 1..24.
    """
    port_ranges = {}

    for port in ports:
        components = extract_port_components(port)
        if not components:
            continue

        prefix, unit, slot, port_number = components
        key = (prefix, unit, slot)
        port_ranges[key] = max(port_ranges.get(key, 0), port_number)

    inferred_ports = set()
    for (prefix, unit, slot), max_port in port_ranges.items():
        for port_number in range(1, max_port + 1):
            inferred_ports.add(f"{prefix}{unit}/{slot}/{port_number}")

    return inferred_ports


def parse_mac_table_output(file_path):
    """Lee y parsea un fichero de output 'show mac address-table'."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            mac_block = f.read().split('\n')
    except FileNotFoundError:
        print(f"Error: No se encontró el archivo de tabla MAC en {file_path}")
        return pd.DataFrame()

    macs_data = []

    for line in mac_block:
        line_stripped = line.strip()

        # 🛑 CORRECCIÓN: Nueva expresión regular flexible.
        # Captura: 1. VLAN (\d+), 2. MAC (xxxx.xxxx.xxxx), ignora el medio (.*?\s+),
        # y captura el último token ((\S+)) como Puerto.
        # Esto soporta tanto el formato corto (3700/3800) como el largo (4500).
        regex = r"^\s*(\d+)\s+([0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4})\s+.*?\s+(\S+)$"
        match = re.match(regex, line_stripped, re.IGNORECASE)

        if match:
            # Los grupos capturados son: 1=VLAN, 2=MAC, 3=Puerto
            vlan, mac, port_raw = match.groups()
            port = standardize_port_name(port_raw)
            if not port.lower().startswith(('cpu', 'vlan', 'po')):
                macs_data.append({'VLAN': vlan, 'MAC': mac, 'Current_Port': port})

    df = pd.DataFrame(macs_data)

    if not df.empty:
        df['VLAN_NORMALIZED'] = df['VLAN'].apply(vlan_key)
        df['MAC_VLAN_KEY'] = df['VLAN_NORMALIZED'].astype(str) + '-' + df['MAC'].astype(str)
        df = df.drop_duplicates(subset=['MAC_VLAN_KEY'], keep='first')

    return df

# ==============================================================================
# 2. FUNCIONES DE CARGA Y CÁLCULO
# ==============================================================================

def load_tracking_base(file_path):
    """
    Carga el tracking principal, garantiza la existencia de columnas de status y filtra.
    """
    try:
        # AÑADIDO: FORZAMOS EL USO DEL MOTOR OPENPYXL para evitar errores de xlrd/zip
        df = pd.read_excel(file_path, sheet_name=0, engine='openpyxl')

        # Corrección de nombres de columnas
        df = df.rename(columns={'Puerto Destino (Esperado)': 'Puerto Nuevo'}, errors='ignore')

        # Verificar que las columnas clave existen después de la lectura
        if 'VLAN' not in df.columns or 'MAC' not in df.columns or 'Puerto Nuevo' not in df.columns:
            raise ValueError("Faltan las columnas 'VLAN', 'MAC' o 'Puerto Nuevo' en la hoja principal.")

        df['Puerto Origen'] = df['Puerto Origen'].apply(standardize_port_name)
        df['Puerto Nuevo'] = df['Puerto Nuevo'].apply(standardize_port_name)
        df['VLAN_NORMALIZED'] = df['VLAN'].apply(vlan_key)
        df['MAC_VLAN_KEY'] = df['VLAN_NORMALIZED'].astype(str) + '-' + df['MAC'].astype(str)

        return df

    except Exception as e:
        print(f"❌ Error al leer el archivo de Tracking. Asegúrese de que el archivo '{file_path}' exista, no esté corrupto y contenga las columnas 'VLAN', 'MAC', y 'Puerto Nuevo'. Error: {e}")
        sys.exit(1)


def get_audit_mode():
    """Solicita al usuario si la auditoría es Pre- o Post-migración."""
    while True:
        mode_input = input("¿Las MACs recopiladas son [A]ntes (Pre) o [D]espués (Post) de la migración? (A/D): ").strip().upper()
        if mode_input == 'A':
            return 'PRE_MIGRATION'
        elif mode_input == 'D':
            return 'POST_MIGRATION'
        else:
            print("Entrada inválida. Rellena 'A' para Antes o 'D' para Después.")

def normalize_vlan(value):
    """Normaliza una VLAN eliminando '.0' y devolviendo enteros cuando es posible."""
    if pd.isna(value):
        return None

    # Intentar conversión numérica segura
    try:
        numeric = float(value)
        if numeric.is_integer():
            return int(numeric)
    except (TypeError, ValueError):
        pass

    # Fallback: limpiar cadenas
    return str(value).strip()


def vlan_key(value):
    """Devuelve la VLAN normalizada en forma de cadena para claves."""
    normalized = normalize_vlan(value)
    if normalized is None:
        return ''
    return str(normalized)


def calculate_status_v2(current, expected, vlan_current=None, vlan_expected=None):
    """
    Calcula el estado del puerto, recibiendo valores escalares.
    """
    # Si no hay puerto esperado (no migrado), no evaluamos nada
    if pd.isna(expected) or str(expected).strip() == '':
        return '🔵 Puerto sin migración planificada'

    # Si no hay puerto detectado, marcar como no encontrada antes de cualquier validación adicional
    if pd.isna(current):
        return '❌ NO ENCONTRADA (Pérdida/Desconexión)'

    vlan_current_norm = normalize_vlan(vlan_current)
    vlan_expected_norm = normalize_vlan(vlan_expected)

    # Primero validar VLAN
    if (
        vlan_current_norm is not None
        and vlan_expected_norm is not None
        and vlan_current_norm != vlan_expected_norm
    ):
        return f'⚠️ VLAN INCORRECTA: VLAN {vlan_current_norm} detectada (esperada {vlan_expected_norm})'

    # Si coincide
    if str(current) == str(expected):
        return '✅ OK'
    else:
        # Si no coincide (Desvío)
        return f'⚠️ DESVÍO: MAC aprendida en {current} (vs {expected}m)'


def build_unexpected_ports_map(df_current_macs, known_keys, expected_ports):
    """Construye un mapa puerto -> ["MAC@VLAN"] para MACs que no están en el tracking."""
    unexpected = {}
    df_unplanned = df_current_macs[~df_current_macs['MAC_VLAN_KEY'].isin(known_keys)]

    for _, row in df_unplanned.iterrows():
        port = row.get('Current_Port')
        if port not in expected_ports:
            unexpected.setdefault(port, []).append(f"{row.get('MAC')}@VLAN{row.get('VLAN')}")

    return unexpected


def build_missing_port_rows(df_base, all_ports, expected_ports, status_col_name, audit_mode, unexpected_ports):
    """Genera filas vacías para puertos no migrados y marca si hay MACs no planificadas."""
    missing_ports = all_ports - expected_ports
    if not missing_ports:
        return pd.DataFrame(), set()

    filler_rows = []
    for port in sorted(missing_ports, key=port_sort_key):
        row_data = {col: '' for col in df_base.columns}
        row_data['Puerto Origen'] = port
        row_data['Puerto Destino (Esperado)'] = port

        if audit_mode == 'PRE_MIGRATION':
            row_data[status_col_name] = '🔵 Puerto sin migración planificada'
        else:
            mac_notes = unexpected_ports.get(port)
            if mac_notes:
                row_data[status_col_name] = f"⚠️ MAC NO PLANIFICADA: {', '.join(mac_notes)}"
            else:
                row_data[status_col_name] = '🔵 Puerto sin migración planificada'

        filler_rows.append(row_data)

    filler_ports = {row['Puerto Destino (Esperado)'] for row in filler_rows}
    return pd.DataFrame(filler_rows), filler_ports


def apply_filler_style(tracking_file, filler_ports):
    """Pinta en azul claro las filas creadas para puertos sin migrar."""
    if not filler_ports:
        return

    try:
        wb = load_workbook(tracking_file)
        if 'Sheet1' not in wb.sheetnames:
            return

        ws = wb['Sheet1']
        port_col_idx = None
        for idx, cell in enumerate(ws[1], start=1):
            if cell.value == 'Puerto Destino (Esperado)':
                port_col_idx = idx
                break

        if port_col_idx is None:
            wb.close()
            return

        filler_fill = PatternFill(start_color="D9EAF7", end_color="D9EAF7", fill_type="solid")

        for row in ws.iter_rows(min_row=2):
            port_value = row[port_col_idx - 1].value
            if port_value in filler_ports:
                for cell in row:
                    cell.fill = filler_fill

        wb.save(tracking_file)
    except Exception as e:
        print(f"⚠️ No se pudo aplicar el color a las filas vacías: {e}")


def append_summary_table(tracking_file, df_summary):
    """Agrega una tabla resumen 5 filas debajo de la tabla principal."""
    if df_summary.empty:
        return

    try:
        wb = load_workbook(tracking_file)
        if 'Sheet1' not in wb.sheetnames:
            return

        ws = wb['Sheet1']
        start_row = ws.max_row + 6
        headers = ['Puerto Origen', 'Puerto Destino (Esperado)', 'MAC']

        for col_idx, header in enumerate(headers, start=1):
            ws.cell(row=start_row, column=col_idx, value=header)

        for row_offset, row in enumerate(df_summary.itertuples(index=False), start=1):
            ws.cell(row=start_row + row_offset, column=1, value=row[0])
            ws.cell(row=start_row + row_offset, column=2, value=row[1])
            ws.cell(row=start_row + row_offset, column=3, value=row[2])

        wb.save(tracking_file)
    except Exception as e:
        print(f"⚠️ No se pudo agregar la tabla resumen: {e}")

# ==============================================================================
# 3. LÓGICA DE AUDITORÍA Y REPORTE (Función Principal)
# ==============================================================================

def run_auditoria_mac():
    """Función principal para ejecutar la auditoría."""

    print("**************************************************")
    print("* INICIO DE AUDITORÍA MAC POST-MIGRACIÓN (V1.7)  *")
    print("**************************************************")

    # --- A. ENTRADA DE ARCHIVOS Y MODO DE AUDITORÍA ---

    tracking_file = input("1. Rellena el nombre del archivo de TrackingMAC (*TrackingMac_hostname.xlsx*): ").strip()

    # Generar el timestamp para usar en los títulos de las columnas
    timestamp_suffix = datetime.now().strftime("%Y%m%d_%H%M%S")

    df_base = load_tracking_base(tracking_file)

    audit_mode = get_audit_mode()

    mac_table_files = input("Rellena el/los nombre(s) del archivo(s) show mac address-table separados por coma: ").strip()
    file_list = [f.strip() for f in mac_table_files.split(",") if f.strip()]
    df_all = pd.DataFrame()

    for file in file_list:
        df_temp = parse_mac_table_output(file)
        if not df_temp.empty:
            df_all = pd.concat([df_all, df_temp], ignore_index=True)
    df_current_macs = df_all.drop_duplicates()  # Opcional para evitar duplicados

    if df_current_macs.empty:
        print("❌ No se pudieron extraer MACs válidas del archivo de auditoría. Saliendo.")
        return

    # --- B. DEFINICIÓN DE COLUMNAS DE TRABAJO Y CRUCE ---

    # 🛑 MODIFICACIÓN CRÍTICA: Construir el nombre de columna con el timestamp
    mode_name = 'Pre' if audit_mode == 'PRE_MIGRATION' else 'Post'
    status_col_name = f"Status_{mode_name}_Migracion_{timestamp_suffix}"

    port_esperado_col = 'Puerto Origen' if audit_mode == 'PRE_MIGRATION' else 'Puerto Nuevo'

    current_port_map = df_current_macs.set_index('MAC_VLAN_KEY')['Current_Port'].to_dict()
    current_vlan_map = df_current_macs.set_index('MAC')['VLAN'].to_dict()
    current_mac_map = df_current_macs.set_index('MAC_VLAN_KEY')['MAC'].to_dict()

    # 1. Asignar el 'Current_Port' al DF base usando .map()
    df_base['Current_Port_Audit'] = df_base['MAC_VLAN_KEY'].map(current_port_map)
    df_base['MAC_Audit'] = df_base['MAC_VLAN_KEY'].map(current_mac_map)
    df_base['VLAN_Audit'] = df_base['MAC'].map(current_vlan_map)

    # 2. Calcular el NUEVO STATUS basado en la comparación
    new_status_series = df_base.apply(
        lambda row: calculate_status_v2(
            row['Current_Port_Audit'],
            row[port_esperado_col],
            row.get('VLAN_Audit'),
            row.get('VLAN'),
        ),
        axis=1
    )

    # 3. Aplicar la actualización al DataFrame base (df_base)
    # Pandas creará automáticamente la nueva columna con el timestamp.
    df_base.loc[:, status_col_name] = new_status_series

    known_keys = set(df_base['MAC_VLAN_KEY'])

    # 4. Limpieza de columna temporal
    df_base = df_base.drop(columns=['Current_Port_Audit'], errors='ignore')
    df_base = df_base.rename(columns={'Puerto Nuevo': 'Puerto Destino (Esperado)'}, errors='ignore')

    df_base['Puerto Destino (Esperado)'] = df_base['Puerto Destino (Esperado)'].apply(standardize_port_name)

    # --- C. COMPLETAR PUERTOS NO MIGRADOS Y DETECTAR DESVÍOS EN PUERTOS VACÍOS ---
    expected_ports = set(df_base['Puerto Destino (Esperado)'].dropna())
    current_ports = set(df_current_macs['Current_Port'].dropna())
    inferred_ports = infer_full_port_list(expected_ports | current_ports)

    unexpected_ports = build_unexpected_ports_map(df_current_macs, known_keys, expected_ports)

    filler_df, filler_ports = build_missing_port_rows(
        df_base,
        inferred_ports,
        expected_ports,
        status_col_name,
        audit_mode,
        unexpected_ports,
    )

    if not filler_df.empty:
        df_base = pd.concat([df_base, filler_df], ignore_index=True)

    # --- Ordenar por puerto destino para la salida Post-Migración ---
    if audit_mode == 'POST_MIGRATION':
        df_base = df_base.sort_values(
            by=['Puerto Destino (Esperado)'],
            key=lambda col: col.map(port_sort_key),
            na_position='last'
        ).reset_index(drop=True)

    summary_columns = ['Puerto Origen', 'Puerto Destino (Esperado)', 'MAC']
    df_summary = df_base[[col for col in summary_columns if col in df_base.columns]].copy()
    if not df_summary.empty and 'Puerto Destino (Esperado)' in df_summary.columns:
        df_summary = df_summary.sort_values(
            by=['Puerto Destino (Esperado)'],
            key=lambda col: col.map(port_sort_key),
            na_position='last'
        ).reset_index(drop=True)

    # Reordenar columnas: quitar MAC_VLAN_KEY de la salida y colocar MAC/VLAN auditadas tras el puerto destino
    status_columns = [c for c in df_base.columns if c.startswith('Status_')]
    base_columns = [c for c in df_base.columns if c not in status_columns]

    # Remover columnas auxiliares antes de exportar
    base_columns = [c for c in base_columns if c not in ('MAC_VLAN_KEY', 'VLAN_NORMALIZED')]

    ordered_columns = []
    for col in base_columns:
        ordered_columns.append(col)
        if col == 'Puerto Destino (Esperado)':
            if 'MAC_Audit' in base_columns:
                ordered_columns.append('MAC_Audit')
            if 'VLAN_Audit' in base_columns:
                ordered_columns.append('VLAN_Audit')

    # Agregar columnas de estado al final, manteniendo su orden original
    ordered_columns.extend(status_columns)

    # Eliminar duplicados en caso de haber insertado dos veces
    seen = set()
    ordered_columns = [c for c in ordered_columns if not (c in seen or seen.add(c))]
    df_base = df_base[ordered_columns]

    # --- D. EXPORTACIÓN A EXCEL (Sobreescritura Segura) ---

    print(f"\nGuardando la hoja principal 'Sheet1' en: {tracking_file}")

    try:
        # Intentamos cargar el libro para ver cuántas hojas hay
        book = load_workbook(tracking_file)
        sheet_names = book.sheetnames

        if 'Sheet1' in sheet_names and len(sheet_names) > 1:
            # Caso 1: Hay más de una hoja. Eliminamos Sheet1 y reescribimos.
            print("-> Más de una hoja detectada. Eliminando y reescribiendo 'Sheet1'...")

            # 1. Eliminar la hoja
            book.remove(book['Sheet1'])
            book.save(tracking_file)

            # 2. Reescribir la nueva 'Sheet1'
            with pd.ExcelWriter(tracking_file, engine='openpyxl', mode='a', if_sheet_exists='replace') as writer:
                df_base.to_excel(writer, sheet_name='Sheet1', index=False)

        else:
            # Caso 2: Solo existe 'Sheet1' o el archivo es nuevo. Sobreescribimos todo.
            if 'Sheet1' in sheet_names and len(sheet_names) == 1:
                print("-> Solo 'Sheet1' detectada. Sobreescribiendo el archivo completo (modo 'w')...")
            else:
                print("-> Archivo nuevo o vacío. Creando 'Sheet1'...")

            # Usamos xlsxwriter para la escritura completa por ser más robusto para este modo
            with pd.ExcelWriter(tracking_file, engine='xlsxwriter', mode='w') as writer:
                df_base.to_excel(writer, sheet_name='Sheet1', index=False)

        # Aplicar estilo a filas vacías de puertos no migrados
        apply_filler_style(tracking_file, filler_ports)
        append_summary_table(tracking_file, df_summary)

        print(f"✅ Auditoría consolidada completada. Fichero actualizado: {tracking_file}")

    except Exception as e:
        print(f"❌ Error crítico al exportar el Tracking a Excel: {e}")
        print("Asegúrese de que el archivo no esté abierto y que tenga instaladas las librerías: pandas, openpyxl, xlsxwriter.")

if __name__ == "__main__":
    run_auditoria_mac()
