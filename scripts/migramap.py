#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# HM/Nov/25/v15.1 - CORRECCIÓN: Añadido el Tipo de Migración (TipoX) al nombre de los 3 ficheros de salida.

import re
import pandas as pd
import sys
from collections import defaultdict
import xlsxwriter
from datetime import datetime

# ==============================================================================
# 1. VARIABLES GLOBALES Y DEFINICIÓN DE CLASES
# ==============================================================================

# Variables de configuración global
GLOBAL_PORT_MAP = {}
C9300_PORT_OCCUPANCY = {}  # Puertos en C9300 ocupados por mapeo scsf 1:1 o secuencial scsf
HUECO_CANDIDATES = defaultdict(list)  # { unit_id: [port_name, port_name, ...] } - Slots disponibles para relleno por puertos Normales

# Modelos disponibles para el menú de selección
AVAILABLE_C9300_MODELS = [
    'C9300-48UXM',  # PoE
    'C9300-48P',    # PoE
    'C9300-48T',
    'C9300-24P',    # PoE
    'C9300-24T',
    'C9200CX-8P'
]

# Unidades de destino que soportan PoE.
POE_MODELS = ['C9300-48UXM', 'C9300-48P', 'C9300-24P', 'C9200CX-8P']

PORT_MAPPING_RULES = {
    'C9300-48UXM': {'Tw': (1, 36), 'Te': (37, 48)},
    'C9300-48P':   {'Gi': (1, 48)},
    'C9300-48T':   {'Gi': (1, 48)},
    'C9300-24P':   {'Gi': (1, 24)},
    'C9300-24T':   {'Gi': (1, 24)},
    'C9200CX-8P':  {'Gi': (1, 8)}
}


class PortCounter:
    """Gestiona el siguiente puerto disponible en una unidad de stack C9300, empezando por el puerto 1."""
    def __init__(self, model):
        self.model = model
        self.rules = PORT_MAPPING_RULES.get(model, {})
        self.port_groups = {
            prefix: {'current': start, 'limit': end}
            for prefix, (start, end) in self.rules.items()
        }
        self.total_migrated = 0

    def get_next_available_port(self, stack_unit=1, consume_port=True):
        """Retorna el siguiente puerto disponible con el formato [Prefijo][Stack]/0/[Número],
        saltando puertos ocupados por mapeo scsf 1:1.
        """
        global C9300_PORT_OCCUPANCY

        # Buscar el siguiente puerto NO OCUPADO por un scsf 1:1
        for prefix, data in self.port_groups.items():
            current_port_num = data['current']
            limit = data['limit']

            while current_port_num <= limit:
                potential_port_name = f"{prefix}{stack_unit}/0/{current_port_num}"

                # Si el puerto potencial NO está ocupado por CUALQUIER mapeo (scsf o Normal 1:1), es el slot libre.
                if potential_port_name not in C9300_PORT_OCCUPANCY:
                    if consume_port:
                        self.port_groups[prefix]['current'] = current_port_num + 1
                        self.total_migrated += 1
                    return potential_port_name

                # Si está ocupado, saltar el contador
                current_port_num += 1

            # Si se llega al límite, asegurar que el contador se detiene
            if current_port_num > limit and data['current'] <= limit:
                self.port_groups[prefix]['current'] = limit + 1

        return None


class Switch:
    """Clase para manejar y parsear ficheros de configuración y tablas MAC."""
    def __init__(self, name, model, config_file_path=""):
        self.name = name
        self.model = model
        self.config_path = config_file_path
        self.config_raw = self._load_config()
        self.interfaces = {}
        self.module_model = None
        self.mac_table = pd.DataFrame()
        self.vlans = {}  # atributo para almacenar {ID: Name}
        self.poe_usage = {}  # {port_name: power_watts}

    def _load_config(self):
        if not self.config_path:
            return ""
        try:
            if self.config_path.startswith('uploaded:'):
                return "Configuración simulada"
            else:
                return open(self.config_path, 'r', encoding='utf-8').read()
        except FileNotFoundError:
            print(f"ERROR: Fichero de configuración/MAC para {self.name} no encontrado en '{self.config_path}'.")
            return ""
        except Exception as e:
            print(f"ERROR al cargar el fichero para {self.name}: {e}")
            return ""

    def parse_interfaces(self):
        interface_blocks = re.findall(r"(interface.*?)(?=^interface|^!$)",
                                      self.config_raw, re.MULTILINE | re.DOTALL)

        for block in interface_blocks:
            name_match = re.search(r"interface\s+(\S+)", block)
            if name_match:
                int_name_raw = name_match.group(1).strip()
                int_name = standardize_port_name(int_name_raw)

                description_match = re.search(r"description\s+(.*)", block, re.IGNORECASE)
                description = description_match.group(1).strip() if description_match else ""

                vlan = '1'
                mode = 'default'

                if re.search(r"switchport\s+mode\s+trunk", block, re.IGNORECASE):
                    mode = 'trunk'
                    vlan = 'N/A'
                elif re.search(r"switchport\s+mode\s+access", block, re.IGNORECASE):
                    mode = 'access'
                    vlan_match = re.search(r"switchport\s+access\s+vlan\s+(\d+)", block, re.IGNORECASE)
                    if vlan_match:
                        vlan = vlan_match.group(1)

                if not re.search(r"switchport", block, re.IGNORECASE):
                    mode = 'L3'
                    vlan = 'N/A'

                has_speed_10 = bool(re.search(r"^\s*speed\s+10\b", block, re.MULTILINE | re.IGNORECASE))
                self.interfaces[int_name] = {
                    'config': block.strip(),
                    'description': description,
                    'vlan': vlan,
                    'mode': mode,
                    'poe_watts': 0.0,  # Valor por defecto
                    'speed_10': has_speed_10
                }

    def parse_poe_usage(self):
        in_table = False
        for raw_line in self.config_raw.splitlines():
            line = raw_line.strip()

            # Detecta inicio tabla
            if line.lower().startswith("interface admin") and "power" in line.lower():
                in_table = True
                continue

            if not in_table:
                continue

            # Fin tabla: prompt, separadores, línea vacía, etc.
            if not line or line.endswith("#") or line.startswith("----") or line.startswith("T4-"):
                continue

            parts = line.split()
            # Esperado: Interface Admin Oper Power ...
            if len(parts) < 4:
                continue

            port_raw = parts[0]
            power_str = parts[3]

            try:
                power_watts = float(power_str)
            except ValueError:
                continue

            port_name = standardize_port_name(port_raw)

            # Guarda siempre (aunque 0.0 si quieres)
            if power_watts > 0.0:
                self.poe_usage[port_name] = power_watts
                if port_name in self.interfaces:
                    self.interfaces[port_name]['poe_watts'] = power_watts

    def parse_vlans(self):
        vlans_data = {}
        vlan_block_pattern = re.compile(
            r'^vlan\s+(\d+)[ \t\r\f\v]*\n((?:[ \t]+.*\n)*)^[ \t]*!\s*$',
            re.MULTILINE | re.IGNORECASE
        )
        for match in vlan_block_pattern.finditer(self.config_raw):
            vlan_id = match.group(1).strip()
            vlan_block_body = match.group(2)
            name_match = re.search(r'^\s*name\s+(.+)$', vlan_block_body, re.MULTILINE | re.IGNORECASE)
            if name_match:
                vlan_name = name_match.group(1).strip()
            else:
                vlan_name = f"VLAN_{vlan_id}"
            if vlan_id != '1':
                vlans_data[vlan_id] = vlan_name
        self.vlans = vlans_data

    def parse_mac_table(self):
        """Busca los bloques 'show mac address-table' y los parsea, manejando múltiples formatos."""
        mac_table_match = re.search(
            r"(\s*Vlan\s+Mac Address\s+Type\s+Ports.*?)(?=(?:^!|\n\n|\Z))|(\s*vlan\s+mac address\s+type\s+protocols\s+port.*?)(?=(?:^!|\n\n|\Z))",
            self.config_raw, re.MULTILINE | re.DOTALL | re.IGNORECASE
        )

        macs_data = []

        if mac_table_match:
            mac_block_raw = mac_table_match.group(1) or mac_table_match.group(2)

            unicast_match = re.search(
                r"Unicast Entries\s*.*?-------+\+-------+\+.*?vlan\s+mac address\s+type\s+protocols\s+port\s*(.*?)\s*Multicast Entries",
                mac_block_raw, re.MULTILINE | re.DOTALL | re.IGNORECASE
            )

            if unicast_match:
                mac_lines = unicast_match.group(1).split('\n')
            else:
                mac_lines = mac_block_raw.split('\n')

            for line in mac_lines:
                line = line.strip()

                match = re.match(r"^\s*(\d+)\s+([0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4})\s+\S+\s+(\S+)$",
                                 line, re.IGNORECASE)

                if not match:
                    match = re.match(r"^\s*(\d+)\s+([0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4})\s+\S+\s+.*?\s+(\S+)$",
                                     line, re.IGNORECASE)

                if match:
                    groups = match.groups()
                    vlan = groups[0]
                    mac = groups[1]
                    port_raw = groups[-1]

                    port_raw_lower = port_raw.lower()
                    if port_raw_lower in ('cpu', 'switch', 'router') or port_raw_lower.startswith('po'):
                        continue

                    port = standardize_port_name(port_raw)
                    macs_data.append({'VLAN': vlan, 'MAC': mac, 'Old_Port': port})

            self.mac_table = pd.DataFrame(macs_data)
        else:
            print("Advertencia: No se pudo encontrar el bloque 'show mac address-table'. La tabla MAC estará vacía.")
            self.mac_table = pd.DataFrame(columns=['VLAN', 'MAC', 'Old_Port'])


# ==============================================================================
# 2. FUNCIONES DE UTILIDAD Y LÓGICA CORE
# ==============================================================================

def get_numeric_sort_key(port_name):
    """Extrae las partes numéricas de un nombre de puerto para ordenación (Prefijo, Unidad, Slot, Puerto)."""
    if not isinstance(port_name, str):
        return (0, 0, 0, 0)

    temp_name = port_name
    temp_name = re.sub(r'GigabitEthernet', 'Gi', temp_name, flags=re.IGNORECASE)
    temp_name = re.sub(r'FastEthernet', 'Fa', temp_name, flags=re.IGNORECASE)
    temp_name = re.sub(r'TenGigabitEthernet', 'Te', temp_name, flags=re.IGNORECASE)

    all_numbers = re.findall(r'\d+', temp_name)

    unit = int(all_numbers[0]) if len(all_numbers) > 0 else 0
    slot = int(all_numbers[1]) if len(all_numbers) > 1 and len(all_numbers) == 3 else 0
    port = int(all_numbers[-1]) if len(all_numbers) > 0 else 0

    match = re.match(r"([A-Za-z]+)", temp_name)
    prefix_sort = match.group(1).lower() if match else temp_name.lower()

    return (prefix_sort, unit, slot, port)


def get_port_parts(port_name):
    """Extrae el prefijo, unidad y número de puerto."""
    standardized_name = standardize_port_name(port_name)

    prefix_match = re.match(r"([A-Za-z]+)", standardized_name)
    prefix = prefix_match.group(1) if prefix_match else None

    all_numbers = re.findall(r'\d+', standardized_name)

    if len(all_numbers) < 2:
        return None, None, None

    unit = int(all_numbers[0])
    port = int(all_numbers[-1])

    if prefix and unit > 0 and port > 0:
        return prefix, unit, port

    return None, None, None


def standardize_port_name(port_name):
    """Estandariza los nombres de interfaz a un formato corto (e.g., Gi, Te, Fa) para mapeo."""
    if not isinstance(port_name, str):
        return port_name

    port_name = port_name.strip()
    port_name = re.sub(r'GigabitEthernet', 'Gi', port_name, flags=re.IGNORECASE)
    port_name = re.sub(r'FastEthernet', 'Fa', port_name, flags=re.IGNORECASE)
    port_name = re.sub(r'TenGigabitEthernet', 'Te', port_name, flags=re.IGNORECASE)
    port_name = re.sub(r'FortyGigabitEthernet', 'Fo', port_name, flags=re.IGNORECASE)
    port_name = re.sub(r'HundredGigabitEthernet', 'Hu', port_name, flags=re.IGNORECASE)

    return port_name


def is_uxm_model(model_name):
    """Indica si el modelo es C9300-48UXM (no admite puertos a speed 10)."""
    return model_name == 'C9300-48UXM'


def build_destination_ports(stack_models):
    """Genera la lista ordenada de puertos destino por unidad y rango."""
    ordered_ports = []
    for unit_id in sorted(stack_models.keys()):
        model = stack_models[unit_id]
        rules = PORT_MAPPING_RULES.get(model, {})
        for prefix, (start, end) in rules.items():
            for port_num in range(start, end + 1):
                ordered_ports.append(f"{prefix}{unit_id}/0/{port_num}")
    return ordered_ports


def is_excluded(interface_config):
    """Determina si un puerto está excluido por configuración (VLAN 1 o shutdown)."""
    if re.search(r"^\s*shutdown\s*$", interface_config, re.MULTILINE | re.IGNORECASE):
        return True
    if re.search(r"switchport\s+access\s+vlan\s+1\b", interface_config, re.IGNORECASE):
        return True
    return False


def is_interface_to_exclude_by_type(int_name):
    """Excluye interfaces VLAN y Port-channel por tipo."""
    int_name_lower = int_name.lower()
    if int_name_lower.startswith('vlan') or int_name_lower.startswith('vla'):
        return True
    if int_name_lower.startswith('port-channel') or int_name_lower.startswith('po'):
        return True
    return False


def clean_config(config_block):
    """
    Limpia comandos obsoletos/irrelevantes para el C9300, asegurando que
    los comandos switchport y la descripción se preserven.
    """
    cleaned_lines = []
    EXCLUDE_PATTERNS = [
        r"logging\s+rate-limit", r"mls\s+qos", r"no\s+ip\s+address",
        r"ip\s+address", r"shutdown", r"vlan\s+1", r"interface"
    ]

    for line in config_block.split('\n'):
        line = line.strip()
        if not line:
            continue
        if any(re.search(p, line, re.IGNORECASE) for p in EXCLUDE_PATTERNS):
            continue
        cleaned_lines.append(line)

    return '\n'.join(cleaned_lines)


def load_security_vlans(file_path='scsf_T123.txt'):
    """Carga la lista de VLANs de seguridad desde scsf_T123.txt."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read().replace('\n', ',')
            return {v.strip() for v in content.split(',') if v.strip().isdigit()}
    except FileNotFoundError:
        print(f"Advertencia: No se encontró el archivo de VLANs de Seguridad en {file_path}. scsf operará sin segregación.")
        return set()


def config_has_scsf_vlan(config, scsf_vlans):
    """Verifica si la configuración de un puerto tiene una VLAN de Seguridad (solo en modo access)."""
    if not scsf_vlans:
        return False
    if re.search(r"switchport\s+mode\s+access", config, re.IGNORECASE):
        for vlan in scsf_vlans:
            if re.search(rf"switchport\s+access\s+vlan\s+{vlan}\b", config, re.IGNORECASE):
                return True
    return False


def register_non_migrated(sw_name, old_port, data, status_suffix=''):
    """Función auxiliar para registrar puertos L3/Trunk como no migrados, capturando la IP si es L3/Trunk o N7K."""
    global GLOBAL_PORT_MAP

    status = 'NO_MIGRADO'
    if status_suffix:
        status = status_suffix

    new_port_text = f"NO MIGRADO ({data['mode']})"

    ip_address = None

    is_l3_candidate = (
        data.get('mode') == 'L3' or
        data.get('mode') == 'trunk' or
        'N7K' in data.get('description', '').upper()
    )

    if is_l3_candidate:
        ip_match = re.search(
            r"^\s*ip\s+address\s+(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s+(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}).*$",
            data['config'],
            re.MULTILINE | re.IGNORECASE
        )
        if ip_match:
            ip_address = f"{ip_match.group(1)} {ip_match.group(2)}"

    GLOBAL_PORT_MAP[(sw_name, old_port)] = {
        'new_sw': 'N/A',
        'new_port': new_port_text,
        'old_description': data.get('description', 'N/A'),
        'old_vlan': data.get('vlan', 'N/A'),
        'old_mode': data.get('mode', 'N/A'),
        'status': status,
        'ip_address': ip_address
    }


def create_destination_config_entry(sw_name, old_port, new_port, config_data, new_sw_name, status_suffix, dest_stack_obj):
    """Crea la entrada de configuración y la registra en el mapa global, RECONSTRUYENDO los comandos esenciales."""
    global GLOBAL_PORT_MAP
    global C9300_PORT_OCCUPANCY

    full_cleaned_config = clean_config(config_data['config'])

    old_mode = config_data.get('mode', 'default').lower()
    old_vlan = config_data.get('vlan', '1')

    new_config_lines = []

    if old_mode == 'access':
        new_config_lines.append(" switchport mode access")
        if old_vlan != 'N/A' and old_vlan != 'default':
            new_config_lines.append(f" switchport access vlan {old_vlan}")

    elif old_mode == 'trunk':
        new_config_lines.append(" switchport mode trunk")

    for line in full_cleaned_config.split('\n'):
        line = line.strip()
        if not line:
            continue

        if re.search(r"switchport\s+mode\s+(access|trunk)|switchport\s+access\s+vlan", line, re.IGNORECASE):
            continue

        new_config_lines.append(f" {line}")

    new_config_block = (
        f"interface {new_port}\n"
        + '\n'.join(new_config_lines)
    )

    dest_stack_obj.interfaces[new_port] = {'config': new_config_block}

    C9300_PORT_OCCUPANCY[new_port] = (sw_name, old_port)

    GLOBAL_PORT_MAP[(sw_name, old_port)] = {
        'new_sw': new_sw_name,
        'new_port': new_port,
        'old_description': config_data['description'],
        'old_vlan': config_data['vlan'],
        'old_mode': config_data['mode'],
        'status': 'MIGRADO' + status_suffix
    }


def pre_map_scsf_ports(migratable_interfaces, dest_stack_obj, scsf_units, scsf_vlans, stack_models):
    """
    FASE PREVIA: Mapea todos los puertos scsf al 1:1 estricto (GiX/0/Y -> PrefijoX/0/Y)
    donde X es una unidad scsf. Esto fija los puertos scsf.
    """
    print("\n  >> FASE PREVIA: Mapeando puertos scsf (GiX/0/Y -> PrefijoX/0/Y) en Unidades scsf...")

    for item in migratable_interfaces:
        sw_name = item['sw_name']
        old_port = item['old_port']
        data = item['data']

        if config_has_scsf_vlan(data['config'], scsf_vlans):
            prefix, unit_orig, port_num_orig = get_port_parts(old_port)

            if unit_orig in scsf_units:
                model_dest = stack_models.get(unit_orig, dest_stack_obj.model)
                rules_dest = PORT_MAPPING_RULES.get(model_dest, {})
                if data.get('speed_10') and is_uxm_model(model_dest):
                    continue

                prefix_dest = None
                for prefix_check, (start, end) in rules_dest.items():
                    if start <= port_num_orig <= end:
                        prefix_dest = prefix_check
                        break

                if prefix_dest is None:
                    register_non_migrated(sw_name, old_port, data, 'NO_MIGRADO_SIN_REGLA_1_1')
                    continue

                potential_port = f"{prefix_dest}{unit_orig}/0/{port_num_orig}"

                if potential_port not in C9300_PORT_OCCUPANCY:
                    new_port = potential_port

                    create_destination_config_entry(
                        sw_name, old_port, new_port, data, dest_stack_obj.name,
                        "_scsf_MANTENIDO_1_1", dest_stack_obj
                    )
                else:
                    register_non_migrated(sw_name, old_port, data, 'NO_MIGRADO_COLISION_scsf')
            else:
                register_non_migrated(sw_name, old_port, data, 'PENDIENTE_scsf_SECUENCIAL')

    return [
        item for item in migratable_interfaces
        if (item['sw_name'], item['old_port']) not in GLOBAL_PORT_MAP
        or GLOBAL_PORT_MAP[(item['sw_name'], item['old_port'])]['status'] == 'PENDIENTE_scsf_SECUENCIAL'
        or not config_has_scsf_vlan(item['data']['config'], scsf_vlans)
    ]


def process_migratable_interfaces(ports_to_process, dest_stack_obj, normal_counters, scsf_counters,
                                  normal_units, scsf_vlans, scsf_units, stack_models, source_switches):
    """
    Procesa la lista ordenada de interfaces.
    Aplica las prioridades: scsf Secuencial > Normal 1:1 > Normal Secuencial (PoE/3800/Huecos)
    """
    global C9300_PORT_OCCUPANCY
    global HUECO_CANDIDATES
    global POE_MODELS

    print("\n  >> FASE PRINCIPAL: Procesando secuencia de puertos (NO scsf 1:1, scsf Secuenciales, Relleno de Huecos)...")

    for new_port, (sw_name, old_port) in C9300_PORT_OCCUPANCY.items():
        if GLOBAL_PORT_MAP.get((sw_name, old_port), {}).get('status') == 'MIGRADO_scsf_MANTENIDO_1_1':
            prefix, unit_dest, port_num_dest = get_port_parts(new_port)

            if unit_dest not in normal_units:
                continue

            counter = normal_counters.get(unit_dest)
            if counter and counter.port_groups.get(prefix):
                current_normal_port_num = counter.port_groups[prefix]['current']

                if port_num_dest >= current_normal_port_num:

                    for i in range(current_normal_port_num, port_num_dest):
                        hueco_port = f"{prefix}{unit_dest}/0/{i}"
                        if hueco_port not in C9300_PORT_OCCUPANCY:
                            HUECO_CANDIDATES[unit_dest].append(hueco_port)

                    counter.port_groups[prefix]['current'] = port_num_dest + 1

    scsf_segregation_active = bool(scsf_units)

    scsf_ports_secuencial = []
    normal_ports = []

    for item in ports_to_process:
        mapping_key = (item['sw_name'], item['old_port'])

        if GLOBAL_PORT_MAP.get(mapping_key) and not GLOBAL_PORT_MAP[mapping_key]['status'].startswith('PENDIENTE'):
            continue

        is_scsf_port = scsf_segregation_active and config_has_scsf_vlan(item['data']['config'], scsf_vlans)

        if is_scsf_port:
            scsf_ports_secuencial.append(item)
        else:
            normal_ports.append(item)

    for item in scsf_ports_secuencial:
        sw_name = item['sw_name']
        old_port = item['old_port']
        data = item['data']

        new_port_assigned = False
        scsf_unit_ids = sorted(scsf_counters.keys())

        for unit_id in scsf_unit_ids:
            if data.get('speed_10') and is_uxm_model(stack_models.get(unit_id)):
                continue
            counter = scsf_counters.get(unit_id)
            if not counter:
                continue

            new_port = counter.get_next_available_port(stack_unit=unit_id, consume_port=True)
            if new_port:
                create_destination_config_entry(
                    sw_name, old_port, new_port, data, dest_stack_obj.name,
                    "_scsf_SECUENCIAL", dest_stack_obj
                )
                new_port_assigned = True
                break

        if not new_port_assigned:
            register_non_migrated(sw_name, old_port, data, 'NO_MIGRADO_STACK_scsf_LLENO')

    ports_for_flexible_mapping = []

    for item in normal_ports:
        sw_name = item['sw_name']
        old_port = item['old_port']
        data = item['data']

        prefix_orig, unit_orig, port_num_orig = get_port_parts(old_port)

        if not port_num_orig:
            ports_for_flexible_mapping.append(item)
            continue

        model_dest = stack_models.get(unit_orig) or dest_stack_obj.model
        rules_dest = PORT_MAPPING_RULES.get(model_dest, {})

        prefix_dest = next((p for p, (s, e) in rules_dest.items() if s <= port_num_orig <= e), None)

        is_poe_port = data.get('poe_watts', 0.0) > 0.0
        status_prefix = "_POE" if is_poe_port else "_NORMAL"

        if prefix_dest:
            potential_1_1_port = f"{prefix_dest}{unit_orig}/0/{port_num_orig}"
        else:
            potential_1_1_port = "UNIDAD_SIN_PREFIJO"

        is_port_occupied = (potential_1_1_port in C9300_PORT_OCCUPANCY)
        is_poe_unit = model_dest in POE_MODELS
        should_skip_1_1 = is_poe_port and not is_poe_unit
        should_skip_1_1 = should_skip_1_1 or (data.get('speed_10') and is_uxm_model(model_dest))

        if not should_skip_1_1 and not is_port_occupied and potential_1_1_port != "UNIDAD_SIN_PREFIJO":
            new_port = potential_1_1_port
            status_suffix = f"{status_prefix}_MANTENIDO_1_1"

            if unit_orig in HUECO_CANDIDATES and new_port in HUECO_CANDIDATES[unit_orig]:
                HUECO_CANDIDATES[unit_orig].remove(new_port)
                status_suffix = f"{status_prefix}_MANTENIDO_1_1_EN_HUECO"

            create_destination_config_entry(
                sw_name, old_port, new_port, data, dest_stack_obj.name,
                status_suffix, dest_stack_obj
            )
        else:
            ports_for_flexible_mapping.append(item)

    normal_ports_poe = []
    normal_ports_3800 = []
    normal_ports_rest = []

    for item in ports_for_flexible_mapping:
        sw_name = item['sw_name']
        sw_obj = next(sw for sw in source_switches.values() if sw.name == sw_name)
        data = item['data']

        is_poe_port = data.get('poe_watts', 0.0) > 0.0
        is_3800_port = sw_obj.model == 'C3800'

        if is_poe_port:
            normal_ports_poe.append(item)
        elif is_3800_port:
            normal_ports_3800.append(item)
        else:
            normal_ports_rest.append(item)

    final_flexible_order = normal_ports_poe + normal_ports_3800 + normal_ports_rest

    for item in final_flexible_order:
        sw_name = item['sw_name']
        old_port = item['old_port']
        data = item['data']
        sw_obj = next(sw for sw in source_switches.values() if sw.name == sw_name)

        is_poe_port = data.get('poe_watts', 0.0) > 0.0
        is_3800_port = sw_obj.model == 'C3800'

        new_port = None
        status_suffix = None
        hueco_filled = False

        best_hueco_unit_id = None

        for unit_id in sorted(normal_units):
            unit_model = stack_models.get(unit_id)
            is_poe_unit = unit_model in POE_MODELS

            if data.get('speed_10') and is_uxm_model(unit_model):
                continue

            if HUECO_CANDIDATES.get(unit_id):
                if is_poe_port and is_poe_unit:
                    best_hueco_unit_id = unit_id
                    break
                elif best_hueco_unit_id is None:
                    best_hueco_unit_id = unit_id

        if best_hueco_unit_id:
            new_port = HUECO_CANDIDATES[best_hueco_unit_id].pop(0)
            hueco_filled = True

        if not new_port:

            normal_unit_ids = sorted(normal_units)

            if is_poe_port:
                for unit_id in normal_unit_ids:
                    if stack_models.get(unit_id) in POE_MODELS:
                        if data.get('speed_10') and is_uxm_model(stack_models.get(unit_id)):
                            continue
                        counter = normal_counters.get(unit_id)
                        if not counter:
                            continue
                        new_port = counter.get_next_available_port(stack_unit=unit_id, consume_port=True)
                        if new_port:
                            break

            if not new_port:
                for unit_id in normal_unit_ids:
                    if data.get('speed_10') and is_uxm_model(stack_models.get(unit_id)):
                        continue
                    counter = normal_counters.get(unit_id)
                    if not counter:
                        continue
                    new_port = counter.get_next_available_port(stack_unit=unit_id, consume_port=True)
                    if new_port:
                        break

        if new_port:
            status_prefix = "_NORMAL"
            if is_poe_port:
                status_prefix = "_POE"
            if is_3800_port:
                status_prefix = "_3800"

            status_suffix = f"{status_prefix}_EN_HUECO" if hueco_filled else f"{status_prefix}_SECUENCIAL"

            create_destination_config_entry(
                sw_name, old_port, new_port, data, dest_stack_obj.name,
                status_suffix, dest_stack_obj
            )
        else:
            print(f"    - ADVERTENCIA: Stack Normal Lleno. Puerto {sw_name}/{old_port} NO MIGRADO.")
            register_non_migrated(sw_name, old_port, data, 'NO_MIGRADO_STACK_NORMAL_LLENO')


def map_trunk_ports_interactively(global_port_map, dest_hostname):
    """
    Permite al usuario mapear interactivamente los interfaces trunk 'NO MIGRADOS'.
    Actualiza el GLOBAL_PORT_MAP con el nuevo puerto y el estado MIGRADO_MANUAL_TRUNK.
    """
    print("\n" + "="*80)
    print(">>> MODO INTERACTIVO: Mapeo Manual de Interfaces TRUNK NO MIGRADOS RMS <<<")
    print("="*80)

    trunk_keys_to_map = []
    for key, data in list(global_port_map.items()):
        if data.get('old_mode') in ['trunk', 'L3'] and data.get('status').startswith('NO_MIGRADO'):
            trunk_keys_to_map.append(key)

    if not trunk_keys_to_map:
        print("No se encontraron interfaces TRUNK/L3 NO MIGRADOS pendientes de mapeo manual.")
        return

    print(f"Se encontraron {len(trunk_keys_to_map)} interfaces trunk/L3 pendientes de mapeo.")

    for i, key in enumerate(trunk_keys_to_map):
        sw_name, old_port = key
        data = global_port_map[key]

        while True:
            print("-" * 50)
            print(f"[{i + 1}/{len(trunk_keys_to_map)}] Mapeo TRUNK/L3:")
            print(f"  > Origen: {sw_name} - {old_port}")
            print(f"  > Descripción: {data.get('old_description', 'N/A')}")

            ip_info = data.get('ip_address')
            if ip_info:
                print(f"  > IP/Máscara: {ip_info}")

            new_port_input = input(
                f"Observa config Interface de origen {old_port} y traslada a {dest_hostname} "
                f"basándote en la regla: para UPLINKS usa Twe1/1/2 o Te2/1/8 "
                f"para DOWNLINKS usa por ejemplo Gi1/0/48 o Te1/0/48 "
                f"(con 's' saltas, con 'q' finalizas.) -->"
            ).strip()

            if new_port_input.lower() == 'q':
                print("\nModo interactivo finalizado por el usuario. Los restantes permanecen como NO_MIGRADO.")
                return

            if new_port_input.lower() == 's':
                print(f"Interface {old_port} saltada. Permanece como NO_MIGRADO.")
                break

            if not new_port_input:
                print("El puerto no puede estar vacío. Intenta de nuevo.")
                continue

            global_port_map[key]['new_sw'] = dest_hostname
            global_port_map[key]['new_port'] = standardize_port_name(new_port_input)
            global_port_map[key]['status'] = 'MIGRADO_MANUAL_TRUNK'
            print(f"Mapeo registrado: {old_port} -> {global_port_map[key]['new_port']}")

            break

    print("\n>>> Todos los interfaces trunk/L3 pendientes han sido procesados. <<<")
    print("="*80)


# ==============================================================================
# 3. LÓGICA DE MIGRACIÓN POR TIPOLOGÍA
# ==============================================================================

def collect_and_sort_interfaces(source_switches, migration_type):
    """Recopila y ordena TODAS las interfaces de origen que deben migrarse (incluye scsf y Normal)."""
    migratable_interfaces = []

    switches_to_process = list(source_switches.keys())

    for sw_name in switches_to_process:
        sw_obj = source_switches[sw_name]
        for old_port, data in sw_obj.interfaces.items():

            if is_interface_to_exclude_by_type(old_port):
                continue

            if is_excluded(data['config']):
                continue

            if data['mode'] in ['L3', 'trunk']:
                register_non_migrated(sw_name, old_port, data)
                continue

            migratable_interfaces.append({
                'sw_name': sw_name,
                'old_port': old_port,
                'data': data,
                'sort_key': get_numeric_sort_key(old_port)
            })

    migratable_interfaces.sort(key=lambda x: (x['sw_name'], x['sort_key']))

    return migratable_interfaces


def execute_migration(migratable_interfaces, dest_stack_obj, normal_counters, scsf_counters,
                      normal_units, scsf_vlans, scsf_units, stack_models, source_switches):
    """Añadida la variable source_switches al argumento."""
    if not scsf_units or not scsf_vlans:
        print("  >> Ejecutando migración en modo NORMAL (sin segregación scsf)...")
        ports_to_process = migratable_interfaces
    else:
        ports_to_process = pre_map_scsf_ports(migratable_interfaces, dest_stack_obj, scsf_units, scsf_vlans, stack_models)

    process_migratable_interfaces(ports_to_process, dest_stack_obj, normal_counters, scsf_counters,
                                  normal_units, scsf_vlans, scsf_units, stack_models, source_switches)


def process_video_migration(migratable_interfaces, dest_stack_obj, stack_models):
    """
    Procesa interfaces para la tipología de Video (1:1 estricto).
    No hay scsf, secuenciales ni relleno de huecos. Solo 1:1 o NO MIGRADO.
    Los puertos NO_MIGRADO (L3/Trunk) ya han sido excluidos.
    """
    global C9300_PORT_OCCUPANCY

    print("\n  >> FASE: Procesando puertos de Video (1:1 estricto)...")

    for item in migratable_interfaces:
        sw_name = item['sw_name']
        old_port = item['old_port']
        data = item['data']

        prefix, unit_orig, port_num_orig = get_port_parts(old_port)

        if not port_num_orig:
            register_non_migrated(sw_name, old_port, data, 'NO_MIGRADO_FALLO_PARSE_VIDEO')
            continue

        model_dest = stack_models.get(unit_orig)
        if model_dest is None:
            model_dest = dest_stack_obj.model

        rules_dest = PORT_MAPPING_RULES.get(model_dest, {})
        prefix_dest = None

        if data.get('speed_10') and is_uxm_model(model_dest):
            register_non_migrated(sw_name, old_port, data, 'NO_MIGRADO_SPEED10_UXM')
            continue

        for prefix_check, (start, end) in rules_dest.items():
            if start <= port_num_orig <= end:
                prefix_dest = prefix_check
                break

        potential_1_1_port = None
        if prefix_dest:
            potential_1_1_port = f"{prefix_dest}{unit_orig}/0/{port_num_orig}"

        if potential_1_1_port and potential_1_1_port not in C9300_PORT_OCCUPANCY:
            new_port = potential_1_1_port
            status_suffix = "_VIDEO_1_1"

            create_destination_config_entry(
                sw_name, old_port, new_port, data, dest_stack_obj.name,
                status_suffix, dest_stack_obj
            )
        else:
            if potential_1_1_port in C9300_PORT_OCCUPANCY:
                register_non_migrated(sw_name, old_port, data, 'NO_MIGRADO_COLISION_VIDEO')
            else:
                register_non_migrated(sw_name, old_port, data, 'NO_MIGRADO_FUERA_DE_RANGO_VIDEO')


# ==============================================================================
# 4. FUNCIONES DE REPORTE
# ==============================================================================

def generate_mapping_excel(global_port_map, dest_hostname, timestamp_suffix, migration_type, source_switches, stack_models):
    """Genera el fichero Excel de mapeo, incluyendo Huecos Generados y scsf segregados, y resalta PoE."""
    global HUECO_CANDIDATES

    output_filename = f"Tipo{migration_type}_MapeoMigracion_{dest_hostname}{timestamp_suffix}.xlsx"
    print(f"\n--- Generando Fichero de Mapeo (Excel): {output_filename} ---")
    mapping_data = []

    macs_by_port = defaultdict(list)
    for sw_name, sw_obj in source_switches.items():
        if sw_obj.mac_table.empty:
            continue
        for _, row in sw_obj.mac_table.iterrows():
            macs_by_port[(sw_name, row['Old_Port'])].append(row['MAC'])

    migrated_entries = []
    hueco_filled_entries = []
    unfilled_hueco_entries = []
    not_migrated_entries = []

    for key, mapping_info in global_port_map.items():
        mac_list = macs_by_port.get(key, [])
        macs_text = ', '.join(sorted(set(mac_list))) if mac_list else 'N/A'
        entry = {
            'Hostname Origen': 'N/A',
            'Interface Origen': 'N/A',
            'Description Origen': mapping_info.get('old_description', ''),
            'VLAN Origen': mapping_info.get('old_vlan', 'N/A'),
            'Modo Puerto Origen': mapping_info.get('old_mode', 'N/A'),
            'Hostname Nuevo': mapping_info['new_sw'],
            'Interface Nuevo': mapping_info['new_port'],
            'Status': mapping_info['status'],
            'MAC Origen': macs_text,
            'MAC Nueva': ''
        }

        status = mapping_info['status']
        old_sw_name, old_port = key
        entry['Hostname Origen'] = old_sw_name
        entry['Interface Origen'] = old_port

        if status.startswith('MIGRADO'):
            if 'EN_HUECO' in status:
                entry['Description Origen'] = f"{mapping_info['old_description']} (RELLENO HUECO GENERADO)"
                hueco_filled_entries.append(entry)
            else:
                migrated_entries.append(entry)
        elif status.startswith('NO_MIGRADO'):
            desc = mapping_info['old_description'] if mapping_info.get('old_description') else 'EXCLUIDO'
            entry['Description Origen'] = f"{desc} ({mapping_info['new_port']})"
            not_migrated_entries.append(entry)
        elif status.startswith('PENDIENTE'):
            continue

    for unit_id in sorted(HUECO_CANDIDATES.keys()):
        for hueco_port in HUECO_CANDIDATES[unit_id]:
            unfilled_hueco_entries.append({
                'Hostname Origen': 'HUECO NO RELLENO',
                'Interface Origen': f"Generado en U{unit_id}",
                'Description Origen': 'HUECO GENERADO - SLOT LIBRE',
                'VLAN Origen': 'N/A',
                'Modo Puerto Origen': 'N/A',
                'Hostname Nuevo': dest_hostname,
                'Interface Nuevo': hueco_port,
                'Status': 'HUECO_NO_RELLENO',
                'MAC Origen': 'N/A',
                'MAC Nueva': ''
            })

    mapping_data = migrated_entries + hueco_filled_entries + unfilled_hueco_entries + not_migrated_entries
    expected_ports = build_destination_ports(stack_models)
    mapping_by_new_port = {entry['Interface Nuevo']: entry for entry in mapping_data}
    ordered_entries = []
    for new_port in expected_ports:
        entry = mapping_by_new_port.get(new_port)
        if entry:
            ordered_entries.append(entry)
        else:
            ordered_entries.append({
                'Hostname Origen': 'N/A',
                'Interface Origen': 'N/A',
                'Description Origen': 'PUERTO SIN ASIGNAR',
                'VLAN Origen': 'N/A',
                'Modo Puerto Origen': 'N/A',
                'Hostname Nuevo': dest_hostname,
                'Interface Nuevo': new_port,
                'Status': 'PUERTO_SIN_ASIGNAR',
                'MAC Origen': 'N/A',
                'MAC Nueva': ''
            })
    df = pd.DataFrame(ordered_entries)
    df['Comparacion MAC'] = ''

    try:
        writer = pd.ExcelWriter(output_filename, engine='xlsxwriter')
        df.to_excel(writer, sheet_name='Mapeo de Puertos', index=False)
        workbook = writer.book
        worksheet = writer.sheets['Mapeo de Puertos']

        orange_format = workbook.add_format({'bg_color': '#FFC7CE', 'font_color': '#9C0006', 'bold': True})
        fill_format = workbook.add_format({'bg_color': '#FFEB9C', 'font_color': '#9C5700', 'bold': True})
        gray_format = workbook.add_format({'bg_color': '#D9D9D9', 'font_color': '#404040'})
        green_format = workbook.add_format({'bg_color': '#C6EFCE', 'font_color': '#006100', 'bold': True})
        blue_format = workbook.add_format({'bg_color': '#B4C6E7', 'font_color': '#1F497D', 'bold': True})
        normal_1_1_format = workbook.add_format({'bg_color': '#DAEEF3', 'font_color': '#1F497D'})
        poe_format = workbook.add_format({'bg_color': '#D9EAD3', 'font_color': '#1D5A3C', 'bold': True})

        for row_num, status in enumerate(df['Status']):
            excel_row = row_num + 2
            worksheet.write_formula(
                row_num + 1,
                df.columns.get_loc('Comparacion MAC'),
                f'=SI(I{excel_row}=J{excel_row};"CORRECTA";"DIFERENTES")'
            )

            if status == 'HUECO_NO_RELLENO':
                worksheet.set_row(row_num + 1, None, orange_format)

            elif 'EN_HUECO' in status:
                worksheet.set_row(row_num + 1, None, fill_format)

            elif status.startswith('NO_MIGRADO'):
                worksheet.set_row(row_num + 1, None, gray_format)

            elif 'MIGRADO_POE_' in status:
                worksheet.set_row(row_num + 1, None, poe_format)

            elif status == 'MIGRADO_scsf_MANTENIDO_1_1':
                worksheet.set_row(row_num + 1, None, green_format)
            elif status == 'MIGRADO_scsf_SECUENCIAL':
                worksheet.set_row(row_num + 1, None, blue_format)

            elif status == 'MIGRADO_NORMAL_MANTENIDO_1_1' or status == 'MIGRADO_3800_MANTENIDO_1_1':
                worksheet.set_row(row_num + 1, None, normal_1_1_format)

            elif status.startswith('MIGRADO'):
                worksheet.set_row(row_num + 1, None, None)

        writer.close()
        print(f"Fichero de Mapeo generado: {output_filename}")
    except Exception as e:
        print(f"Error al exportar el Mapeo a Excel: {e}")


def generate_tracking_report(source_switches, global_port_map, dest_hostname, timestamp_suffix, migration_type):
    """
    Genera el fichero de Tracking (MAC -> Puerto Nuevo) en formato Excel.
    """
    output_filename = f"Tipo{migration_type}_TrackingMac_{dest_hostname}{timestamp_suffix}.xlsx"
    print(f"\n--- Generando Fichero de Tracking (MACs a Puerto Nuevo): {output_filename} ---")
    final_tracking_data = []

    for sw_name, sw_obj in source_switches.items():
        if sw_obj.mac_table.empty:
            continue

        for index, row in sw_obj.mac_table.iterrows():
            old_port = row['Old_Port']
            mapping_key = (sw_name, old_port)

            old_description = 'N/A'
            new_sw_name, new_port_map = 'N/A', 'EXCLUIDO/NO MIGRADO'
            status = "NO MIGRADO"

            mapping_info = global_port_map.get(mapping_key)

            if mapping_info and not mapping_info['status'].startswith('PENDIENTE') and mapping_info['status'] != 'HUECO_NO_RELLENO':
                new_sw_name = mapping_info.get('new_sw', 'N/A')
                new_port_map = mapping_info.get('new_port', 'EXCLUIDO/NO MIGRADO')
                status = mapping_info.get('status', 'NO MIGRADO')
                old_description = mapping_info.get('old_description', 'N/A')

            final_tracking_data.append({
                'Switch Origen': sw_name,
                'VLAN': row['VLAN'],
                'MAC': row['MAC'],
                'Puerto Origen': old_port,
                'Description Origen': old_description,
                'Switch Nuevo': new_sw_name,
                'Puerto Nuevo': new_port_map,
                'Status': status
            })

    df = pd.DataFrame(final_tracking_data)

    try:
        df.to_excel(output_filename, index=False)
        print(f"Fichero de Tracking generado: {output_filename}")
    except Exception as e:
        print(f"Error al exportar el tracking a Excel: {e}")


def export_configs(dest_stack_obj, timestamp_suffix, all_vlans_info, migration_type):
    """
    Genera el fichero de configuración .txt final para el 9300.
    """
    global GLOBAL_PORT_MAP

    sw_name = dest_stack_obj.name
    output_path = f"Tipo{migration_type}_Config_{sw_name}{timestamp_suffix}.txt"

    non_migrated_ports = []

    for (old_sw, old_port), mapping_info in GLOBAL_PORT_MAP.items():
        status = mapping_info.get('status', '')
        if status.startswith('NO_MIGRADO'):

            show_ip_info = (
                mapping_info.get('old_mode') == 'L3' or
                mapping_info.get('old_mode') == 'trunk' or
                'N7K' in mapping_info.get('old_description', '').upper()
            )

            line_parts = [
                f"! {old_sw}/{old_port}",
                f"Descripcion antigua: {mapping_info.get('old_description', 'N/A')}",
                f"Razon: {status}"
            ]

            if show_ip_info:
                ip_info = mapping_info.get('ip_address')

                if ip_info is None:
                    ip_info_display = "IP NO ENCONTRADA"
                else:
                    ip_info_display = ip_info

                line_parts.insert(1, f"IP: {ip_info_display}")

            non_migrated_ports.append(' -> '.join(line_parts))

    non_migrated_ports.sort(key=lambda x: get_numeric_sort_key(x.split(' -> ')[0].split('! ')[1]))

    content_lines = []
    content_lines.append(f"! Tipologia de Migracion Elegida: {migration_type}")
    content_lines.append("! ==============================================================")
    content_lines.append("!")

    content_lines.append(f"hostname {sw_name}")
    content_lines.append("!")

    if all_vlans_info:
        sorted_vlans = sorted(all_vlans_info.items(), key=lambda item: int(item[0]))

        vlan_config_lines = []
        for vlan_id, vlan_name in sorted_vlans:
            vlan_config_lines.append(f"vlan {vlan_id}")
            vlan_config_lines.append(f"name {vlan_name}")

        content_lines.extend([
            "! ==============================================================",
            "! Config VLANs. Revisa que esten todas.",
            "! ==============================================================",
        ])
        content_lines.extend(vlan_config_lines)
        content_lines.append("!")

    content_lines.extend([
        "! =====================================================================================================",
        "! Plantilla Config MANUAL de uplinks SW 9300 y modulo NM-8X o NM-2Y.",
        "! =====================================================================================================",
        "!",
        "! Ejemplo de configuracion Uplink segun modulo de stack:",
        "!",
        "! - Si el modulo es C9300-NM-8X:",
        "!   El trunk se configura en el ultimo puerto del modulo (Ten x/1/8)",
        "!   interface TenGigabitEthernet2/1/8",
        "!     description [variable descripcion, miembro trunk 1]",
        "!     switchport mode trunk",
        "!     switchport nonegotiate",
        "!     ip arp inspection trust",
        "!     storm-control broadcast level 1.00",
        "!     channel-protocol lacp",
        "!     channel-group 1 mode active",
        "!     end",
        "!",
        "! - Si el modulo es C9300-NM-2Y:",
        "!   El trunk se configura en el ultimo puerto del modulo (Twe 1/1/2)",
        "!   interface TwentyFiveGigE1/1/2",
        "!     description [variable descripcion, miembro trunk 1]",
        "!     switchport mode trunk",
        "!     switchport nonegotiate",
        "!     ip arp inspection trust",
        "!     speed 10000  (forzado a 10G N7K)",
        "!     storm-control broadcast level 1.00",
        "!     channel-protocol lacp",
        "!     channel-group 1 mode active",
        "!     end",
        "!",
        "! =====================================================================================================",
        "! Plantilla Config MANUAL para Uplinks SW C9200",
        "! =====================================================================================================",
        "!   El trunk se configura en los 2 ultimos interfaces logicos (o en el ultimo si es solo un enlace)",
        "!",
        "!   interface TenGigabitEthernet1/1/3",
        "!     description XXXX",
        "!     switchport mode trunk",
        "!     switchport nonegotiate",
        "!     ip arp inspection trust",
        "!     storm-control broadcast level 1.00",
        "!     end",
        "!",
        "!   interface TenGigabitEthernet1/1/4",
        "!     description XXXX",
        "!     switchport mode trunk",
        "!     switchport nonegotiate",
        "!     ip arp inspection trust",
        "!     storm-control broadcast level 1.00",
        "!     end",
    ])

    if non_migrated_ports:
        content_lines.extend([
            "!",
            "! =================================================================================================",
            "! Puertos Trunk (uplinks y downlinks), NO MIGRADOS (Requieren revision manual):",
            "! La asignacion es MANUAL por lo que deberas aplicar tambien el cambio en el fichero TrackingMac",
            "! =================================================================================================",
            "!"
        ])
        content_lines.extend(non_migrated_ports)
        content_lines.append("!")

    content_lines.extend([
        "!",
        "! ===========================================================================================================",
        "! INICIO CONFIG PUERTOS DE ACCESO. COPY/PASTE PERO REVISAR UNA ULTIMA VEZ.",
        "! ===========================================================================================================",
        "!"
    ])

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(content_lines) + "\n")

        sorted_interfaces = sorted(dest_stack_obj.interfaces.items(), key=lambda item: get_numeric_sort_key(item[0]))

        for int_name, int_data in sorted_interfaces:
            raw_new_config = int_data['config']

            filtered_lines = []
            for line in raw_new_config.split('\n'):
                if 'port-security' in line.lower():
                    continue
                filtered_lines.append(line)

            f.write('\n'.join(filtered_lines) + "\n\n")

        print(f"Fichero de configuracion generado: {output_path}")


# ==============================================================================
# 5. FUNCIONES DE ENTRADA Y FUNCIÓN PRINCIPAL (ORQUESTACIÓN)
# ==============================================================================

def get_migration_type():
    """Muestra el menú de tipología y obtiene la selección del usuario."""
    print("\n--- SELECCION DE TIPOLOGIA DE MIGRACION ---")
    print("1: Migracion de 3700 a 9300 (1:1 simple)")
    print("2: Migracion de 3700 + Integracion servicios en 3800 a 9300")
    print("3: Migracion de 3700 a 9300 + agrupacion ultima unidad stack para scsf")
    print("4: Migracion de 3700 + integracion servicios en 3800 + agrupacion ultima unidad para scsf")
    print("5: Migracion de Video (1:1 estricto con uplinks L3)")

    while True:
        try:
            migration_type = int(input("Rellena la tipologia de migracion a aplicar (1, 2, 3, 4 o 5): "))
            if migration_type in [1, 2, 3, 4, 5]:
                return migration_type
            else:
                print("Tipo de migracion invalido. Debe ser 1, 2, 3, 4 o 5.")
        except ValueError:
            print("Entrada invalida. Rellena un numero entero (1, 2, 3, 4 o 5).")


def get_scsf_file_info(migration_type):
    """Solicita el nombre del fichero scsf si la tipología lo requiere."""
    if migration_type in [3, 4]:
        filename = input("Introduce el nombre del fichero scsf (Enter para 'scsf_T123.txt'): ").strip()
        if not filename:
            filename = 'scsf_T123.txt'
        return filename
    return None


def get_destination_stack_info(migration_type):
    """Solicita la composición del stack de destino 9300, incluyendo modelos y unidades scsf."""
    print("\n--- Composicion del Stack de Destino C9300 ---")
    destination_hostname = input("Rellena el HOSTNAME que tendra el nuevo stack C9300: ").strip().upper()

    while True:
        try:
            num_units = int(input("¿Cuantas unidades componen el stack C9300? (Ej: 4): "))
            if num_units <= 0:
                raise ValueError
            break
        except ValueError:
            print("Entrada invalida. Rellena un numero entero positivo.")

    destination_stack_models = {}
    print("\nModelos de C9300 disponibles:")
    for i, model in enumerate(AVAILABLE_C9300_MODELS):
        print(f"  [{i+1}] {model}")

    for i in range(1, num_units + 1):
        while True:
            model_choice = input(f"Selecciona el numero o rellena el modelo de la Unidad {i}: ").strip().upper()

            if model_choice.isdigit():
                idx = int(model_choice) - 1
                if 0 <= idx < len(AVAILABLE_C9300_MODELS):
                    model = AVAILABLE_C9300_MODELS[idx]
                    break

            if model_choice in PORT_MAPPING_RULES or model_choice.startswith('C9300'):
                model = model_choice
                break

            print("Seleccion invalida. Intenta de nuevo.")

        destination_stack_models[i] = model

    scsf_units = []
    if migration_type in [3, 4]:
        while True:
            try:
                unit_ids = sorted(destination_stack_models.keys())
                print(f"\nUnidades disponibles: {unit_ids}")
                scsf_units_input = input("Rellena las Unidades de Seguridad (scsf) separadas por coma (Ej: 3,4): ")

                scsf_units = [int(u.strip()) for u in scsf_units_input.split(',') if u.strip().isdigit()]

                if all(u in unit_ids for u in scsf_units):
                    break
                else:
                    print("Una o mas unidades scsf no son validas o no existen en el stack.")
            except ValueError:
                print("Entrada invalida. Intenta de nuevo.")

    return destination_hostname, destination_stack_models, scsf_units


def get_source_info(migration_type):
    """Solicita los hostnames de origen, sus modelos y la ruta a sus ficheros de configuración."""
    print("\n--- Switches de Origen ---")
    hosts_input = input("Rellena los HOSTNAMES de ORIGEN separados por coma (Ej: SW_CORE_A, SW_EDGE_B, SW_3800): ")
    source_hostnames = [h.strip().upper() for h in hosts_input.split(',') if h.strip()]

    source_sw_data = {}
    general_path = None

    if migration_type in [1, 3, 5]:
        print("Nota: Para esta tipologia, se asume que UN UNICO FICHERO contiene la configuracion de todos los switches.")
        general_path = input("Rellena la RUTA/NOMBRE del fichero de configuracion UNICO para todos los switches de origen: ").strip()

    for h in source_hostnames:
        model = input(f"Rellena el modelo de switch para {h} (Ej: C3700, C4500, C3800): ").strip().upper()

        config_path = general_path

        if migration_type in [2, 4]:
            config_path = input(f"Rellena la RUTA/NOMBRE del fichero de configuracion (show run, mac y poe) para {h}: ").strip()

        if not config_path:
            print(f"Error: No se pudo obtener la ruta de configuracion para el switch {h}.")
            continue

        source_sw_data[h] = {'model': model, 'config_path': config_path}

    return source_sw_data


def main():
    global GLOBAL_PORT_MAP
    global C9300_PORT_OCCUPANCY
    global HUECO_CANDIDATES

    timestamp_suffix = datetime.now().strftime("_%Y%m%d_%H%M%S")

    print("\n" + "="*80)
    print("= INICIO DE MigraMap v15.1 - Mapeador de Migraciones de Cisco a Catalyst 9300/9200CX =")
    print("= (Priorizacion: scsf > 1:1 Estricto Fa/Gi > Flexible PoE/3800/Huecos) =")
    print("="*80)

    migration_type = get_migration_type()

    scsf_file = get_scsf_file_info(migration_type)

    scsf_vlans = set()
    if scsf_file:
        scsf_vlans = load_security_vlans(scsf_file)
        print(f"VLANs scsf cargadas desde {scsf_file}: {scsf_vlans}")

    destination_hostname, stack_models, scsf_units = get_destination_stack_info(migration_type)

    source_sw_data = get_source_info(migration_type)

    GLOBAL_PORT_MAP = {}
    C9300_PORT_OCCUPANCY = {}
    HUECO_CANDIDATES = defaultdict(list)

    source_switches = {}
    for name, data in source_sw_data.items():
        sw = Switch(name, data['model'], data['config_path'])
        sw.parse_interfaces()
        sw.parse_poe_usage()
        sw.parse_mac_table()
        sw.parse_vlans()
        source_switches[name] = sw

    if not source_switches:
        print("ERROR: No se cargaron configuraciones de origen. Saliendo.")
        sys.exit(1)

    all_units = sorted(stack_models.keys())

    if migration_type in [1, 2, 5]:
        normal_units = all_units
    else:
        normal_units = [u for u in all_units if u not in scsf_units]

    print(f"\nConfiguracion del Stack: Unidades Normales: {normal_units}, Unidades Seguridad (scsf): {scsf_units}")

    all_stack_counters = {unit_id: PortCounter(model) for unit_id, model in stack_models.items()}

    normal_counters = {unit_id: all_stack_counters[unit_id] for unit_id in normal_units}
    scsf_counters = {unit_id: all_stack_counters[unit_id] for unit_id in scsf_units}

    dest_stack_obj = Switch(destination_hostname, "C9300", "")
    dest_stack_obj.interfaces = {}

    migratable_interfaces = collect_and_sort_interfaces(source_switches, migration_type)

    all_vlans_info = {}
    for sw_name, sw_obj in source_switches.items():
        all_vlans_info.update(sw_obj.vlans)

    if migration_type == 5:
        process_video_migration(migratable_interfaces, dest_stack_obj, stack_models)
    else:
        execute_migration(migratable_interfaces, dest_stack_obj, normal_counters, scsf_counters,
                          normal_units, scsf_vlans, scsf_units, stack_models, source_switches)

    map_trunk_ports_interactively(GLOBAL_PORT_MAP, dest_stack_obj.name)

    generate_mapping_excel(
        GLOBAL_PORT_MAP,
        destination_hostname,
        timestamp_suffix,
        migration_type,
        source_switches,
        stack_models
    )
    generate_tracking_report(source_switches, GLOBAL_PORT_MAP, destination_hostname, timestamp_suffix, migration_type)
    export_configs(dest_stack_obj, timestamp_suffix, all_vlans_info, migration_type)

    print("\n--- LEYENDA DE COLORES EN EL FICHERO 'TipoX_MapeoMigracion_hostname_timestamp.xlsx' ---")
    print("  Verde claro: puertos PoE (Power over Ethernet) asignados 1:1 o secuencialmente.")
    print("  Azul claro: puertos Normales o 3800 que mantienen su asignacion original (1:1).")
    print("  Amarillo: puertos que rellenaron un hueco disponible (incluye PoE que relleno hueco).")
    print("  Azul oscuro: puertos de Seguridad (SCSF) asignados secuencialmente.")
    print("  Verde: puertos de Seguridad (SCSF) que mantienen su asignacion original (1:1).")
    print("  Gris: puertos NO MIGRADO.")
    print("  Rojo: hueco no relleno.")
    print("  Blanco: puertos Trunk/L3 migrados manualmente, o puertos Normales/3800 secuenciales sin PoE.")
    print("-------------------------------------------------------------")
    print("\n" + "="*80)
    print("= PROCESO COMPLETADO. Revisa los ficheros de salida. =")
    print("="*80)


try:
    main()
except Exception as e:
    print(f"\n[ERROR CRITICO] Ocurrio un error inesperado: {e}", file=sys.stderr)
    sys.exit(1)
