# Scripts

Repositorio para almacenar scripts personales.

## Scripts disponibles

### `inventario_puertos.py`

Herramienta en Python para generar un inventario sencillo de puertos a partir
de logs de switches Cisco (`show interface status`, `show mac address-table`,
`show run`, `show version`, etc.) o, en el modo inverso, para consumir un Excel
ya rellenado y producir el plan de migración. El script produce:

- **Modo 1 (inventario desde logs)**
  - `Migración_<Estación>_v1.0.xlsx`: Excel cuya pestaña "Actual" contiene las
    columnas `Hostname`, `Model`, `Stack`, `Port`, `Description`, `Estado`,
    `Cableado`, `VLANs`, `Last input/output`, `Migrar` y `MACs` (las MACs quedan
    al final). El número de stack se toma del sufijo numérico del nombre del log
    (por ejemplo, `NA_CTPLANETARIO_00_1.log` -> stack `1`) y se reutiliza luego
    en el plan de migración. El nombre del fichero se calcula automáticamente a
    partir del texto común en los nombres de los logs (por ejemplo,
    `Migración_Planetario_v1.0.xlsx`). Entre cada dispositivo se intercala una
    fila azul que anuncia el siguiente log junto con su hostname detectado,
    modelo e IP. La cabecera permanece en la primera fila y la segunda línea de
    la hoja contiene la fila azul del primer dispositivo. Las interfaces
    enrutadas (`no switchport`) muestran el literal `routed` en la columna
    `VLANs` (para diferenciarlas de los puertos de acceso o trunk) y la columna
    `Migrar` siempre indica `No` en ellas aunque estén `connected`. La columna
    `Cableado` marca `Yes` únicamente cuando la interfaz está `connected` (en
    cualquier otro estado queda vacía) y las descripciones se extraen
    directamente de cada bloque `interface` del `show run`, por lo que no se
    recortan como sucede en `show interface status`. Las interfaces con estado
    `notconnect` o `disabled` rellenan `Last input/output` con los valores
    obtenidos en `show interfaces` para saber cuándo estuvieron activas por
    última vez, y la columna `Migrar` marca `Yes` únicamente cuando el puerto
    está `connected` y no es una interfaz enrutada; en cualquier otro caso se
    muestra `No`. Además se genera la hoja "Stacks" con el resumen por equipo
    (SITE, modelo detectado, hostname, IP de gestión, número de stack, etc.).

- **Modo 2 (plan desde un Excel ya cumplimentado)**
  - Solicita el Excel generado en el modo 1 (ya editado con `Cableado` y
    `Migrar`). Toma solo las filas con `Migrar=Yes` y las reparte en hojas por
    número de stack (según la columna `Stack`); las filas sin stack explícito se
    agrupan por hostname en hojas independientes.
  - Pide el modelo de destino para cada hoja (catálogo de 6 tipos con su número
    de puertos) y asigna puertos nuevos secuenciales. Con esos datos genera:
    - `Plan_migracion_<Estación>.xlsx` con las mismas columnas originales más
      `Nuevo puerto` y `Modelo destino`, una hoja por stack/dispositivo.
    - `config_migracion_<Estación>.txt` con la plantilla base y un resumen de las
      asignaciones calculadas.

Requiere `pandas` y `openpyxl`. Al ejecutarlo, primero pregunta qué modo deseas
usar (inventario desde logs o migración desde Excel) y, según el caso, solicita
las rutas necesarias. Las rutas pueden pegarse completas (con espacios o
comillas) y se normalizan antes de validar. La carpeta de salida se crea si no
existe y, si algún fichero está bloqueado, el script permite reintentar o elegir
otra ruta:

```bash
python inventario_puertos.py
```
