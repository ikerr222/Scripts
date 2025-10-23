# ==============================================================================
# Migración Ambar/UCA — Aplica BASE completa (según tipo) + Interfaces nuevas
# (VERSIÓN SIN TRUNK: no genera ni usa puertos trunk ni port-channels)
# - Pide hostname AMBAR (para POE y AMBAR_T) y hostname UCA (para UCA).
# - Para cada switch destino:
#     1) Inserta la PLANTILLA BASE COMPLETA del tipo (AMBAR/UCA), forzando 'hostname <pedido>'.
#     2) Añade interfaces nuevas clonadas del running de origen (sin 'sticky').
#     3) NO genera nada relativo a trunks o port-channels.
# ==============================================================================

import os
import re
import sys
import unicodedata
from datetime import datetime
from typing import Iterable, List, Dict, Tuple, Optional, Callable, Union

import pandas as pd

# --- Parámetros de switches destino (nombres internos para el mapeo) ---
NEW_SWITCH_WIFI_NAME    = "SW-NUEVO-UXM-01"   # Miembro con WIFI (TwoGig/TenGig)
NEW_SWITCH_POE_NAME     = "SW-NUEVO-POE-01"   # Miembros POE sin WIFI

NEW_SWITCH_AMBAR_T_NAME = "SW-NUEVO-AMBAR-T-01"
NEW_IF_AMBAR_T_PREFIX   = "GigabitEthernet1/0/"

NEW_SWITCH_UCA_T_NAME   = "SW-NUEVO-UCA-T-01"
NEW_IF_UCA_T_PREFIX     = "GigabitEthernet1/0/"

# --- Conjuntos de VLAN objetivo ---
WIFI_VLANS = {"360", "361"}
VOIP_VLANS = {"1211", "1223", "1225", "1226", "1227", "1228", "1229"}
UCA_VLANS  = {
    "3", "29", "134", "137", "138", "141", "142", "451", "453", "454", "455",
    "623", "2204", "2205", "2206", "2207", "2208", "2209", "2210", "2211", "2212",
    "2215", "2216", "2217", "2218", "2219", "2221", "2222", "2230", "2300"
}
IGNORED_VLANS = {"606"}

# --- Capacidad y reservas del switch POE ---
POE_MAX_PORTS          = 48
RESERVED_AFTER_WIFI    = 4
RESERVED_TAIL_FREE     = 10
POE_AUX_FINAL_PORTS    = 24   # último miembro de 24 si faltan <= 24
POE_MAX_STACK_MEMBERS  = 3    # WIFI + POE + AMBAR (máximo)
WIFI_TWOGIG_LIMIT      = 36

# --- Capacidad de switches adicionales ---
AMBAR_T_MAX_PORTS = 48
UCA_DEFAULT_PORTS = 24
UCA_LARGE_PORTS   = 48

# --- Rutas por defecto de plantillas base ---
AMBAR_TEMPLATE_DEFAULT       = "AMBAR template actualizado v3.txt"
UCA_TEMPLATE_DEFAULT         = "UCA template actualizado v2.txt"
AMBAR_EXTRA_TEMPLATE_DEFAULT = "ambar_config_base_extra.txt"
UCA_EXTRA_TEMPLATE_DEFAULT   = "uca_config_base_extra.txt"

# --- Estado dinámico del mapeo POE ---
POE_MEMBER_METADATA: Dict[int, Dict[str, object]] = {}

# ---------- Parsers de los logs ----------

EQUIPO_RE = re.compile(r"^\s*Equipo:\s*([A-Za-z0-9\-\._/]+)", re.IGNORECASE)

# Show interface status
INT_STATUS_HEADER_RE = re.compile(r"^\s*Port\s+Name\s+Status\s+Vlan\s+Duplex", re.IGNORECASE)
INT_STATUS_ROW_RE    = re.compile(
    r"^\s*(?P<port>(?:Fa|Gi|Te)\d+(?:/\d+){0,2})\s+(?P<name>.*?)\s+"
    r"(?P<status>connected|notconnect|disabled)\s+(?P<vlan>\S+)\s+",
    re.IGNORECASE
)

# Show mac address-table
MAC_HEADER_RE = re.compile(r"^\s*Vlan\s+Mac\s+Address\s+Type\s+Ports", re.IGNORECASE)
MAC_ROW_RE    = re.compile(
    r"^\s*(?P<vlan>\S+)\s+(?P<mac>[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4})\s+"
    r"(?P<type>STATIC|DYNAMIC)\s+(?P<port>.+?)\s*$", re.IGNORECASE
)

# show running-config (interfaces)
IFACE_START_RE = re.compile(r"^\s*interface\s+(\S+)", re.IGNORECASE)
VOICE_VLAN_RE  = re.compile(r"^\s*switchport\s+voice\s+vlan\s+(\d+)", re.IGNORECASE)

# --- Filtros de seguridad / parsing ---
STICKY_LINE_RE   = re.compile(r"^\s*switchport\s+port-?security.*sticky\b", re.IGNORECASE)
BASE_HOSTNAME_RE = re.compile(r"^\s*hostname\s+\S+", re.IGNORECASE)

# ---------- Utilidades ----------

def to_short_ifname(ifname: str) -> str:
    s = ifname.strip()
    low = s.lower()
    if low.startswith("tengigabitethernet"):
        return "Te" + s[len("tengigabitethernet"):]
    if low.startswith("twogigabitethernet"):
        return "Tw" + s[len("twogigabitethernet"):]
    if low.startswith("twentyfivegige"):
        return "Twe" + s[len("twentyfivegige"):]
    if low.startswith("twentyfivegigabitethernet"):
        return "Twe" + s[len("twentyfivegigabitethernet"):]
    if low.startswith("gigabitethernet"):
        return "Gi" + s[len("gigabitethernet"):]
    if low.startswith("fastethernet"):
        return "Fa" + s[len("fastethernet"):]
    m = re.match(r"(?:Fa|Gi|Te)\d+(?:/\d+){0,2}", s, re.IGNORECASE)
    return m.group(0) if m else s

def normalize_port(p: str) -> str:
    p = p.strip()
    m = re.search(r"(Fa|Gi|Te|Tw|Twe)\d+(?:/\d+){0,2}", p, re.IGNORECASE)
    return m.group(0) if m else ""

# ---------- Helpers de nombres/interfaz ----------

def _format_numbered_name(base_name: str, ordinal: int) -> str:
    m = re.match(r"^(.*?)(\d+)$", base_name)
    if m:
        width = len(m.group(2))
        return f"{m.group(1)}{ordinal:0{width}d}"
    base = base_name.rstrip("-")
    return f"{base}-{ordinal:02d}" if base else f"{ordinal:02d}"

def _resolve_new_interface(builder: Union[Callable[[int], str], str], idx: int) -> str:
    if callable(builder):
        return builder(idx)
    return f"{builder}{idx}"

# ---------- Parseos de running-config ----------

def parse_voice_vlans_from_running_config(lines: List[str]) -> Dict[str, str]:
    voice_map: Dict[str, str] = {}
    current_if: Optional[str] = None
    for line in lines:
        m_if = IFACE_START_RE.search(line)
        if m_if:
            current_if = to_short_ifname(m_if.group(1))
            continue
        if current_if:
            m_v = VOICE_VLAN_RE.search(line)
            if m_v:
                voice_map[current_if] = m_v.group(1)
            if line.strip() == "!" or (line and not line[0].isspace()):
                current_if = None
    return voice_map

def parse_interface_configs_from_running_config(lines: List[str]) -> Dict[str, List[str]]:
    iface_cfg: Dict[str, List[str]] = {}
    current_if: Optional[str] = None
    buf: List[str] = []

    def flush():
        nonlocal current_if, buf
        if current_if is not None:
            iface_cfg[current_if] = buf[:]
        current_if, buf = None, []

    for raw in lines:
        m = IFACE_START_RE.search(raw)
        if m:
            flush()
            current_if = to_short_ifname(m.group(1))
            buf = []
            continue
        if current_if is not None:
            ln = raw.rstrip("\n")
            if ln.strip() == "!":
                flush()
            else:
                buf.append(ln)
    flush()
    return iface_cfg

# ---------- Parser de logs ----------

def parse_log(filepath: str):
    hostname = None
    int_rows = []
    mac_static: Dict[str, List[str]] = {}
    mac_dynamic: Dict[str, List[str]] = {}

    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    # Equipo:
    for line in lines:
        m = EQUIPO_RE.search(line)
        if m:
            hostname = m.group(1).strip()
            break
    if not hostname:
        hostname = os.path.basename(filepath)

    # show int status
    in_int_status = False
    for line in lines:
        if INT_STATUS_HEADER_RE.search(line):
            in_int_status = True
            continue
        if in_int_status:
            if not line.strip():
                in_int_status = False
                continue
            m = INT_STATUS_ROW_RE.search(line)
            if m:
                int_rows.append({
                    "port":   m.group("port").strip(),
                    "name":   m.group("name").strip(),
                    "status": m.group("status").lower(),
                    "vlan":   m.group("vlan").strip()
                })

    # mac address-table
    in_mac = False
    for line in lines:
        if MAC_HEADER_RE.search(line):
            in_mac = True
            continue
        if in_mac:
            m = MAC_ROW_RE.search(line)
            if not m:
                continue
            port = normalize_port(m.group("port"))
            if not port:
                continue
            mac = m.group("mac").lower()
            mtype = m.group("type").upper()
            (mac_static if mtype == "STATIC" else mac_dynamic).setdefault(port, []).append(mac)

    mac_map = {p: mac_static.get(p) or mac_dynamic.get(p) or [] for p in set(mac_static) | set(mac_dynamic)}

    # running-config: voice por interfaz + bloques por interfaz
    voice_map     = parse_voice_vlans_from_running_config(lines)
    iface_cfg_map = parse_interface_configs_from_running_config(lines)

    return hostname, int_rows, mac_map, voice_map, iface_cfg_map

# ---------- Inventarios ----------

def build_inventory_from_logs(filepaths: List[str]):
    """
    Devuelve:
      - wifi_items, voip_items, uca_items, ambar_other_items (listas de dict)
      - all_iface_cfgs: {(host, if_short): [líneas running sin 'interface' ni '!']}
    (Sin trunks: ignora entradas con VLAN 'trunk')
    """
    wifi_items, voip_items, uca_items, ambar_other_items = [], [], [], []
    all_iface_cfgs: Dict[Tuple[str, str], List[str]] = {}

    for fp in filepaths:
        host, int_rows, mac_map, voice_map, iface_cfg_map = parse_log(fp)

        # Guarda bloques running por interfaz
        for if_short, block in iface_cfg_map.items():
            all_iface_cfgs[(host, if_short)] = block

        for r in int_rows:
            if r["status"] != "connected":
                continue
            vlan = r["vlan"].strip()
            if vlan in IGNORED_VLANS:
                continue
            port_short = r["port"]
            voice_vlan = voice_map.get(port_short)

            vlan_lc = vlan.lower()
            if not vlan.isdigit():
                # Ignora explícitamente cualquier puerto en modo trunk
                if vlan_lc == "trunk":
                    continue
                else:
                    # Formatos raros no numéricos -> descartar
                    continue

            item = {
                "src_host": host,
                "src_port": port_short,
                "name": r["name"] if r["name"] else "N/A",
                "vlan": vlan,
                "macs": mac_map.get(port_short, []),
                "voice_vlan": voice_vlan,
                "mode": "access"
            }

            if vlan in WIFI_VLANS:
                wifi_items.append(item)
            elif voice_vlan and voice_vlan in VOIP_VLANS:
                voip_items.append(item)
            elif vlan in UCA_VLANS:
                uca_items.append(item)
            else:
                ambar_other_items.append(item)

    def port_key(p):
        nums = re.findall(r"\d+", p)
        return tuple(int(x) for x in nums) if nums else (9999,)

    wifi_items.sort(key=lambda x: (x["src_host"], port_key(x["src_port"])))
    voip_items.sort(key=lambda x: (x["src_host"], port_key(x["src_port"])))
    uca_items.sort(key=lambda x: (x["src_host"], port_key(x["src_port"])))
    ambar_other_items.sort(key=lambda x: (x["src_host"], port_key(x["src_port"])))
    return wifi_items, voip_items, uca_items, ambar_other_items, all_iface_cfgs


# ---------- Mapeos ----------

def _mk_row(new_if_builder, idx_new, item, sw_name, group_tag):
    new_if = _resolve_new_interface(new_if_builder, idx_new)
    mac_str = ", ".join(item["macs"]) if item["macs"] else "N/A"
    tag_str = f"VOICE={item['voice_vlan']}" if item.get("voice_vlan") else "N/A"
    return [
        item["src_host"],           # SW Actual
        item["src_port"],           # Interface actual
        item["name"],               # Description actual
        new_if,                      # Interface nuevo
        item["name"],               # Description nueva
        sw_name,                     # SW Nuevo (interno / etiqueta)
        item["vlan"],               # VLAN (informativo)
        "access",                   # Mode (sin trunk)
        tag_str,                     # Tags (p.ej., VOICE=1229)
        mac_str,                     # MAC Actual
        group_tag                    # _Grupo
    ]

def _poe_switch_name_for_member(member_index: int) -> str:
    meta = POE_MEMBER_METADATA.get(member_index)
    if meta:
        return str(meta.get("sw_name"))
    if member_index == 1:
        return NEW_SWITCH_POE_NAME
    return _format_numbered_name(NEW_SWITCH_POE_NAME, member_index)

def _format_poe_interface(member_index: int, port_idx: int) -> str:
    meta = POE_MEMBER_METADATA.get(member_index, {})
    member_type = meta.get("type")
    if member_type == "wifi":
        if port_idx <= WIFI_TWOGIG_LIMIT:
            prefix = f"TwoGigabitEthernet{member_index}/0/"
        else:
            prefix = f"TenGigabitEthernet{member_index}/0/"
    else:
        prefix = f"GigabitEthernet{member_index}/0/"
    return f"{prefix}{port_idx}"

def _poe_interface_formatter(member_index: int) -> Callable[[int], str]:
    return lambda port_idx, member=member_index: _format_poe_interface(member, port_idx)

def _libre_row(builder, idx: int, sw_name: str, group_tag: str):
    new_if = _resolve_new_interface(builder, idx)
    return ["LIBRE","LIBRE","LIBRE",new_if,"LIBRE",sw_name,"LIBRE","access","LIBRE","N/A",group_tag]

def _pad_member_with_libres(rows, builder, sw_name, start_idx, capacity, group_tag):
    idx = start_idx
    while idx <= capacity:
        rows.append(_libre_row(builder, idx, sw_name, group_tag))
        idx += 1

def _poe_member_capacities(required_slots: int):
    capacities: List[int] = []
    remaining = max(0, required_slots)
    member_index = 0
    while remaining > 0 and member_index < POE_MAX_STACK_MEMBERS:
        member_index += 1
        if member_index < POE_MAX_STACK_MEMBERS:
            cap = POE_MAX_PORTS
        else:
            # Último miembro: usa 24 puertos (22 efectivos) si lo restante cabe, si no 48.
            usable_in_aux = max(POE_AUX_FINAL_PORTS - 2, 0)
            cap = POE_MAX_PORTS if remaining > usable_in_aux else POE_AUX_FINAL_PORTS
        capacities.append(cap)
        remaining = max(0, remaining - max(cap - 2, 0))
    if not capacities and required_slots > 0:
        capacities.append(POE_MAX_PORTS)
    return capacities

def make_mapping_poe(wifi_items, voip_items, ambar_others):
    global POE_MEMBER_METADATA
    has_wifi = bool(wifi_items)
    has_voip = bool(voip_items)
    reserve_after_wifi = RESERVED_AFTER_WIFI if has_wifi else 0
    tail_free = RESERVED_TAIL_FREE if (has_wifi or has_voip) else 0

    base_slots = len(wifi_items) + reserve_after_wifi + len(voip_items) + tail_free
    required_slots = base_slots
    if required_slots == 0 and ambar_others:
        max_stack_capacity = POE_MAX_PORTS * POE_MAX_STACK_MEMBERS
        required_slots = min(len(ambar_others), max_stack_capacity)

    capacities = _poe_member_capacities(required_slots)

    if not capacities:
        POE_MEMBER_METADATA.clear()
        return [], ambar_others

    POE_MEMBER_METADATA.clear()
    wifi_counter = 0
    poe_counter = 0
    for member_index, capacity in enumerate(capacities, start=1):
        if has_wifi and member_index == 1:
            wifi_counter += 1
            sw_name = _format_numbered_name(NEW_SWITCH_WIFI_NAME, wifi_counter)
            member_type = "wifi"
        else:
            poe_counter += 1
            sw_name = _format_numbered_name(NEW_SWITCH_POE_NAME, poe_counter)
            member_type = "poe"
        POE_MEMBER_METADATA[member_index] = {
            "type": member_type,
            "sw_name": sw_name,
            "capacity": capacity,
            "usable_capacity": max(capacity - 2, 0),
        }

    total_usable = sum(info["usable_capacity"] for info in POE_MEMBER_METADATA.values())
    if base_slots > total_usable:
        raise RuntimeError("Capacidad POE insuficiente para WIFI/VoIP/tail configurados")

    slots_for_ambar = max(0, total_usable - base_slots)
    ambar_for_poe = ambar_others[:slots_for_ambar]
    ambar_overflow = ambar_others[slots_for_ambar:]

    sequence = []
    sequence.extend(("ITEM", it) for it in wifi_items)
    sequence.extend(("LIBRE", None) for _ in range(reserve_after_wifi))
    sequence.extend(("ITEM", it) for it in voip_items)
    sequence.extend(("ITEM", it) for it in ambar_for_poe)
    sequence.extend(("LIBRE", None) for _ in range(tail_free))

    rows: List[List[str]] = []
    member_index = 1
    idx = 1

    for kind, payload in sequence:
        while True:
            member_info = POE_MEMBER_METADATA[member_index]
            capacity = int(member_info["capacity"])
            usable_capacity = int(member_info["usable_capacity"])
            formatter = _poe_interface_formatter(member_index)
            sw_name = str(member_info["sw_name"])

            if usable_capacity <= 0:
                _pad_member_with_libres(rows, formatter, sw_name, 1, capacity, "POE")
                member_index += 1
                if member_index > len(capacities):
                    raise RuntimeError("Capacidad POE insuficiente para la secuencia generada")
                idx = 1
                continue

            if idx > usable_capacity:
                _pad_member_with_libres(rows, formatter, sw_name, max(idx, usable_capacity + 1), capacity, "POE")
                member_index += 1
                if member_index > len(capacities):
                    raise RuntimeError("Capacidad POE insuficiente para la secuencia generada")
                idx = 1
                continue

            if kind == "ITEM":
                rows.append(_mk_row(formatter, idx, payload, sw_name, "POE"))
            else:
                rows.append(_libre_row(formatter, idx, sw_name, "POE"))
            idx += 1
            break

    while True:
        member_info = POE_MEMBER_METADATA[member_index]
        capacity = int(member_info["capacity"])
        formatter = _poe_interface_formatter(member_index)
        sw_name = str(member_info["sw_name"])
        _pad_member_with_libres(rows, formatter, sw_name, idx, capacity, "POE")
        if member_index >= len(capacities):
            break
        member_index += 1
        idx = 1

    return rows, ambar_overflow

def make_mapping_ambar_t(ambar_items):
    if not ambar_items:
        return []

    rows: List[List[str]] = []
    total = len(ambar_items)
    idx_item = 0
    switch_ordinal = 1

    while idx_item < total:
        sw_name = _format_numbered_name(NEW_SWITCH_AMBAR_T_NAME, switch_ordinal)
        usable = max(AMBAR_T_MAX_PORTS - 2, 0)
        for port_idx in range(1, AMBAR_T_MAX_PORTS + 1):
            if port_idx <= usable and idx_item < total:
                rows.append(_mk_row(NEW_IF_AMBAR_T_PREFIX, port_idx, ambar_items[idx_item], sw_name, "AMBAR_T"))
                idx_item += 1
            else:
                rows.append(_libre_row(NEW_IF_AMBAR_T_PREFIX, port_idx, sw_name, "AMBAR_T"))
        switch_ordinal += 1

    return rows

def _uca_interface_formatter() -> Callable[[int], str]:
    prefix = NEW_IF_UCA_T_PREFIX
    return lambda port_idx, pref=prefix: f"{pref}{port_idx}"

def _uca_switch_capacities(required_ports: int) -> List[int]:
    capacities: List[int] = []
    remaining = required_ports
    if remaining <= 0:
        return [UCA_DEFAULT_PORTS]
    while remaining > 0:
        if remaining > max(UCA_LARGE_PORTS - 2, 0):
            cap = UCA_LARGE_PORTS
        elif remaining > max(UCA_DEFAULT_PORTS - 2, 0):
            cap = UCA_LARGE_PORTS
        else:
            cap = UCA_DEFAULT_PORTS
        capacities.append(cap)
        remaining -= max(cap - 2, 0)
    return capacities if capacities else [UCA_DEFAULT_PORTS]

def make_mapping_uca(uca_items):
    total_items = len(uca_items)
    capacities = _uca_switch_capacities(total_items)
    rows: List[List[str]] = []
    idx_item = 0
    switch_ordinal = 1
    formatter = _uca_interface_formatter()

    for capacity in capacities:
        sw_name = _format_numbered_name(NEW_SWITCH_UCA_T_NAME, switch_ordinal)
        usable = max(capacity - 2, 0)
        for port_idx in range(1, capacity + 1):
            if port_idx <= usable and idx_item < total_items:
                rows.append(_mk_row(formatter, port_idx, uca_items[idx_item], sw_name, "UCA"))
                idx_item += 1
            else:
                rows.append(_libre_row(formatter, port_idx, sw_name, "UCA"))
        switch_ordinal += 1

    return rows

def _extract_if_index(ifname: str) -> Optional[int]:
    m = re.search(r"(\d+)$", ifname)
    return int(m.group(1)) if m else None

def _sort_group_rows(rows: List[List[str]]) -> None:
    rows.sort(key=lambda r: (r[5], _extract_if_index(r[3]) or 0))

# ---------- Excel ----------

def export_excel(all_rows, out_dir):
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    xlsx = os.path.join(out_dir, f"mapeo_wifi_voip_ambar_uca_{ts}.xlsx")

    cols = [
        "SW Actual","Interface actual","Description actual","Interface nuevo",
        "Description nueva","SW Nuevo","VLAN","Mode","Tags","MAC Actual","_Grupo"
    ]
    df = pd.DataFrame(all_rows, columns=cols)

    with pd.ExcelWriter(xlsx, engine="xlsxwriter") as w:
        df.to_excel(w, sheet_name="Mapeo", index=False)
        wb = w.book
        ws = w.sheets["Mapeo"]

        fmt_header = wb.add_format({'bold': True})
        fmt_libre  = wb.add_format({'bg_color': '#FFF59D'})
        fmt_uca    = wb.add_format({'bg_color': '#CFE8FF'})
        fmt_amb_t  = wb.add_format({'bg_color': '#E6F4EA'})

        ws.set_row(0, None, fmt_header)
        ws.set_column("G:G", None, wb.add_format({'num_format': '@'}))

        sw_actual_list = df["SW Actual"].tolist()
        grupo_list     = df["_Grupo"].tolist()
        for i in range(1, len(df)+1):
            if str(sw_actual_list[i-1]).upper() == "LIBRE":
                ws.set_row(i, None, fmt_libre)
            elif grupo_list[i-1] == "UCA":
                ws.set_row(i, None, fmt_uca)
            elif grupo_list[i-1] == "AMBAR_T":
                ws.set_row(i, None, fmt_amb_t)

        col_index = df.columns.get_loc("_Grupo")
        ws.set_column(col_index, col_index, None, None, {'hidden': True})

    return xlsx

# ---------- Plantillas base + export de configs ----------

def pick_existing_path(name: str):
    def variants(base: str):
        base = base.strip()
        base_nfc = unicodedata.normalize("NFC", base)
        base_nfd = unicodedata.normalize("NFD", base)
        swap_dash = base.replace("\u2013", "-").replace("\u2014", "-")
        with_dash = base.replace("-", "\u2013")
        return list(dict.fromkeys([base, base_nfc, base_nfd, swap_dash, with_dash]))

    search_dirs = ["", "templates", "shrunActual", "/mnt/data"]
    for v in variants(name):
        if os.path.isabs(v) and os.path.exists(v):
            return v
        for d in search_dirs:
            path = os.path.join(d, v) if d else v
            if os.path.exists(path):
                return path
    return None

AMBAR_TEMPLATE_PATH       = pick_existing_path(AMBAR_TEMPLATE_DEFAULT)       or AMBAR_TEMPLATE_DEFAULT
UCA_TEMPLATE_PATH         = pick_existing_path(UCA_TEMPLATE_DEFAULT)         or UCA_TEMPLATE_DEFAULT
AMBAR_EXTRA_TEMPLATE_PATH = pick_existing_path(AMBAR_EXTRA_TEMPLATE_DEFAULT) or AMBAR_EXTRA_TEMPLATE_DEFAULT
UCA_EXTRA_TEMPLATE_PATH   = pick_existing_path(UCA_EXTRA_TEMPLATE_DEFAULT)   or UCA_EXTRA_TEMPLATE_DEFAULT

def _read_template_file(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            ln = raw.rstrip("\n")
            if STICKY_LINE_RE.search(ln):
                continue
            if BASE_HOSTNAME_RE.match(ln):
                continue
            if ln.strip().lower() == "end":
                continue
            out.append(ln)
    return out

def _chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]

def _ambar_extra_base_lines() -> List[str]:
    lines = [
        "clock timezone GMT 1 0",
        "clock summer-time GMT recurring 1 Sun Mar 2:00 last Sun Oct 2:00",
        "!",
        "aaa new-model",
        "!",
        "aaa group server tacacs+ ISE_GROUP",
        " server name ISE",
        " server name ISE2",
        "!",
        "service password-encryption",
        "!",
        "aaa authentication username-prompt LOCAL_username:",
        "aaa authentication password-prompt LOCAL_password:",
        "aaa authentication login TAC-AUTH group tacacs+ local",
        "aaa authorization config-commands",
        "aaa authorization exec TAC-AUTO group tacacs+ local if-authenticated",
        "aaa authorization commands 0 default group tacacs+ local",
        "aaa authorization commands 1 default group tacacs+ local",
        "aaa authorization commands 15 default group tacacs+ local",
        "aaa accounting exec TAC-ACC start-stop group tacacs+",
        "aaa accounting commands 0 default start-stop group tacacs+",
        "aaa accounting commands 1 default start-stop group tacacs+",
        "aaa accounting commands 15 default start-stop group tacacs+",
        "!",
        "aaa session-id common",
        "!",
        "no ip domain lookup",
        "vtp domain AMBAR",
        "vtp mode transparent",
        "ip domain name aena.es",
        "!",
        "login block-for 30 attempts 10 within 60",
        "!",
        "license smart url cslu https://104.16.0.102/cslu/v1/pi/BcnSSC-1",
        "!",
        "logging trap debugging",
        "logging host 104.18.220.10",
        "!",
        "spanning-tree mode mst",
        "spanning-tree portfast default",
        "spanning-tree portfast bpduguard default",
        "spanning-tree extend system-id",
        "!",
        "spanning-tree mst configuration",
        " name AMBAR",
        " revision 1",
    ]

    for chunk in _chunked(list(range(1, 4094, 2)), 12):
        lines.append(" instance 1 vlan " + ", ".join(str(n) for n in chunk))
    for chunk in _chunked(list(range(2, 4095, 2)), 12):
        lines.append(" instance 2 vlan " + ", ".join(str(n) for n in chunk))

    lines.extend([
        "!",
        "spanning-tree mst hello-time 1",
        "spanning-tree mst forward-time 4",
        "spanning-tree mst max-age 6",
        "spanning-tree mst max-hops 4",
        "!",
        "vlan 623",
        "name BCN_ACC_GESTION_AMBAR",
        "!",
        "interface GigabitEthernet0/0",
        " vrf forwarding Mgmt-vrf",
        " ip address 6.6.6.6 255.255.255.252",
        " negotiation auto",
        "no shutdown",
        "!",
        "interface Vlan623",
        " ip address 10.192.130.xx 255.255.254.0",
        "!",
        "lldp run",
        "!",
        "ip tftp source-interface Vlan623",
        "ip ssh time-out 60",
        "ip ssh authentication-retries 2",
        "ip ssh version 2",
        "ip scp server enable",
        "!",
        "archive",
        " log config",
        "  logging enable",
        "  logging size 1000",
        "  notify syslog contenttype plaintext",
        "  hidekeys",
        "memory free low-watermark processor 79475",
        "!",
        "enable secret 0 Aena.2025!",
        "!",
        "username admin privilege 15 secret 0 4BCN@ena.2024,",
        "username NAC privilege 7 secret 9 $14$vVH7$Ezprg0JsSnaXVU$mxbty5BUxkUggfGwRM.D8TUnrhy2dgbt.i53OjvP6GQ",
        "!",
        "!",
        "ip default-gateway 10.192.131.254",
        "ip forward-protocol nd",
        "no ip http server",
        "ip http authentication aaa login-authentication TAC-AUTH",
        "ip http authentication aaa exec-authorization TAC-AUTO",
        "ip http secure-server",
        "ip http client source-interface Vlan623",
        "ip tftp source-interface Vlan623",
        "ip tftp blocksize 512",
        "ip ssh time-out 60",
        "ip ssh authentication-retries 2",
        "ip ssh version 2",
        "ip scp server enable",
        "!",
        "!",
        "logging trap debugging",
        "logging host 4.9.0.135",
        "logging host 104.16.0.225",
        "logging host 104.18.220.10",
        "!",
        "tacacs server ISE",
        " address ipv4 104.16.0.210",
        " key 0 @Amb@r.2024!",
        "tacacs server ISE2",
        " address ipv4 104.16.0.211",
        " key 0 @Amb@r.2024!",
        "!",
        "snmp-server group V3gesred v3 priv notify *tv.FFFFFFFF.FFFFFFFF.FFFFFFFF.FFFFFFFF7F",
        "snmp-server group nacgroup v3 auth read nacview write nacview",
        "snmp-server group nacgroup v3 auth context AO-SIPAS_SVC2 read nacview write nacview",
        "snmp-server group nacgroup v3 auth context AO-SIPAS_SVC3 read nacview write nacview",
        "snmp-server group V3groupDCOM v3 priv read V3bcnAMBro write V3bcnAMBrw",
        "snmp-server view V3bcnAMBro iso included",
        "snmp-server view V3bcnAMBrw iso included",
        "snmp-server view nacview iso included",
        "snmp-server view V3GreBCNro iso included",
        "snmp-server view NO_BAD_SNMP iso included",
        "snmp-server view NO_BAD_SNMP internet included",
        "snmp-server view NO_BAD_SNMP snmpUsmMIB excluded",
        "snmp-server view NO_BAD_SNMP snmpVacmMIB excluded",
        "snmp-server view NO_BAD_SNMP snmpCommunityMIB excluded",
        "snmp-server view NO_BAD_SNMP transmission.94 excluded",
        "snmp-server view NO_BAD_SNMP mib-2.34.9 excluded",
        "snmp-server view NO_BAD_SNMP ciscoMgmt.35 excluded",
        "snmp-server view NO_BAD_SNMP ciscoMgmt.95 excluded",
        "snmp-server view NO_BAD_SNMP ciscoMgmt.130 excluded",
        "snmp-server view NO_BAD_SNMP ciscoMgmt.219 excluded",
        "snmp-server view NO_BAD_SNMP ciscoMgmt.252 excluded",
        "snmp-server view NO_BAD_SNMP ciscoMgmt.254 excluded",
        "snmp-server view NO_BAD_SNMP ciscoExperiment.997 excluded",
        "snmp-server community V3bcnAMBro RO",
        "snmp-server community V3bcnAMBrw RW",
        "snmp-server user V3Dcom V3groupDCOM v3 auth sha #2024@En@! priv des @En@,.2025!",
        "snmp-server community GreBCNro RO",
        "snmp-server community BCN2010rw RW",
        "snmp-server enable traps",
        "snmp-server host 104.16.0.225 version 2c GreBCNro",
        "snmp-server host 104.16.0.225 version 3 priv V3gesred",
        "snmp-server host 4.9.0.135 version 2c GreBCNro",
        "snmp-server host 4.9.0.135 version 3 priv V3gesred",
        "snmp-server host 104.18.220.10 version 2c GreBCNro",
        "snmp-server host 104.18.220.10 version 3 priv V3gesred",
        "!",
        "banner exec ^CC",
        "Session established to $(hostname) on line $(line)",
        "^C",
        "banner incoming ^CC",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! PROHIBIDO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "!!                                                                            !!",
        "!!           Queda totalmente prohibido el uso del protocolo TELNET           !!",
        "!!                   Contacte con el administrador de la red                  !!",
        "!!                                                                            !!",
        "!!           The use of the TELNET protocol is completely prohibited          !!",
        "!!                       Contact the network administrator                    !!",
        "!!                                                                            !!",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! FORBIDDEN !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "^C",
        "banner login ^CC",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! ATENCION !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "!!                                                                            !!",
        "!!                          Solo personal autorizado                          !!",
        "!!                                                                            !!",
        "!!                          Authorized personal only                          !!",
        "!!                                                                            !!",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! CAUTION !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "^C",
        "banner motd ^CC",
        "********************************************************************************",
        "**                                                                            **",
        "**                       AENA - Aeropuerto Barcelona                          **",
        "**                                                                            **",
        "**         Division de Tecnologias de la Informacion y Comunicaciones         **",
        "**                                                                            **",
        "**                  Esta accediendo a un sistema protegido                    **",
        "**          Si no esta autorizado cierre inmediatamente su conexion           **",
        "**                  La manipulacion no autorizada infringe                    **",
        "**             la ley 21/2003, de 7 de Julio, de Seguridad Aerea              **",
        "**                                                                            **",
        "********************************************************************************",
        "**                                                                            **",
        "**                         This is a Private System                           **",
        "**          If you are not authorized close your connection inmediatly        **",
        "**                    Unauthorized access is regulated by                     **",
        "**                   Air Security Law 21/2003, 7th of July                    **",
        "**                                                                            **",
        "********************************************************************************",
        "|                          Informacion de Acceso",
        "|                          Equipo: $(hostname)",
        "|",
        "|                       Autorizacion mediante TACACS+",
        "|",
        "^C",
        "banner prompt-timeout ^CC",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! ADVERTENCIA !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "!!                                                                            !!",
        "!!                      Ha experiado el tiempo de session                     !!",
        "!!                                                                            !!",
        "!!                        Has experienced session time                        !!",
        "!!                                                                            !!",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! WARNING !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "^C",
        "banner config-save ^CC",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! INFORMACION !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "!!                                                                            !!",
        "!!         Desea realizar una copia de la configuracion en ejecucion          !!",
        "!!                                                                            !!",
        "!!           You want to make a copy of the running configuration             !!",
        "!!                                                                            !!",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! INFORMATION !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "^C",
        "!",
        "!",
        "call-home",
        " ! If contact email address in call-home is configured as sch-smart-licensing@cisco.com",
        " ! the email address configured in Cisco Smart License Portal will be used as contact email address to send SCH notifications.",
        " contact-email-addr sch-smart-licensing@cisco.com",
        " no http secure server-identity-check",
        " profile \"CiscoTAC-1\"",
        "  active",
        "  destination transport-method http",
        "  no destination address http https://tools.cisco.com/its/service/oddce/services/DDCEService ",
        " profile \"BCNscc\"",
        "  destination transport-method http",
        "  destination address http https://104.16.0.102:443/Transportgateway/services/DeviceRequestHandler ",
        "!",
        "line con 0",
        " session-timeout 15",
        " exec-timeout 15 0",
        " authorization exec TAC-AUTO",
        " accounting exec TAC-ACC",
        " logging synchronous",
        " login authentication TAC-AUTH",
        " stopbits 1",
        "line vty 0 4",
        " session-timeout 15",
        " exec-timeout 15 0",
        " authorization exec TAC-AUTO",
        " accounting exec TAC-ACC",
        " logging synchronous",
        " login authentication TAC-AUTH",
        " transport preferred ssh",
        "line vty 5 15",
        " session-timeout 15",
        " exec-timeout 15 0",
        " authorization exec TAC-AUTO",
        " accounting exec TAC-ACC",
        " logging synchronous",
        " login authentication TAC-AUTH",
        " transport preferred ssh",
        "!",
        "ntp source Vlan623",
        "ntp server 104.253.1.1",
        "ntp server 104.253.1.2",
        "!",
        "access-list 10 permit 89.1.7.153",
        "access-list 10 remark Gestion",
        "access-list 10 permit 104.16.0.0 0.0.255.255",
        "access-list 10 permit 104.1.0.0 0.0.255.255",
        "access-list 10 permit 172.24.3.0 0.0.0.255",
        "access-list 10 permit 172.24.5.0 0.0.0.255",
        "access-list 10 permit 172.24.32.0 0.0.0.255",
        "access-list 10 permit 172.24.37.0 0.0.0.255",
        "access-list 10 permit 104.1.0.0 0.0.255.255",
        "access-list 10 permit 104.192.128.0 0.0.255.255",
        "!",
        "! -------------------------------------------------------",
        "! (Comentarios sobre troncales ignorados por esta versión)",
        "! -------------------------------------------------------",
    ])
    return lines

def _uca_extra_base_lines() -> List[str]:
    lines = [
        "aaa new-model",
        "aaa group server tacacs+ ISE_GROUP",
        " server name ISE",
        " server name ISE2",
        "aaa authentication username-prompt LOCAL_username:",
        "aaa authentication password-prompt LOCAL_password:",
        "aaa authentication login TAC-AUTH group tacacs+ local",
        "aaa authorization config-commands",
        "aaa authorization exec TAC-AUTO group tacacs+ local if-authenticated",
        "aaa authorization commands 0 default group tacacs+ local",
        "aaa authorization commands 1 default group tacacs+ local",
        "aaa authorization commands 15 default group tacacs+ local",
        "aaa accounting exec TAC-ACC start-stop group tacacs+",
        "aaa accounting commands 0 default start-stop group tacacs+",
        "aaa accounting commands 1 default start-stop group tacacs+",
        "aaa accounting commands 15 default start-stop group tacacs+",
        "aaa session-id common",
        "!",
        "service password-encryption",
        "!",
        "vtp domain UCA",
        "vtp mode transparent",
        "ip domain name aena.es",
        "!",
        "login block-for 30 attempts 10 within 60",
        "!",
        "spanning-tree mode mst",
        "spanning-tree portfast default",
        "spanning-tree portfast bpduguard default",
        "spanning-tree extend system-id",
        "",
        "spanning-tree mst configuration",
        " name UCA",
        " revision 1",
        " instance 1 vlan 141, 2205, 2207, 2209, 2211, 2213, 2215, 2217, 2219, 2221",
        " instance 1 vlan 2223, 2225, 2227, 2229",
        " instance 2 vlan 32, 110, 142, 2204, 2206, 2208, 2210, 2212, 2214, 2216",
        " instance 2 vlan 2218, 2220, 2222, 2224, 2226, 2228, 2230, 2300",
        "",
        "spanning-tree mst hello-time 1",
        "spanning-tree mst forward-time 4",
        "spanning-tree mst max-age 6",
        "!",
        "lldp run",
        "!",
        "vlan 2230",
        "name AO-GreBCNUCA",
        "!",
        "interface GigabitEthernet0/0",
        " vrf forwarding Mgmt-vrf",
        " ip address 6.6.6.6 255.255.255.252",
        " negotiation auto",
        "no shutdown",
        "!",
        "interface vlan2230",
        " ip address 10.192.129.x 255.255.255.0",
        "!",
        "ip tftp source-interface vlan2230",
        "ip ssh time-out 60",
        "ip ssh authentication-retries 2",
        "ip ssh version 2",
        "ip scp server enable",
        "!",
        "archive",
        " log config",
        "  logging enable",
        "  logging size 1000",
        "  notify syslog contenttype plaintext",
        "memory free low-watermark processor 134148",
        "!",
        "username admin privilege 15 secret 0 0BCN@ena.2024,",
        "!",
        "!",
        "enable secret 0 Aena.2025!",
        "!",
        "!",
        "ip default-gateway 10.192.129.254",
        "no ip http server",
        "ip http authentication aaa login-authentication TAC-AUTH",
        "ip http authentication aaa exec-authorization TAC-AUTO",
        "ip http secure-server",
        "ip http client source-interface Vlan2230",
        "ip tftp source-interface Vlan2230",
        "ip tftp blocksize 512",
        "ip ssh time-out 60",
        "ip ssh authentication-retries 2",
        "ip ssh version 2",
        "ip scp server enable",
        "!",
        "logging trap debugging",
        "logging host 4.9.0.135",
        "logging host 104.16.0.225",
        "logging host 104.18.220.10",
        "!",
        "tacacs server ISE",
        " address ipv4 104.16.0.210",
        " key 0 @UC@.2024!",
        "tacacs server ISE2",
        " address ipv4 104.16.0.211",
        " key 0 @UC@.2024!",
        "!",
        "snmp-server group V3groupDCOM v3 priv notify *tv.FFFFFFFF.FFFFFFFF.FFFFFFFF.FFFFFFFF7F",
        "snmp-server group V3groupDCOM v3 priv read V3bcnVIDro write V3bcnVIDrw",
        "snmp-server view V3bcnVIDBro iso included",
        "snmp-server view V3bcnVIDrw iso included",
        "snmp-server view nacview iso included",
        "snmp-server view V3bcnVIDro iso included",
        "snmp-server community V3bcnVIDro RO",
        "snmp-server community V3bcnVIDrw RW",
        "snmp-server user V3Dcom V3groupDCOM v3 auth sha #2024@En@! priv des @En@,.2025!",
        "snmp-server enable traps",
        "snmp-server host 104.16.0.225 version 2c GreBCNro",
        "snmp-server host 104.16.0.225 version 3 priv V3gesred",
        "snmp-server host 4.9.0.135 version 2c GreBCNro",
        "snmp-server host 4.9.0.135 version 3 priv V3gesred",
        "snmp-server host 104.18.220.10 version 2c GreBCNro",
        "snmp-server host 104.18.220.10 version 3 priv V3gesred",
        "!",
        "banner exec ^C",
        "Session established to $(hostname) on line $(line)",
        "^C",
        "banner incoming ^C",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! PROHIBIDO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "!!                                                                            !!",
        "!!           Queda totalmente prohibido el uso del protocolo TELNET           !!",
        "!!                   Contacte con el administrador de la red                  !!",
        "!!                                                                            !!",
        "!!           The use of the TELNET protocol is completely prohibited          !!",
        "!!                       Contact the network administrator                    !!",
        "!!                                                                            !!",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! FORBIDDEN !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "^C",
        "banner login ^C",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! ATENCION !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "!!                                                                            !!",
        "!!                          Solo personal autorizado                          !!",
        "!!                                                                            !!",
        "!!                          Authorized personal only                          !!",
        "!!                                                                            !!",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! CAUTION !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "^C",
        "banner motd ^C",
        "********************************************************************************",
        "**                                                                            **",
        "**                       AENA - Aeropuerto Barcelona                          **",
        "**                                                                            **",
        "**         Division de Tecnologias de la Informacion y Comunicaciones         **",
        "**                                                                            **",
        "**                  Esta accediendo a un sistema protegido                    **",
        "**          Si no esta autorizado cierre inmediatamente su conexion           **",
        "**                  La manipulacion no autorizada infringe                    **",
        "**             la ley 21/2003, de 7 de Julio, de Seguridad Aerea              **",
        "**                                                                            **",
        "********************************************************************************",
        "**                                                                            **",
        "**                         This is a Private System                           **",
        "**          If you are not authorized close your connection inmediatly        **",
        "**                    Unauthorized access is regulated by                     **",
        "**                   Air Security Law 21/2003, 7th of July                    **",
        "**                                                                            **",
        "********************************************************************************",
        "|                          Informacion de Acceso",
        "|                          Equipo: $(hostname)",
        "|",
        "|                       Autorizacion mediante TACACS+",
        "|",
        "^C",
        "banner prompt-timeout ^C",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! ADVERTENCIA !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "!!                                                                            !!",
        "!!                      Ha experiado el tiempo de session                     !!",
        "!!                                                                            !!",
        "!!                        Has experienced session time                        !!",
        "!!                                                                            !!",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! WARNING !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "^C",
        "banner config-save ^C",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! INFORMACION !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "!!                                                                            !!",
        "!!         Desea realizar una copia de la configuracion en ejecucion          !!",
        "!!                                                                            !!",
        "!!           You want to make a copy of the running configuration             !!",
        "!!                                                                            !!",
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! INFORMATION !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        "^C",
        "",
        "line con 0",
        " session-timeout 15",
        " exec-timeout 15 0",
        " authorization exec TAC-AUTO",
        " accounting exec TAC-ACC",
        " logging synchronous",
        " login authentication TAC-AUTH",
        " stopbits 1",
        "line vty 0 4",
        " session-timeout 15",
        " exec-timeout 15 0",
        " authorization exec TAC-AUTO",
        " accounting exec TAC-ACC",
        " logging synchronous",
        " login authentication TAC-AUTH",
        " transport preferred ssh",
        "line vty 5 15",
        " session-timeout 15",
        " exec-timeout 15 0",
        " authorization exec TAC-AUTO",
        " accounting exec TAC-ACC",
        " logging synchronous",
        " login authentication TAC-AUTH",
        " transport preferred ssh",
        "",
        "ntp source vlan2230",
        "ntp server 104.253.1.1",
        "ntp server 104.253.1.2",
        "",
        "access-list 10 permit 89.1.7.153",
        "access-list 10 remark Gestion",
        "access-list 10 permit 104.16.0.0 0.0.255.255",
        "access-list 10 permit 10.192.129.0 0.0.0.255",
        "access-list 10 permit 104.1.0.0 0.0.255.255",
        "access-list 10 permit 104.129.0.0 0.0.255.255",
        "access-list 10 permit 104.241.0.0 0.0.255.255",
        "access-list 10 permit 104.242.0.0 0.0.255.255",
        "access-list 10 permit 172.24.3.0 0.0.0.255",
        "access-list 10 permit 172.24.5.0 0.0.0.255",
        "access-list 10 permit 172.24.32.0 0.0.0.255",
        "access-list 10 permit 172.24.37.0 0.0.0.255",
        "access-list 10 permit 104.192.128.0 0.0.255.255",
        "!",
        "vlan 2204",
        " name SipaSVC4",
        "!",
        "vlan 2205",
        " name SipaSVC5",
        "!",
        "vlan 2206",
        " name SipaSVC6",
        "!",
        "vlan 2207",
        " name SipaSVC7",
        "!",
        "vlan 2208",
        " name SipaSVC8",
        "!",
        "vlan 2209",
        " name UCA10",
        "!",
        "vlan 2210",
        " name UCA11",
        "!",
        "vlan 2211",
        " name UCA12",
        "!",
        "vlan 2212",
        " name UCA13",
        "!",
        "vlan 2215",
        " name AO_UCA_SBD_T1",
        "!",
        "vlan 2216",
        " name AO-SIPAV6-T1",
        "!",
        "vlan 2217",
        " name AO-SIPAV6-T1-Gen2",
        "!",
        "vlan 2218",
        " name AO-SIPAV6-T1-Emb1",
        "!",
        "vlan 2219",
        " name AO-SIPAV6-T1-Emb2",
        "!",
        "vlan 2221",
        " name AO-SIPAV6-T1-Fac1",
        "!",
        "vlan 2222",
        " name AO-SIPAV6-T1-Fac2",
        "!",
        "vlan 2300",
        " name GESTION_SIPA",
        "!",
        "! (Comentarios sobre troncales ignorados por esta versión)",
    ]
    return lines

def _emit_base_template(
    f,
    forced_hostname: str,
    which: str,
    *,
    include_vlan_623: bool = False,
):
    if which in ("POE", "AMBAR_T"):
        base_lines = _read_template_file(AMBAR_TEMPLATE_PATH)
        base_name  = "AMBAR"
    else:
        base_lines = _read_template_file(UCA_TEMPLATE_PATH)
        base_name  = "UCA"

    f.write(f"!\n! === BASE TEMPLATE: {base_name} ===\n")
    f.write(f"hostname {forced_hostname}\n")
    for ln in base_lines:
        if BASE_HOSTNAME_RE.match(ln):
            continue
        f.write(ln + ("\n" if not ln.endswith("\n") else ""))

    if which in ("POE", "AMBAR_T"):
        extra_lines = _read_template_file(AMBAR_EXTRA_TEMPLATE_PATH)
        if not extra_lines:
            extra_lines = _ambar_extra_base_lines()
        if extra_lines:
            f.write("!\n! === CONFIG BASE AMBAR ADICIONAL ===\n")
            for ln in extra_lines:
                f.write(ln + ("\n" if not ln.endswith("\n") else ""))
        if include_vlan_623:
            f.write("!\n! === CONFIG ADICIONAL VLAN 623 ===\n")
            f.write("vlan 623\n")
            f.write(" name BCN_ACC_GESTION_AMBAR\n")
            f.write("!\n")
            f.write("interface Vlan623\n")
            f.write(" ip address 10.192.130.1XX 255.255.254.0\n")
            f.write("!\n")
    elif which == "UCA":
        extra_lines = _read_template_file(UCA_EXTRA_TEMPLATE_PATH)
        if not extra_lines:
            extra_lines = _uca_extra_base_lines()
        if extra_lines:
            f.write("!\n! === CONFIG BASE UCA ADICIONAL ===\n")
            for ln in extra_lines:
                f.write(ln + ("\n" if not ln.endswith("\n") else ""))

    f.write("!\n! === INTERFACES (clonadas del running) ===\n")

def _filter_out_sticky(lines: List[str]) -> List[str]:
    return list(lines)

def _normalize_command(cmd: str) -> str:
    return re.sub(r"\s+", " ", cmd.strip().lower()) if cmd.strip() else ""

def _ensure_security_basics(commands: List[str]) -> None:
    required = [
        " switchport port-security mac-address sticky",
        " switchport port-security",
        " spanning-tree portfast",
    ]
    existing = {
        _normalize_command(cmd)
        for cmd in commands
        if cmd and not cmd.strip().startswith("!")
    }
    for line in required:
        norm = _normalize_command(line)
        if norm and norm not in existing:
            commands.append(line)
            existing.add(norm)

def export_config_with_templates(
    rows: List[List[str]],
    out_dir: str,
    which: str,
    iface_cfgs: Dict[Tuple[str,str], List[str]],
    *,
    hostname_ambar: str,
    hostname_uca: str,
    include_vlan_623: bool = False,
) -> str:
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_map = {
        "POE":      f"config_poe_{ts}.txt",
        "AMBAR_T":  f"config_ambar_t_{ts}.txt",
        "UCA":      f"config_uca_t_{ts}.txt",
    }
    txt = os.path.join(out_dir, out_map[which])

    rows_sorted = [r for r in rows if r[-1] == which and r[0] != "LIBRE"]

    def _config_sort_key(row: List[str]) -> Tuple[str, float, str]:
        sw_name = row[5]
        iface = row[3]
        idx = _extract_if_index(iface)
        return sw_name, float(idx) if idx is not None else float("inf"), iface

    rows_sorted.sort(key=_config_sort_key)

    forced_hostname = hostname_ambar if which in ("POE", "AMBAR_T") else hostname_uca

    with open(txt, "w", encoding="utf-8") as f:
        f.write(f"!\n! Configuración generada ({which}) [SIN TRUNK]\n!\n")
        _emit_base_template(
            f,
            forced_hostname=forced_hostname,
            which=which,
            include_vlan_623=include_vlan_623,
        )
        f.write("! ------------------------------------------------------------\n")

        current_sw_new = None
        for r in rows_sorted:
            sw_act, if_act, desc_act, if_new, desc_new, sw_new, vlan, mode, tags, mac, grupo = r

            if sw_new != current_sw_new:
                current_sw_new = sw_new
                f.write(f"! Interfaces para {sw_new}\n")

            block = iface_cfgs.get((sw_act, if_act))
            f.write(f"interface {if_new}\n")

            commands: List[str] = []
            if block:
                filtered = _filter_out_sticky(block)
                has_desc = any(re.match(r"^\s*description\b", ln, re.IGNORECASE) for ln in filtered)
                if (not has_desc) and desc_new and desc_new != "N/A":
                    commands.append(f" description {desc_new}")
                for ln in filtered:
                    if re.match(r"^\s*interface\b", ln, re.IGNORECASE):
                        continue
                    commands.append(ln if ln.endswith("\n") else ln)
            else:
                commands.append(f" ! running-config no encontrado para {sw_act} {if_act}")
                if desc_new and desc_new != "N/A":
                    commands.append(f" description {desc_new}")
                if vlan and vlan.isdigit():
                    commands.append(f" switchport access vlan {vlan}")
                    commands.append(" switchport mode access")

            _ensure_security_basics(commands)

            m_voice = re.search(r"VOICE=(\d+)", tags or "")
            if m_voice:
                voice_cmd = f" switchport voice vlan {m_voice.group(1)}"
                if all(_normalize_command(cmd) != _normalize_command(voice_cmd) for cmd in commands):
                    commands.append(voice_cmd)

            for cmd in commands:
                if cmd.endswith("\n"):
                    f.write(cmd)
                else:
                    f.write(cmd + "\n")
            f.write("!\n")

        f.write("!\nend\n!\n")

    return txt

# ---------- Helpers de entrada (logs y CLI) ----------

def sanitize_path(p: str) -> str:
    p = p.strip()
    if (p.startswith('"') and p.endswith('"')) or (p.startswith("'") and p.endswith("'")):
        p = p[1:-1]
    p = unicodedata.normalize("NFC", p)
    return p.strip()

def pick_existing_log(name: str):
    def variants(base: str):
        base_nfc = unicodedata.normalize("NFC", base)
        base_nfd = unicodedata.normalize("NFD", base)
        swap_dash = base.replace("\u2013", "-").replace("\u2014", "-")
        with_dash = base.replace("-", "\u2013")
        return list(dict.fromkeys([base, base_nfc, base_nfd, swap_dash, with_dash]))

    candidates = []
    for v in variants(name):
        candidates.extend([v,
                           os.path.join("shrunActual", v),
                           os.path.join("/mnt/data", v),
                           os.path.expanduser(v)])
    for c in candidates:
        if os.path.exists(c):
            return c

    def norm_key(s: str):
        s = unicodedata.normalize("NFC", s)
        s = s.replace("\u2013", "-").replace("\u2014", "-")
        s = " ".join(s.split())
        return s.lower()

    wanted = norm_key(name)
    search_dirs = [".", "shrunActual", "/mnt/data"]
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if norm_key(fn) == wanted:
                path = os.path.join(d, fn)
                if os.path.exists(path):
                    return path

    return None

def resolve_cli_inputs(args: Iterable[str]) -> List[str]:
    resolved = []
    missing = []
    for raw in args:
        name = sanitize_path(raw)
        picked = pick_existing_log(name)
        if picked:
            resolved.append(picked)
        else:
            missing.append(raw)
    if missing:
        for raw in missing:
            print(f"  - Error: '{raw}' no existe (probé variantes, 'shrunActual/' y '/mnt/data').")
        raise SystemExit(1)
    return resolved

def prompt_yes_no(question: str, *, default: bool = False) -> bool:
    yes_values = {"si", "sí", "s", "y", "yes"}
    no_values = {"no", "n"}

    suffix = " [S/N]" if default else " [s/n]"
    prompt = f"{question.strip()}{suffix}: "

    while True:
        answer = input(prompt).strip().lower()
        if not answer:
            return default
        if answer in yes_values:
            return True
        if answer in no_values:
            return False
        print("  - Responde 'Si' o 'No' (también se aceptan S/N).")

# ---------- Main (SIN TRUNK) ----------

if __name__ == "__main__":
    include_vlan_623 = prompt_yes_no("¿Incluir configuración de gestión (VLAN 623) en AMBAR? Si/No")

    cli_args = sys.argv[1:]
    if cli_args:
        inputs = resolve_cli_inputs(cli_args)
    else:
        print("\nIntroduce, uno por línea, los nombres de los ficheros LOG (Enter en blanco para terminar):")
        inputs = []
        while True:
            raw = input("> ")
            if not raw.strip():
                break
            name = sanitize_path(raw)
            picked = pick_existing_log(name)
            if not picked:
                print(f"  - Aviso: '{raw}' no existe (probé variantes, 'shrunActual/' y '/mnt/data').")
                print("    Vuelve a intentarlo o deja en blanco para terminar.")
                continue
            inputs.append(picked)

    if not inputs:
        print("No se introdujeron ficheros. Saliendo.")
        raise SystemExit(0)

    wifi_items, voip_items, uca_items, ambar_other_items, all_iface_cfgs = build_inventory_from_logs(inputs)

    poe_rows, ambar_overflow = make_mapping_poe(wifi_items, voip_items, ambar_other_items)
    ambar_t_rows = make_mapping_ambar_t(ambar_overflow) if ambar_overflow else []
    uca_rows = make_mapping_uca(uca_items)

    print("\nIntroduce los hostnames base para las plantillas:")
    hostname_ambar = input("Hostname para switches AMBAR (POE y AMBAR_T): ").strip() or "AMBAR-SW"
    hostname_uca   = input("Hostname para switches UCA: ").strip() or "UCA-SW"

    _sort_group_rows(poe_rows)
    if ambar_t_rows:
        _sort_group_rows(ambar_t_rows)
    _sort_group_rows(uca_rows)

    wifi_switch_names = {
        str(info.get("sw_name"))
        for info in POE_MEMBER_METADATA.values()
        if info.get("type") == "wifi"
    }
    wifi_poe_rows: List[List[str]] = []
    non_wifi_poe_rows: List[List[str]] = []
    for row in poe_rows:
        if row[-1] != "POE":
            continue
        if row[5] in wifi_switch_names:
            wifi_poe_rows.append(row)
        else:
            non_wifi_poe_rows.append(row)

    all_rows = wifi_poe_rows + non_wifi_poe_rows + ambar_t_rows + uca_rows

    out_dir = "Amigrar"
    os.makedirs(out_dir, exist_ok=True)

    xlsx_path = export_excel(all_rows, out_dir)

    cfg_poe   = export_config_with_templates(
        all_rows, out_dir, which="POE", iface_cfgs=all_iface_cfgs,
        hostname_ambar=hostname_ambar, hostname_uca=hostname_uca,
        include_vlan_623=include_vlan_623,
    )
    cfg_amb_t = export_config_with_templates(
        all_rows, out_dir, which="AMBAR_T", iface_cfgs=all_iface_cfgs,
        hostname_ambar=hostname_ambar, hostname_uca=hostname_uca,
        include_vlan_623=include_vlan_623,
    ) if ambar_t_rows else None
    cfg_uca_t = export_config_with_templates(
        all_rows, out_dir, which="UCA", iface_cfgs=all_iface_cfgs,
        hostname_ambar=hostname_ambar, hostname_uca=hostname_uca,
        include_vlan_623=include_vlan_623,
    ) if uca_rows else None

    print("\n¡Hecho (sin trunks)!")
    print(f"  Excel: {os.path.abspath(xlsx_path)}")
    print(f"  Config POE:     {os.path.abspath(cfg_poe)}")
    if cfg_amb_t:
        print(f"  Config AMBAR-T: {os.path.abspath(cfg_amb_t)}")
    if cfg_uca_t:
        print(f"  Config UCA-T:   {os.path.abspath(cfg_uca_t)}")
