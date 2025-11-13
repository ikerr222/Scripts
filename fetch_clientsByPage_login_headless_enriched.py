"""Script para descargar clientes de ExtremeCloud IQ y enriquecerlos con datos de AP.
Cada bloque posee comentarios explicativos para facilitar su comprensión.
"""

# ===== Importaciones estándar y de terceros =====
# Importamos todos los módulos utilizados en el script y describimos brevemente su propósito.
import os
import re
import json
import csv
import time
import random
from datetime import datetime
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ===== Configuración base de la GUI de ExtremeCloud IQ =====
# Estos valores se utilizan para construir las peticiones contra la API privada de clientes.
HOST = "https://ach.extremecloudiq.com"
PATH = "/hm-webapp/services/monitoring/clientmonitor/issue/clientsByPage"

REFERER = f"{HOST}/hm-webapp/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/119.0 Safari/537.36"
)
ACCEPT_LANG = "en-GB,en;q=0.9,es-ES;q=0.8,es;q=0.7,en-US;q=0.6"

OWNER_ID = "233852"
PAGE_SIZE = 100
SORT = "hostname,ASC"
HEALTHY = "true"


# ===== Ventana temporal de datos =====
# Calculamos el intervalo temporal (últimos 30 días) en milisegundos desde epoch.
now_ms = int(time.time() * 1000)
start_ms = now_ms - 30 * 24 * 3600 * 1000
end_ms = now_ms


# ===== Parámetros de resiliencia =====
# Controlamos reintentos, backoff y guardrails para evitar loops infinitos.
RETRY_TOTAL = 5
BACKOFF = 0.8
SLEEP_BETWEEN_PAGES = (0.3, 0.8)
HARD_GUARD = 20000

STATE_FILE = "clientsByPage_state.json"
OUT_JSON = "clientsByPage_last30d_all.json"


# ===== Configuración dinámica del CSV =====
# Generamos el nombre del CSV a partir del mes/año del inicio del intervalo.
MESES_ES = [
    "Enero",
    "Febrero",
    "Marzo",
    "Abril",
    "Mayo",
    "Junio",
    "Julio",
    "Agosto",
    "Septiembre",
    "Octubre",
    "Noviembre",
    "Diciembre",
]
start_dt = datetime.fromtimestamp(start_ms / 1000.0)
OUT_CSV = f"{MESES_ES[start_dt.month - 1]}{start_dt.year}.csv"


# ===== Configuración de la API pública de dispositivos =====
# Estos parámetros controlan la obtención del inventario de APs mediante la API oficial.
API_HOST = "https://api.extremecloudiq.com"
API_PAGE_LIMIT = 100
API_VIEWS = "location"
API_FIELDS = "id,hostname,device_function,product_type,locations"


# ===== Utilidades de credenciales =====
def get_credentials() -> tuple[str, str]:
    """Obtiene las credenciales desde variables de entorno."""

    username = os.environ.get("XIQ_USERNAME")
    password = os.environ.get("XIQ_PASSWORD")
    if not username or not password:
        raise RuntimeError(
            "Define XIQ_USERNAME y XIQ_PASSWORD en variables de entorno antes de ejecutar el script."
        )
    return username, password


# ===== Login SSO con Playwright =====
def sso_login_and_get_cookies():
    """Realiza un login real en el portal SSO y devuelve las cookies válidas."""

    # Importación tardía para evitar depender de Playwright al importar el módulo.
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    username, password = get_credentials()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT, locale=ACCEPT_LANG)
        page = context.new_page()

        # Accedemos primero a la aplicación para forzar la redirección al SSO si no existe sesión.
        page.goto(f"{HOST}/hm-webapp/", wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")

        if page.url.startswith(HOST):
            cookies = context.cookies()
            browser.close()
            return cookies

        # Si no estamos en la pantalla de login, la abrimos manualmente.
        if "sso.extremecloudiq.com" not in page.url:
            page.goto("https://sso.extremecloudiq.com/login?sso=true", wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)

        # Intentamos aceptar cualquier banner de cookies que bloquee la interacción.
        for sel in ['button:has-text("Accept")', 'button:has-text("I Accept")', 'button:has-text("Aceptar")']:
            try:
                page.click(sel, timeout=1500)
                break
            except Exception:
                continue

        # Buscamos campos de email mediante múltiples estrategias de localización.
        email_locators = [
            page.get_by_label(re.compile(r"^\s*email\s*$", re.I)),
            page.get_by_placeholder(re.compile(r"email", re.I)),
            page.get_by_role("textbox", name=re.compile(r"email", re.I)),
            page.locator('input[type="email"]'),
            page.locator('input[name*="user" i]'),
            page.locator('input[id*="user" i]'),
            page.locator('input[type="text"]'),
        ]
        filled_email = False
        for loc in email_locators:
            try:
                loc.first.wait_for(state="visible", timeout=4000)
                loc.first.fill(username)
                filled_email = True
                break
            except Exception:
                continue

        # Repetimos el mismo enfoque para el campo de contraseña.
        pwd_locators = [
            page.get_by_label(re.compile(r"^\s*password\s*$", re.I)),
            page.get_by_placeholder(re.compile(r"password", re.I)),
            page.get_by_role("textbox", name=re.compile(r"password", re.I)),
            page.locator('input[type="password"]'),
            page.locator('input[name*="pass" i]'),
            page.locator('input[id*="pass" i]'),
        ]
        filled_pwd = False
        for loc in pwd_locators:
            try:
                loc.first.wait_for(state="visible", timeout=4000)
                loc.first.fill(password)
                filled_pwd = True
                break
            except Exception:
                continue

        # En caso de no encontrar los campos, probamos una escritura directa con teclado.
        if not filled_email or not filled_pwd:
            try:
                tb = page.get_by_role("textbox").first
                tb.wait_for(state="visible", timeout=3000)
                tb.click()
                page.keyboard.type(username)
                page.keyboard.press("Tab")
                page.keyboard.type(password)
                filled_email = True
                filled_pwd = True
            except Exception:
                pass

        if not filled_email or not filled_pwd:
            page.screenshot(path="login_debug.png", full_page=True)
            with open("login_debug.html", "w", encoding="utf-8") as f:
                f.write(page.content())
            raise RuntimeError(
                "No pude localizar/rellenar Email o Password. Se guardaron login_debug.png y login_debug.html"
            )

        # Intentamos hacer submit mediante botones comunes o con Enter como fallback.
        clicked = False
        for sel in [
            'button:has-text("Log In")',
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Acceder")',
            'button:has-text("Iniciar sesión")',
        ]:
            try:
                page.click(sel, timeout=4000)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            page.keyboard.press("Enter")

        # Esperamos a que vuelva a la consola principal para extraer las cookies.
        try:
            page.wait_for_url(re.compile(r"^https?://ach\.extremecloudiq\.com/.*"), timeout=60000)
        except PWTimeout:
            page.goto(f"{HOST}/hm-webapp/", wait_until="domcontentloaded")

        cookies = context.cookies()
        browser.close()
        return cookies


# ===== Utilidades HTTP =====
def seed_requests_session_with_cookies(session: requests.Session, cookies_all):
    """Copia las cookies del contexto de Playwright a una sesión de requests."""

    def domain_matches(d):
        return d and (d.endswith(".extremecloudiq.com") or d.endswith("extremecloudiq.com"))

    for c in cookies_all:
        domain = c.get("domain", "")
        if not domain_matches(domain):
            continue
        name = c.get("name")
        value = c.get("value")
        path = c.get("path", "/")
        if name and value is not None:
            session.cookies.set(name, value, domain=domain, path=path)


def new_session():
    """Construye una sesión requests configurada con cabeceras y reintentos."""

    s = requests.Session()
    s.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": ACCEPT_LANG,
            "User-Agent": USER_AGENT,
            "Referer": REFERER,
            "Origin": "https://ach.extremecloudiq.com",
            "Connection": "keep-alive",
            "Accept-Encoding": "gzip, deflate, br",
        }
    )
    retry = Retry(
        total=RETRY_TOTAL,
        connect=RETRY_TOTAL,
        read=RETRY_TOTAL,
        backoff_factor=BACKOFF,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def build_url(page_num: int) -> str:
    """Genera la URL con query params adecuada para una página concreta."""

    q = {
        "page.size": PAGE_SIZE,
        "page.page": page_num,
        "page.sort": SORT,
        "paged": "true",
        "healthy": HEALTHY,
        "startTime": start_ms,
        "endTime": end_ms,
        "ownerId": OWNER_ID,
        "ownerIds": OWNER_ID,
    }
    return f"{HOST}{PATH}?{urlencode(q)}"


def extract_items(payload: dict) -> list:
    """Extrae el array de registros sin importar el nombre del campo."""

    for key in ("content", "data", "items", "records", "list", "results"):
        v = payload.get(key)
        if isinstance(v, list):
            return v
    if isinstance(payload, list):
        return payload
    return []


def detect_total_pages(meta: dict) -> int | None:
    """Detecta el número total de páginas si la respuesta lo expone."""

    for k in ("totalPages", "total_pages", "pageCount", "pages", "page_count"):
        v = meta.get(k)
        if isinstance(v, int) and v > 0:
            return v
    if "page" in meta and isinstance(meta["page"], dict):
        for k in ("totalPages", "total_pages", "pageCount"):
            v = meta["page"].get(k)
            if isinstance(v, int) and v > 0:
                return v
    return None


def is_last_page(meta: dict) -> bool | None:
    """Determina si la respuesta indica explícitamente que es la última página."""

    for k in ("last", "isLast", "lastPage"):
        if k in meta:
            return bool(meta[k])
    if "page" in meta and isinstance(meta["page"], dict):
        for k in ("last", "isLast", "lastPage"):
            if k in meta["page"]:
                return bool(meta["page"][k])
    return None


def number_of_elements(meta: dict) -> int | None:
    """Obtiene cuántos elementos se devolvieron en la página actual."""

    for k in ("numberOfElements", "elementsInPage"):
        v = meta.get(k)
        if isinstance(v, int):
            return v
    if "page" in meta and isinstance(meta["page"], dict):
        for k in ("numberOfElements", "elementsInPage"):
            v = meta["page"].get(k)
            if isinstance(v, int):
                return v
    return None


def normalize_cell(val):
    """Convierte dict/list a JSON al escribir el CSV."""

    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False, separators=(",", ":"))
    return val


def save_state(next_page: int):
    """Guarda la siguiente página a procesar para reanudar en caso de fallo."""

    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"next_page": next_page}, f)
    except Exception:
        pass


def load_state() -> int:
    """Lee desde disco la página en la que se quedó el último intento."""

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            j = json.load(f)
            return int(j.get("next_page", 1))
    except Exception:
        return 1


# ===== Utilidades para formatear ubicaciones =====
_ID_PREFIX = re.compile(r"^\s*\[?\d{3,}\]?\s*[:-]?\s*")
_ID_PAREN = re.compile(r"\s*\(\d{3,}\)\s*$")


def _strip_id_tokens(s: str) -> str:
    s = _ID_PREFIX.sub("", s or "")
    s = _ID_PAREN.sub("", s)
    return s.strip()


def pretty_location(locs) -> str:
    """Normaliza un campo location heterogéneo a un path legible."""

    def _from_item(it):
        if isinstance(it, dict):
            for k in (
                "name",
                "fullName",
                "full_name",
                "displayName",
                "locationName",
                "label",
                "value",
                "text",
                "title",
            ):
                if k in it and isinstance(it[k], str) and it[k].strip():
                    return _strip_id_tokens(it[k])
            if "path" in it:
                return pretty_location(it["path"])
            if "names" in it:
                return pretty_location(it["names"])
            return ""
        return _strip_id_tokens(str(it))

    if isinstance(locs, str):
        parts = [p.strip() for p in re.split(r"[>/]", locs) if p.strip()]
        return " > ".join(_strip_id_tokens(p) for p in parts)

    if isinstance(locs, list):
        parts = []
        for it in locs:
            if isinstance(it, (str, dict)):
                parts.append(_from_item(it))
            else:
                parts.append(_strip_id_tokens(str(it)))
        parts = [p for p in parts if p]
        return " > ".join(parts)

    if isinstance(locs, dict):
        if "path" in locs:
            return pretty_location(locs["path"])
        if "names" in locs:
            return pretty_location(locs["names"])
        if "name" in locs:
            return _strip_id_tokens(str(locs["name"]))
        return ""

    return ""


# ===== API pública para recuperar APs =====
def api_login_get_token() -> str:
    """Realiza login en la API pública y devuelve el token de acceso."""

    username, password = get_credentials()
    r = requests.post(
        f"{API_HOST}/login", json={"username": username, "password": password}, timeout=30
    )
    if r.status_code != 200:
        raise RuntimeError(f"API login HTTP {r.status_code}: {r.text[:200]}")
    j = r.json()
    token = j.get("access_token")
    if not token:
        raise RuntimeError("API: no access_token en la respuesta")
    return token


def new_api_session(token: str) -> requests.Session:
    """Construye una sesión requests autenticada contra la API pública."""

    s = requests.Session()
    s.headers.update({"Accept": "application/json", "Authorization": f"Bearer {token}"})
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    s.mount("https://", adapter)
    return s


def fetch_ap_map(api: requests.Session) -> dict:
    """Descarga el inventario completo de APs y lo indexa por device_id."""

    page = 1
    ap_map = {}
    while True:
        params = {
            "page": page,
            "limit": API_PAGE_LIMIT,
            "views": API_VIEWS,
            "fields": API_FIELDS,
        }
        r = api.get(f"{API_HOST}/devices", params=params, timeout=60)
        if r.status_code == 429 and "Retry-After" in r.headers:
            time.sleep(int(r.headers["Retry-After"]))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"API devices HTTP {r.status_code}: {r.text[:200]}")
        j = r.json()
        items = j.get("data") or j.get("items") or []
        if not items:
            break
        for d in items:
            fn = str(d.get("device_function", "")).upper()
            pty = str(d.get("product_type", "")).upper()
            if fn != "AP" and not pty.startswith("AP"):
                continue
            dev_id = d.get("id")
            if dev_id is None:
                continue
            name = d.get("hostname") or ""
            locs = d.get("locations") or []
            loc_path = pretty_location(locs)
            ap_map[dev_id] = {"ap_hostname": name, "ap_location": loc_path}
        if len(items) < API_PAGE_LIMIT:
            break
        page += 1
    return ap_map


# ===== Función principal =====
def main():
    """Orquesta todo el flujo de descarga, enriquecimiento y exportación."""

    # 1) Login SSO y creación de sesión autenticada para la API privada de clientes.
    cookies_all = sso_login_and_get_cookies()
    session = new_session()
    seed_requests_session_with_cookies(session, cookies_all)

    # 1bis) Obtenemos el inventario de APs mediante la API pública.
    try:
        token = api_login_get_token()
        api = new_api_session(token)
        ap_map = fetch_ap_map(api)
        print(f"[api] APs indexados: {len(ap_map)}")
    except Exception as e:
        print(f"[api] WARN: No pude obtener el inventario de APs por API pública: {e}")
        ap_map = {}

    # 2) Iteramos todas las páginas de clientes respetando los límites configurados.
    page = 1
    all_items = []
    total_pages = None

    while page < HARD_GUARD:
        url = build_url(page)
        r = session.get(url, timeout=(10, 90))

        ctype = r.headers.get("Content-Type", "")
        if "text/html" in ctype or r.text.lstrip().startswith("<!DOCTYPE html"):
            print(f"[page {page}] recibido HTML (login). Reintentando login...")
            cookies_all = sso_login_and_get_cookies()
            session = new_session()
            seed_requests_session_with_cookies(session, cookies_all)
            r = session.get(url, timeout=(10, 90))

        try:
            j = r.json()
        except Exception:
            print(f"[page {page}] HTTP {r.status_code}: {r.text[:600]}")
            break

        if r.status_code != 200:
            rid = j.get("requestId") or j.get("request_id")
            print(f"[page {page}] HTTP {r.status_code} (requestId={rid})")
            print(json.dumps(j, ensure_ascii=False, indent=2))
            break

        items = extract_items(j)
        count_here = len(items)
        print(f"[page {page}] items: {count_here}")

        if ap_map:
            for rec in items:
                if isinstance(rec, dict):
                    did = rec.get("deviceId")
                    info = ap_map.get(did)
                    if info:
                        rec["apHostname"] = info.get("ap_hostname", "")
                        rec["apLocation"] = info.get("ap_location", "")
                    else:
                        rec.setdefault("apHostname", "")
                        rec.setdefault("apLocation", "")

        all_items.extend(items)
        save_state(page + 1)

        if total_pages is None:
            total_pages = detect_total_pages(j)
        if is_last_page(j) is True:
            break
        if isinstance(total_pages, int) and total_pages > 0 and page >= total_pages - 1:
            break
        noe = number_of_elements(j)
        if (count_here < PAGE_SIZE) or (noe is not None and noe < PAGE_SIZE):
            break

        time.sleep(random.uniform(*SLEEP_BETWEEN_PAGES))
        page += 1

    # 3) Persistimos toda la información en JSON para su uso posterior.
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(all_items, f, ensure_ascii=False, indent=2)

    # 4) Exportamos un CSV incluyendo todas las claves detectadas.
    all_keys = set()
    for rec in all_items:
        if isinstance(rec, dict):
            all_keys.update(rec.keys())

    preferred_order = [
        "id",
        "connectionStatus",
        "connectionType",
        "hostName",
        "snr",
        "rssi",
        "channel",
        "ipAddress",
        "macAddress",
        "userName",
        "osType",
        "vlan",
        "ssidOrPort",
        "userProfile",
        "radioType",
        "deviceId",
        "apHostname",
        "apLocation",
        "channelUtil",
        "timestamp",
        "category",
        "overallClientHealthScore",
    ]
    cols = [c for c in preferred_order if c in all_keys] + [c for c in sorted(all_keys) if c not in preferred_order]

    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=cols,
            delimiter=";",
            quoting=csv.QUOTE_MINIMAL,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for rec in all_items:
            if not isinstance(rec, dict):
                continue
            writer.writerow({k: normalize_cell(rec.get(k, "")) for k in cols})

    print(f"\nTotal filas (clientes): {len(all_items)}")
    print(f"JSON guardado en: {OUT_JSON}")
    print(f"CSV  guardado en: {OUT_CSV} (sep=';')")
    print("Siguiente página sugerida para reanudar (si hiciera falta):", load_state())


if __name__ == "__main__":
    main()
