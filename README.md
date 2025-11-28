```markdown
# Scripts

Repositorio para almacenar scripts personales.

## Scripts disponibles

### `inventario_puertos.py`

Herramienta en Python para generar un inventario sencillo de puertos a partir
de logs de switches Cisco (`show interface status`, `show mac address-table`,
`show run`, `show version`, etc.). El script produce un único Excel:

- `Migración_<Estación>_v1.0.xlsx`: Excel cuya pestaña "Actual" contiene las
  columnas `Hostname`, `Model`, `Port`, `Description`, `Estado`, `Cableado`,
  `VLANs`, `Last input/output`, `Migrar` y `MACs` (las MACs quedan al final). El nombre
  del fichero se calcula automáticamente a partir del texto común en los
  nombres de los logs (por ejemplo, `Migración_Planetario_v1.0.xlsx`). Entre cada
  dispositivo se intercala una fila azul que anuncia el siguiente log junto con
  su hostname detectado, modelo e IP. La cabecera permanece en la primera fila y
  la segunda línea de la hoja contiene la fila azul del primer dispositivo. Las
  interfaces enrutadas (`no switchport`) muestran el literal `routed` en la
  columna `VLANs` (para diferenciarlas de los puertos de acceso o trunk) y la
  columna `Migrar` siempre indica `No` en ellas aunque estén `connected`. La
  nueva columna `Cableado` marca `Sí` únicamente cuando la interfaz está
  `connected` (en cualquier otro estado queda vacía) y las descripciones se
  extraen directamente de cada bloque `interface` del `show run`, por lo que no
  se recortan como sucede en `show interface status`. Las interfaces con estado
  `notconnect` o `disabled` rellenan `Last input/output` con los valores obtenidos
  en `show interfaces` para saber cuándo estuvieron activas por última vez, y la
  columna `Migrar` marca `Yes` únicamente cuando el puerto está `connected` y no
  es una interfaz enrutada; en cualquier otro caso se muestra `No`.

Requiere `pandas`. Al ejecutarlo, primero pregunta cómo quieres indicar los
logs: puedes seguir introduciéndolos manualmente (uno por línea, reutilizando
los `.log` del directorio actual si no escribes ninguno) o bien señalar una
carpeta para que procese automáticamente todos los `.log` que contenga. En
ambos casos es posible pegar rutas completas con espacios o entrecomilladas, ya
que se normalizan antes de validar. Después, el script solicita la carpeta donde
se generará el Excel (Enter en vacío = directorio actual); también puede ser una
ruta fuera del repositorio y, si no existe, se crea automáticamente. Si el
fichero de salida no puede escribirse (por ejemplo, porque esté abierto en
Excel o no haya permisos), el script avisará y te permitirá reintentar en la
misma carpeta o indicar otra distinta:

```bash
python inventario_puertos.py
```
```
