#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Inventario sencillo de puertos a partir de logs Cisco (show run, show interface status,
show mac address-table, show ip int brief, show interfaces, show version) y
flujo inverso para generar un plan de migración desde un Excel ya cumplimentado.

- Entrada (modo 1): 1 o más ficheros .log con los comandos anteriores.
- Salida (modo 1):
    * Migración_<Estación>_v1.0.xlsx (nombre inferido de los logs)
        Hoja "Actual" con columnas: Hostname, Model, Port, Description, Estado,
        Cableado, VLANs, Last input/output, Migrar y MACs (MACs queda al final).
        Entre dispositivos se insertan filas azules con el nombre del log
        siguiente, su hostname detectado, modelo e IP, y la primera fila de la
        hoja queda para los encabezados y la segunda para el banner azul del
        primer log.

- Entrada (modo 2): un Excel ya rellenado (con las columnas anteriores y las
  decisiones de "Cableado"/"Migrar")
- Salida (modo 2):
    * Un TXT con la configuración base y un resumen de asignaciones de puertos.
    * Un Excel de migración que solo contiene los puertos marcados como
      "Migrar=Yes", repartidos por dispositivos y con el puerto nuevo calculado
      según el modelo elegido para cada hoja.

Uso:
    python inventario_puertos.py
    (el script primero pregunta si quieres ejecutar el modo 1 (inventario desde
    logs) o el modo 2 (plan de migración a partir de un Excel). En modo 1, se
    solicita si los logs se introducen manualmente o por carpeta. En modo 2, se
    pide la ruta del Excel y la carpeta donde guardar los nuevos ficheros. En
    ambos casos la carpeta de salida puede estar fuera del proyecto y se crea si
    no existe. Si el Excel final no puede escribirse, el script avisará y
    permitirá reintentar o elegir otra ruta.)
"""

import os
import re
import sys
from collections import defaultdict

import pandas as pd
from openpyxl.styles import PatternFill


EXCEL_COLUMNS = [
    "Hostname",
    "Model",
    "Port",
    "Description",
    "Estado",
    "Cableado",
    "VLANs",
    "Last input/output",
    "Migrar",
    "MACs",
]

EXCEL_SHEET_NAME = "Actual"

INTERFACE_PREFIX_ORDER = {
    "Fa": 0,
    "Gi": 1,
    "Te": 2,
    "Hu": 3,
    "Po": 4,
}

MIGRATION_TYPE_CATALOG = {
    "1": {"name": "5420M-16MW-32P-4YE", "ports": 52},
    "2": {"name": "4120-24MW-4Y 2PSU", "ports": 28},
    "3": {"name": "4120-24MW-4Y 1PSU", "ports": 28},
    "4": {"name": "X435-8P-4S", "ports": 12},
    "5": {"name": "16804", "ports": 12},
    "6": {"name": "5420F-24S-4XE", "ports": 28},
}

BASE_CONFIG_TEMPLATE = """# Configuración base Extreme para la migración\n# (puedes ajustarla tras generarla automáticamente)\n\n#\n# Module devmgr configuration.\n#\nconfigure snmp sysName "{hostname}"\nconfigure snmp sysLocation "{station}_CC1"\nconfigure snmp sysContact "AXIANS"\nconfigure timezone name CET 60 autodst name CEST 60 begins every last sunday march at 2 0 ends every last sunday october at 3 0\n\n# Slots de ejemplo (ajusta según el modelo elegido)\nconfigure slot 1 module 5420M-16MW-32P-4YE\nconfigure sys-recovery-level slot 1 reset\nconfigure slot 2 module 5420M-16MW-32P-4YE\nconfigure sys-recovery-level slot 2 reset\nconfigure slot 3 module 5420F-24S-4XE\nconfigure sys-recovery-level slot 3 reset\n\n#\n# Module vlan configuration.\n#\nconfigure vlan default delete ports all\nconfigure vr VR-Default delete ports all\nconfigure vr VR-Default add ports all\ncreate vlan "GESTION"\nconfigure vlan GESTION tag 1000\ncreate vlan "OFIMATICA"\nconfigure vlan OFIMATICA tag 529\ncreate vlan "PCL"\nconfigure vlan PCL tag 561\ncreate vlan "SAIT-UPS"\nconfigure vlan SAIT-UPS tag 513\ncreate vlan "AUTOMATAS"\nconfigure vlan AUTOMATAS tag 514\ncreate vlan "CONTROL_ACCESO"\nconfigure vlan CONTROL_ACCESO tag 522\ncreate vlan "VOIP"\nconfigure vlan VOIP tag 552\n\n# Añade aquí el resto de VLANs que necesites\n\n#\n# Muestras de plantillas por puerto (añade/ajusta según el plan generado)\n#\n# configure port 1:1 description "ENLACE"\n# configure vlan OFIMATICA add ports 1:1 untagged\n\n#\n# Otras secciones de la plantilla original\n# (log, ntp, snmp, stp, etc.)\n#\n"""


# ---------------------------------------------------------------------------
# PARSING: SHOW INTERFACE STATUS
# ---------------------------------------------------------------------------

def parse_interface_status_block(text: str) -> dict:
    """
    Devuelve un dict: { 'Gi1/0/1': {'name':..., 'status':..., 'vlan':...}, ... }
    a partir del bloque de 'show interface status'.
    """
    if "show interface status" not in text:
        return {}

    after = text.split("show interface status", 1)[1]
    lines = after.splitlines()

    # saltar líneas vacías hasta el header
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines):
        return {}

    header = lines[idx]
    # localizar posiciones de columnas en el header
    starts = {}
    for col in ["Port", "Name", "Status", "Vlan", "Duplex", "Speed", "Type"]:
        try:
            starts[col] = header.index(col)
        except ValueError:
            # por si alguna columna no existe (raro, pero no crítico)
            pass

    def slice_col(line: str, a: int, b: int | None) -> str:
        if b is None:
            seg = line[a:]
        else:
            seg = line[a:b]
        return seg.strip()

    result = {}
    idx += 1
    while idx < len(lines):
        line = lines[idx]
        s = line.strip()
        # fin del bloque cuando ya no hay un nombre de interfaz "normal"
        if not s:
            break
        if not re.match(r"^(Fa|Gi|Te|Hu)\S*", s):
            break

        port = slice_col(line, starts["Port"], starts["Name"])
        name = slice_col(line, starts["Name"], starts["Status"])
        status = slice_col(line, starts["Status"], starts["Vlan"])
        vlan = slice_col(line, starts["Vlan"], starts["Duplex"])

        result[port] = {
            "name": name,
            "status": status,
            "vlan": vlan,
        }
        idx += 1

    return result


def _port_sort_key(port: str) -> tuple:
    """Ordena puertos como Gi1/0/1, Gi1/0/2, Gi1/0/10 en orden natural."""

    match = re.match(r"([A-Za-z]+)([0-9/]+)", port)
    if match:
        prefix, numeric_part = match.groups()
    else:
        prefix, numeric_part = port, ""

    prefix_key = INTERFACE_PREFIX_ORDER.get(prefix[:2], len(INTERFACE_PREFIX_ORDER))

    numeric_values: list[int | str] = []
    if numeric_part:
        for chunk in numeric_part.split("/"):
            if chunk.isdigit():
                numeric_values.append(int(chunk))
            else:
                numeric_values.append(chunk)

    return (
        prefix_key,
        prefix,
        tuple(numeric_values),
        port,
    )


# ---------------------------------------------------------------------------
# PARSING: SHOW MAC ADDRESS-TABLE
# ---------------------------------------------------------------------------

def parse_mac_table_block(text: str) -> dict:
    """
    Devuelve un dict: { 'Gi1/0/3': ['00aa.bbcc.ddee', ...], ... }
    con las MACs dinámicas por puerto a partir del bloque de
    'show mac address-table'.
    """
    if "show mac address-table" not in text:
        return {}

    after = text.split("show mac address-table", 1)[1]
    lines = after.splitlines()

    mac_map: dict[str, list[str]] = defaultdict(list)

    for line in lines:
        # Ejemplo: " 513    0030.d62c.08a0    DYNAMIC     Gi1/0/3"
        m = re.match(r"\s*(\d+)\s+([0-9a-f\.]+)\s+\S+\s+(\S+)\s*$", line, re.IGNORECASE)
        if m:
            vlan, mac, port = m.groups()
            mac_map[port].append(mac.lower())

    return mac_map


# ---------------------------------------------------------------------------
# PARSING: BLOQUES DE RUNNING-CONFIG (INTERFACES)
# ---------------------------------------------------------------------------

def parse_interface_configs(text: str) -> dict:
    """
    Devuelve un dict: { 'GigabitEthernet1/0/1': 'interface ...\n ...', ... }
    con el bloque de running-config de cada interfaz.
    """
    iface_cfgs: dict[str, str] = {}
    current = None
    buf: list[str] = []

    for line in text.splitlines():
        raw = line.rstrip("\n")
        stripped = raw.strip()

        if stripped.startswith("interface "):
            # guardar interfaz previa
            if current is not None:
                iface_cfgs[current] = "\n".join(buf)

            parts = stripped.split()
            current = parts[1] if len(parts) > 1 else None
            buf = [raw]

        elif current is not None:
            # terminamos al encontrar 'end' (por seguridad), aunque realmente
            # el siguiente "interface" ya corta
            if stripped.startswith("end"):
                buf.append(raw)
                iface_cfgs[current] = "\n".join(buf)
                current = None
            else:
                buf.append(raw)

    if current is not None:
        iface_cfgs[current] = "\n".join(buf)

    return iface_cfgs


def get_trunk_allowed_vlans(iface_cfgs: dict) -> dict:
    """
    Devuelve { 'GigabitEthernet1/0/10': '513,529,546,561,1000', ... }
    buscando 'switchport trunk allowed vlan ...' en los bloques de interfaz.
    """
    trunk_allowed: dict[str, str] = {}
    for ifname, cfg in iface_cfgs.items():
        m = re.search(r"switchport trunk allowed vlan\s+(.+)", cfg)
        if m:
            allowed = m.group(1).strip()
            # eliminar espacios, ' add ', etc. (básico)
            allowed = re.sub(r"\s", "", allowed)
            trunk_allowed[ifname] = allowed
    return trunk_allowed


def extract_interface_descriptions(iface_cfgs: dict[str, str]) -> dict[str, str]:
    """Obtiene las descripciones completas de cada interfaz del running-config."""

    descriptions: dict[str, str] = {}
    for ifname, cfg in iface_cfgs.items():
        match = re.search(r"^\s*description\s+(.+)$", cfg, re.IGNORECASE | re.MULTILINE)
        if match:
            descriptions[ifname] = match.group(1).strip()

    return descriptions


def normalize_port_to_ifname(port: str) -> str:
    """
    Convierte nombres cortos de 'show interface status' a nombres de running-config.

    Ejemplos:
        Gi1/0/10 -> GigabitEthernet1/0/10
        Fa0/24   -> FastEthernet0/24
    """
    if port.startswith("Gi"):
        return "GigabitEthernet" + port[2:]
    if port.startswith("Fa"):
        return "FastEthernet" + port[2:]
    if port.startswith("Te"):
        return "TenGigabitEthernet" + port[2:]
    if port.startswith("Hu"):
        return "HundredGigE" + port[2:]
    # por si acaso
    return port


def extract_management_ip(iface_cfgs: dict[str, str]) -> str:
    """Busca la IP de gestión en interfaces Vlan (preferentemente con descripción 'GESTION')."""

    fallback_ip = ""
    for ifname, cfg in iface_cfgs.items():
        if not ifname.lower().startswith("vlan"):
            continue

        ip_match = re.search(r"ip address\s+(\d{1,3}(?:\.\d{1,3}){3})", cfg)
        if not ip_match:
            continue

        ip_addr = ip_match.group(1)

        desc_match = re.search(r"description\s+(.+)", cfg, re.IGNORECASE)
        if desc_match and "gestion" in desc_match.group(1).lower():
            return ip_addr

        if not fallback_ip:
            fallback_ip = ip_addr

    return fallback_ip


def extract_routed_interface_ips(iface_cfgs: dict[str, str]) -> dict[str, str]:
    """Localiza IPs en interfaces con 'no switchport' y 'ip address ...'."""

    routed_ips: dict[str, str] = {}
    ip_pattern = re.compile(r"ip address\s+(\d{1,3}(?:\.\d{1,3}){3})", re.IGNORECASE)

    for ifname, cfg in iface_cfgs.items():
        lowered = cfg.lower()
        if "no switchport" not in lowered:
            continue

        ip_match = ip_pattern.search(cfg)
        if ip_match:
            routed_ips[ifname] = ip_match.group(1)

    return routed_ips


def _candidate_station_suffixes(name: str) -> dict[str, str]:
    """Devuelve posibles sufijos de estación {"PLANETARIO": "PLANETARIO", ...}."""

    base = os.path.splitext(os.path.basename(name))[0]
    segments = [seg for seg in re.split(r"[_\\-]+", base) if seg]

    mapping: dict[str, str] = {}
    for segment in segments:
        letters = "".join(ch for ch in segment if ch.isalpha())
        if len(letters) < 3:
            continue

        for idx in range(len(letters)):
            suffix = letters[idx:]
            if len(suffix) < 3:
                continue

            key = suffix.upper()
            stored = mapping.get(key)
            if stored is None or len(suffix) > len(stored):
                mapping[key] = suffix

    return mapping


def infer_station_name(log_paths: list[str]) -> str:
    """Intenta deducir el nombre de la estación a partir de los nombres de los logs."""

    candidate_sets: list[set[str]] = []
    originals: dict[str, str] = {}

    for path in log_paths:
        mapping = _candidate_station_suffixes(path)
        if not mapping:
            continue

        candidate_sets.append(set(mapping.keys()))

        for key, original in mapping.items():
            existing = originals.get(key)
            if existing is None or len(original) > len(existing):
                originals[key] = original

    if not candidate_sets:
        return "Estacion"

    intersection = set.intersection(*candidate_sets) if candidate_sets else set()

    def choose_best(options: set[str]) -> str:
        if not options:
            return ""
        return sorted(options, key=lambda s: (-len(s), s))[0]

    best_key = choose_best(intersection)

    if not best_key:
        for options in candidate_sets:
            best_key = choose_best(options)
            if best_key:
                break

    if not best_key:
        return "Estacion"

    original = originals.get(best_key, best_key)
    pretty = original.strip()
    return pretty.title() if pretty else "Estacion"


def sanitize_station_for_filename(station_name: str) -> str:
    """Limpia el nombre de estación para componer el Excel final."""

    cleaned = station_name.strip().replace(os.sep, "_")
    if os.altsep:
        cleaned = cleaned.replace(os.altsep, "_")

    invalid_chars = set('<>:"/\\|?*')
    cleaned = "".join("_" if ch in invalid_chars else ch for ch in cleaned)
    cleaned = cleaned.strip().strip(".")

    return cleaned or "Estacion"


def parse_hostname(text: str, default: str) -> str:
    """Obtiene el hostname del log (o usa un valor por defecto)."""

    match = re.search(r"^\s*hostname\s+(\S+)", text, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return default


def parse_device_model(text: str) -> str:
    """Busca el modelo del switch dentro de la salida de 'show version'."""

    if "show version" not in text:
        return ""

    after = text.split("show version", 1)[1]
    lines = after.splitlines()

    model_candidates: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        match_prefix = re.search(r"^cisco\s+([A-Za-z0-9-]+)\s+\(", stripped, re.IGNORECASE)
        if match_prefix:
            model_candidates.append(match_prefix.group(1).upper())
            continue

        match_label = re.search(r"Model\s+number\s*[:=]\s*(\S+)", stripped, re.IGNORECASE)
        if match_label:
            model_candidates.append(match_label.group(1).upper())
            continue

        match_pid = re.search(r"PID:\s*([A-Za-z0-9-]+)", stripped, re.IGNORECASE)
        if match_pid:
            model_candidates.append(match_pid.group(1).upper())

    return model_candidates[0] if model_candidates else ""


def parse_last_io_info(text: str) -> dict[str, tuple[str, str]]:
    """Devuelve { 'GigabitEthernet1/0/1': ('00:00:01', '00:00:02'), ... } con Last input/output."""

    last_io: dict[str, tuple[str, str]] = {}
    current_if: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.lstrip()
        stripped = raw_line.strip()

        if not stripped:
            continue

        start_match = re.match(r"^([A-Za-z][\w./-]+)\s+is\s+.+", line)
        if start_match:
            current_if = start_match.group(1)
            continue

        if current_if is None:
            continue

        io_match = re.search(
            r"Last input\s+([^,]+),\s+output\s+([^,]+)", stripped, re.IGNORECASE
        )
        if io_match:
            last_io[current_if] = (io_match.group(1).strip(), io_match.group(2).strip())

    return last_io


# ---------------------------------------------------------------------------
# PARSING POR LOG: INVENTARIO
# ---------------------------------------------------------------------------

def parse_log_inventory(path: str):
    """
    Procesa un log y devuelve:
        rows: lista de dicts con columnas para el Excel
        device_info: metadatos del dispositivo (log, hostname, ip, modelo)
    """
    with open(path, "r", errors="ignore") as f:
        text = f.read()

    int_status = parse_interface_status_block(text)
    mac_map = parse_mac_table_block(text)
    iface_cfgs = parse_interface_configs(text)
    trunk_allowed = get_trunk_allowed_vlans(iface_cfgs)
    iface_descriptions = extract_interface_descriptions(iface_cfgs)
    management_ip = extract_management_ip(iface_cfgs)
    routed_ips = extract_routed_interface_ips(iface_cfgs)
    model = parse_device_model(text)
    last_io_info = parse_last_io_info(text)

    rows: list[dict] = []
    log_name = os.path.basename(path)
    hostname = parse_hostname(text, os.path.splitext(log_name)[0])

    # ordenamos por nombre de interfaz (Gi1/0/1, Fa0/1, etc.) con orden natural
    for port in sorted(int_status.keys(), key=_port_sort_key):
        info = int_status[port]
        status = info["status"]
        vlan = info["vlan"]
        ifname = normalize_port_to_ifname(port)
        description = iface_descriptions.get(ifname, info.get("name", ""))
        routed_ip = routed_ips.get(ifname, "")

        # VLANs: si es trunk, sacamos las allowed; si no, usamos la VLAN de 'show int status'
        if vlan.lower() == "trunk":
            allowed = trunk_allowed.get(ifname)
            if allowed:
                vlans_str = allowed
            else:
                vlans_str = "all"   # trunk sin lista explícita
        elif vlan.lower() == "routed":
            vlans_str = "routed"
        else:
            vlans_str = vlan

        # MACs asociadas a ese puerto
        macs = sorted(set(mac_map.get(port, [])))
        macs_str = ", ".join(macs)

        estado = status
        last_io_display = ""
        status_lower = status.lower()
        if status_lower in {"notconnect", "disabled"}:
            last_in, last_out = last_io_info.get(
                ifname, ("desconocido", "desconocido")
            )
            last_io_display = f"Last input={last_in}; Last output={last_out}"

        is_routed = routed_ip != ""
        migrar_value = "Yes" if status_lower == "connected" and not is_routed else "No"
        cableado_value = "Yes" if status_lower == "connected" else ""

        rows.append(
            {
                "Hostname": hostname,
                "Model": model,
                "Port": port,
                "Description": description,
                "Estado": estado,
                "Cableado": cableado_value,
                "VLANs": vlans_str,
                "Last input/output": last_io_display,
                "Migrar": migrar_value,
                "MACs": macs_str,
            }
        )

    device_info = {
        "log_name": log_name,
        "hostname": hostname,
        "ip": management_ip,
        "model": model,
    }

    return rows, device_info


# ---------------------------------------------------------------------------
# EXPORTS
# ---------------------------------------------------------------------------

def export_to_excel(
    all_rows: list[dict],
    out_path: str,
    separator_indices: list[int],
    first_banner: dict | None,
):
    """
    Genera un Excel con columnas:
        Hostname, Model, Port, Description, Estado, Cableado, VLANs,
        Last input/output, Migrar, MACs
    La cabecera permanece en la primera fila y, si hay banner inicial, se inserta
    la fila azul justo debajo. Los índices indicados en separator_indices ya
    contienen la fila separadora correspondiente y aquí solo se colorean.
    """
    if not all_rows and not first_banner:
        print("No hay datos para exportar al Excel.")
        return

    df = pd.DataFrame(all_rows, columns=EXCEL_COLUMNS)

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=EXCEL_SHEET_NAME, startrow=0)

        ws = writer.sheets[EXCEL_SHEET_NAME]
        fill = PatternFill(start_color="99CCFF", end_color="99CCFF", fill_type="solid")

        banner_offset = 0
        if first_banner:
            ws.insert_rows(2)
            for col_idx, column in enumerate(EXCEL_COLUMNS, start=1):
                ws.cell(row=2, column=col_idx, value=first_banner.get(column, ""))
                ws.cell(row=2, column=col_idx).fill = fill
            banner_offset = 1

        for idx in separator_indices:
            excel_row = idx + 2 + banner_offset  # cabecera incluida
            for col_idx in range(1, len(EXCEL_COLUMNS) + 1):
                ws.cell(row=excel_row, column=col_idx).fill = fill

    print(f"[OK] Excel generado: {out_path}")


# ---------------------------------------------------------------------------
# MIGRACIÓN DESDE EXCEL EXISTENTE
# ---------------------------------------------------------------------------

def prompt_primary_mode() -> str:
    """Pregunta si se ejecutará el flujo de inventario o el de migración."""

    prompt = (
        "¿Qué modo quieres ejecutar?\n"
        "  [1] Inventario desde logs (genera el Excel 'Actual').\n"
        "  [2] Plan de migración desde un Excel ya cumplimentado.\n"
        "Selecciona 1 o 2: "
    )

    while True:
        choice = input(prompt).strip()
        if choice == "1":
            return "inventario"
        if choice == "2":
            return "migracion"
        print("Opción no válida. Escribe 1 para inventario o 2 para migración.")


def prompt_for_inventory_excel() -> str:
    """Solicita la ruta del Excel de inventario ya cumplimentado."""

    while True:
        user_input = input("Introduce la ruta del Excel con el inventario: ").strip()
        normalized = _normalize_user_path(user_input)
        if not normalized:
            print("Ruta no válida. Inténtalo de nuevo.")
            continue

        if not os.path.isfile(normalized):
            print(f"No existe el fichero: {normalized}")
            continue

        return normalized


def _load_inventory_dataframe(path: str) -> pd.DataFrame:
    """Lee el Excel de inventario y devuelve solo las filas con un puerto válido."""

    df = pd.read_excel(path, sheet_name=0)
    df = df.fillna("")

    if not set(EXCEL_COLUMNS).issubset(df.columns):
        missing = set(EXCEL_COLUMNS) - set(df.columns)
        raise ValueError(f"Faltan columnas en el Excel: {', '.join(sorted(missing))}")

    df = df[df["Port"].astype(str).str.strip() != ""]
    return df


def _filter_migratable_rows(df: pd.DataFrame) -> list[dict]:
    """Extrae filas marcadas con Migrar=Yes."""

    rows: list[dict] = []
    for _, row in df.iterrows():
        migrar = str(row.get("Migrar", "")).strip().lower()
        if migrar != "yes":
            continue

        port = str(row.get("Port", "")).strip()
        if not port:
            continue

        rows.append({col: str(row.get(col, "")) for col in EXCEL_COLUMNS})

    rows.sort(key=lambda r: (r.get("Hostname", ""), _port_sort_key(r.get("Port", ""))))
    return rows


def _group_rows_for_migration(rows: list[dict]) -> dict[str, list[dict]]:
    """Agrupa las filas migrables en stacks y dispositivos individuales."""

    grouped: dict[str, list[dict]] = {}
    stack_planetario: list[dict] = []
    stack_pcl: list[dict] = []

    for row in rows:
        hostname = row.get("Hostname", "")
        upper = hostname.upper()
        if "NA_PLANETARIO" in upper:
            stack_planetario.append(row)
            continue
        if "NA_PCLPLANETARIO" in upper:
            stack_pcl.append(row)
            continue

        grouped.setdefault(hostname or "Desconocido", []).append(row)

    if stack_planetario:
        grouped["Stack_NA_PLANETARIO"] = stack_planetario
    if stack_pcl:
        grouped["Stack_NA_PCLPLANETARIO"] = stack_pcl

    return grouped


def _prompt_target_model(sheet_name: str) -> dict:
    """Pregunta qué modelo se usará para una hoja de migración."""

    print(f"Selecciona el modelo destino para la hoja '{sheet_name}':")
    for key, info in sorted(MIGRATION_TYPE_CATALOG.items()):
        print(f"  [{key}] {info['name']} ({info['ports']} puertos)")

    while True:
        choice = input("Modelo (1-6): ").strip()
        if choice in MIGRATION_TYPE_CATALOG:
            return MIGRATION_TYPE_CATALOG[choice]
        print("Opción no válida. Introduce un número entre 1 y 6.")


def _assign_new_ports(rows: list[dict], model_info: dict) -> list[dict]:
    """Asigna puertos nuevos secuenciales según la capacidad del modelo."""

    port_capacity = model_info.get("ports", 0)
    available_ports = [f"1:{idx}" for idx in range(1, port_capacity + 1)]

    planned: list[dict] = []
    for idx, row in enumerate(rows):
        new_port = available_ports[idx] if idx < len(available_ports) else "SIN_PUERTO"
        planned.append({**row, "Nuevo puerto": new_port, "Modelo destino": model_info.get("name", "")})

    return planned


def build_migration_plan(df: pd.DataFrame) -> dict[str, dict]:
    """Construye el plan de migración por hoja con asignación de puertos."""

    migratable_rows = _filter_migratable_rows(df)
    if not migratable_rows:
        raise ValueError("El Excel no contiene filas con Migrar=Yes y puerto válido.")

    grouped = _group_rows_for_migration(migratable_rows)

    plan: dict[str, dict] = {}
    for sheet_name, rows in grouped.items():
        model_info = _prompt_target_model(sheet_name)
        planned_rows = _assign_new_ports(rows, model_info)
        plan[sheet_name] = {"rows": planned_rows, "model": model_info}

    return plan


def export_migration_excel(plan: dict[str, dict], out_path: str):
    """Genera el Excel de migración con una hoja por stack/dispositivo."""

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        for sheet_name, data in plan.items():
            df = pd.DataFrame(data["rows"], columns=EXCEL_COLUMNS + ["Nuevo puerto", "Modelo destino"])
            df.to_excel(writer, sheet_name=sheet_name[:31] or "Plan", index=False)

    print(f"[OK] Plan de migración generado: {out_path}")


def export_config_plan(plan: dict[str, dict], out_path: str, station: str):
    """Genera un TXT con la plantilla base y el resumen de puertos."""

    first_hostname = ""
    for data in plan.values():
        for row in data["rows"]:
            first_hostname = row.get("Hostname", "Switch")
            if first_hostname:
                break
        if first_hostname:
            break

    station_safe = station or "Estacion"
    header = BASE_CONFIG_TEMPLATE.format(hostname=first_hostname or "Switch", station=station_safe)

    lines = [header]
    for sheet_name, data in plan.items():
        lines.append(f"\n# === Plan {sheet_name} (modelo {data['model'].get('name', '')}) ===")
        for row in data["rows"]:
            lines.append(
                f"- {row.get('Hostname', '')} {row.get('Port', '')} -> {row.get('Nuevo puerto', '')} "
                f"({row.get('Description', '')}) VLANs={row.get('VLANs', '')} Estado={row.get('Estado', '')} "
                f"Migrar={row.get('Migrar', '')}"
            )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"[OK] Configuración TXT generada: {out_path}")

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def _normalize_user_path(entry: str) -> str:
    """Normaliza una ruta introducida por pantalla (quita comillas, expande ~, etc.)."""

    cleaned = entry.strip()

    if not cleaned:
        return ""

    if (cleaned.startswith("\"") and cleaned.endswith("\"")) or (
        cleaned.startswith("'") and cleaned.endswith("'")
    ):
        cleaned = cleaned[1:-1].strip()

    if not cleaned:
        return ""

    cleaned = os.path.expanduser(os.path.expandvars(cleaned))
    cleaned = os.path.abspath(cleaned)

    return cleaned


def prompt_for_log_mode() -> str:
    """Pregunta si se introducirán logs manualmente o por carpeta."""

    prompt = (
        "¿Cómo quieres indicar los logs?\n"
        "  [1] Introducir rutas manualmente (uno por línea, como hasta ahora).\n"
        "  [2] Indicar una carpeta y procesar todos los .log que contenga.\n"
        "Selecciona 1 o 2: "
    )

    while True:
        choice = input(prompt).strip()
        if choice in {"1", "manual"}:
            return "manual"
        if choice in {"2", "carpeta", "folder"}:
            return "folder"
        print("Opción no válida. Escribe 1 para modo manual o 2 para modo carpeta.")


def _prompt_logs_manual() -> list[str]:
    """Solicita los logs manualmente como en versiones previas."""

    available_logs = sorted(
        [f for f in os.listdir(".") if f.lower().endswith(".log") and os.path.isfile(f)]
    )

    if available_logs:
        print("Logs .log detectados en el directorio actual:")
        for log in available_logs:
            print(f"  - {log}")
    else:
        print("No se han encontrado ficheros .log en el directorio actual.")

    print(
        "Introduce los nombres de los logs a procesar, uno por línea.\n"
        "Pulsa Enter sin escribir nada para finalizar.\n"
        "Si no introduces ninguno, se usarán todos los .log detectados en el directorio actual."
    )

    def gather_user_entries() -> list[str]:
        entries: list[str] = []
        while True:
            raw_entry = input("Log: ")
            if not raw_entry.strip():
                break
            normalized = _normalize_user_path(raw_entry)
            if not normalized:
                print("Entrada vacía. Introduce una ruta válida o pulsa Enter para terminar.")
                continue
            entries.append(normalized)
        return entries

    while True:
        typed_logs = gather_user_entries()
        candidate_logs = typed_logs if typed_logs else available_logs

        if not candidate_logs:
            print("No se ha proporcionado ningún log. Inténtalo de nuevo.")
            continue

        valid_logs = [p for p in candidate_logs if os.path.isfile(p)]
        missing_logs = [p for p in candidate_logs if not os.path.isfile(p)]

        if missing_logs:
            print("No se han encontrado los siguientes ficheros:")
            for missing in missing_logs:
                print(f"  - {missing}")

        if valid_logs:
            print("Procesando logs:")
            for p in valid_logs:
                print(f"  - {p}")
            return valid_logs

        print("No se han indicado logs válidos. Vuelve a introducirlos.")


def _prompt_logs_from_directory() -> list[str]:
    """Solicita una carpeta y devuelve todos los .log contenidos en ella."""

    while True:
        folder_input = input(
            "Introduce la carpeta que contiene los .log (Enter = directorio actual): "
        ).strip()

        if not folder_input:
            folder = os.getcwd()
        else:
            folder = _normalize_user_path(folder_input)
            if not folder:
                print("Ruta no válida. Inténtalo de nuevo o deja vacío para usar el directorio actual.")
                continue

        if not os.path.isdir(folder):
            print(f"La ruta indicada no es una carpeta existente: {folder}")
            continue

        logs = sorted(
            [
                os.path.join(folder, name)
                for name in os.listdir(folder)
                if name.lower().endswith(".log") and os.path.isfile(os.path.join(folder, name))
            ]
        )

        if not logs:
            print("La carpeta no contiene ficheros .log. Introduce otra ruta.")
            continue

        print("Se procesarán los siguientes logs:")
        for log in logs:
            print(f"  - {log}")

        return logs


def prompt_for_logs() -> list[str]:
    """Gestiona el flujo de selección de logs (manual o por carpeta)."""

    mode = prompt_for_log_mode()
    if mode == "manual":
        return _prompt_logs_manual()
    return _prompt_logs_from_directory()


def prompt_for_output_directory() -> str:
    """Solicita al usuario la carpeta de salida y la crea si es necesario."""

    while True:
        user_input = input(
            "Introduce la carpeta donde se generarán los ficheros (Enter = directorio actual): "
        ).strip()

        if not user_input:
            out_dir = os.getcwd()
        else:
            normalized = _normalize_user_path(user_input)
            if not normalized:
                print("Ruta no válida. Inténtalo de nuevo o deja vacío para usar el directorio actual.")
                continue
            out_dir = normalized

        if os.path.isdir(out_dir):
            return out_dir

        try:
            os.makedirs(out_dir, exist_ok=True)
            print(f"[INFO] Se ha creado la carpeta: {out_dir}")
            return out_dir
        except OSError as exc:
            print(f"[ERROR] No se pudo crear la carpeta '{out_dir}': {exc}")
            print("Intenta introducir otra ruta.")


def prompt_retry_output_directory(current_dir: str) -> str:
    """Permite reintentar la misma carpeta de salida o introducir otra distinta."""

    print(
        "Cierra el Excel si está abierto. Pulsa Enter para reintentar en la misma "
        "carpeta o introduce otra ruta para guardar el fichero."
    )

    while True:
        user_input = input(
            "Ruta (Enter = reintentar en la misma carpeta): "
        ).strip()

        if not user_input:
            return current_dir

        normalized = _normalize_user_path(user_input)
        if not normalized:
            print("Ruta no válida. Inténtalo de nuevo.")
            continue

        if os.path.isdir(normalized):
            return normalized

        try:
            os.makedirs(normalized, exist_ok=True)
            print(f"[INFO] Se ha creado la carpeta: {normalized}")
            return normalized
        except OSError as exc:
            print(f"[ERROR] No se pudo crear la carpeta '{normalized}': {exc}")
            print("Introduce otra ruta o deja Enter para reintentar.")


def build_separator_row(next_device: dict[str, str]) -> dict:
    """Crea la fila separadora azul con los datos del siguiente log."""

    ip_display = next_device.get("ip") or "IP no detectada"
    hostname = next_device.get("hostname", "")
    log_name = next_device.get("log_name", "")

    return {
        "Hostname": f"Log: {log_name}",
        "Model": next_device.get("model", ""),
        "Port": "",
        "Description": f"Hostname: {hostname} | IP: {ip_display}",
        "Estado": "",
        "Cableado": "",
        "VLANs": "",
        "Last input/output": "",
        "Migrar": "",
        "MACs": "",
    }


def run_inventory_mode():
    """Flujo original: leer logs y generar el Excel de inventario."""

    log_paths = prompt_for_logs()

    station_name = infer_station_name(log_paths)
    print(f"[INFO] Estación detectada: {station_name}")

    output_dir = prompt_for_output_directory()

    processed_logs: list[dict] = []

    for path in log_paths:
        try:
            rows, device_info = parse_log_inventory(path)
        except Exception as e:
            print(f"[ERROR] Procesando {path}: {e}")
            continue

        processed_logs.append({"rows": rows, "metadata": device_info})

    excel_rows: list[dict] = []
    separator_indices: list[int] = []
    first_banner: dict | None = None

    for device in processed_logs:
        rows = device["rows"]
        if not rows:
            continue

        separator_row = build_separator_row(device["metadata"])
        if first_banner is None:
            first_banner = separator_row
        else:
            separator_indices.append(len(excel_rows))
            excel_rows.append(separator_row)

        excel_rows.extend(rows)

    station_slug = sanitize_station_for_filename(station_name)
    excel_filename = f"Migración_{station_slug}_v1.0.xlsx"

    while True:
        excel_path = os.path.join(output_dir, excel_filename)
        try:
            export_to_excel(excel_rows, excel_path, separator_indices, first_banner)
            break
        except PermissionError as exc:
            print(f"[ERROR] No se pudo escribir '{excel_path}': {exc}")
            print(
                "Es posible que el fichero esté abierto en otra aplicación o que "
                "no tengas permisos suficientes."
            )
            output_dir = prompt_retry_output_directory(output_dir)
        except OSError as exc:
            print(f"[ERROR] No se pudo escribir '{excel_path}': {exc}")
            print("Revisa la ruta indicada o selecciona otra carpeta.")
            output_dir = prompt_retry_output_directory(output_dir)


def run_migration_mode():
    """Flujo nuevo: leer Excel existente y generar plan TXT + Excel."""

    inventory_path = prompt_for_inventory_excel()
    output_dir = prompt_for_output_directory()

    try:
        df = _load_inventory_dataframe(inventory_path)
    except Exception as exc:
        print(f"[ERROR] No se pudo leer el Excel: {exc}")
        return

    try:
        plan = build_migration_plan(df)
    except Exception as exc:
        print(f"[ERROR] No se pudo generar el plan de migración: {exc}")
        return

    station_name = infer_station_name([inventory_path])
    station_slug = sanitize_station_for_filename(station_name)

    migration_excel = f"Plan_migracion_{station_slug}.xlsx"
    config_txt = f"config_migracion_{station_slug}.txt"

    while True:
        excel_path = os.path.join(output_dir, migration_excel)
        try:
            export_migration_excel(plan, excel_path)
            break
        except PermissionError as exc:
            print(f"[ERROR] No se pudo escribir '{excel_path}': {exc}")
            output_dir = prompt_retry_output_directory(output_dir)
        except OSError as exc:
            print(f"[ERROR] No se pudo escribir '{excel_path}': {exc}")
            output_dir = prompt_retry_output_directory(output_dir)

    txt_path = os.path.join(output_dir, config_txt)
    try:
        export_config_plan(plan, txt_path, station_name)
    except Exception as exc:
        print(f"[ERROR] No se pudo generar el TXT: {exc}")


def main():
    mode = prompt_primary_mode()
    if mode == "inventario":
        run_inventory_mode()
    else:
        run_migration_mode()


if __name__ == "__main__":
    main()
