# PDFs de las pruebas de carga

Todas las mediciones (k6 y Vegeta) usan el mismo set de datos: los 4 PDFs oficiales de la
consigna, desde livianos hasta ~9 MB con gráficos/capas.

> **Pendiente:** todavía no se agregaron los archivos. Hasta que estén, los scripts fallan
> con un mensaje que indica cuál falta.

## Archivos

Los scripts buscan estos nombres en esta carpeta, ordenados de más liviano a más pesado:

| Archivo  | Tamaño   | Contenido                      | Origen |
|----------|----------|--------------------------------|--------|
| `01.pdf` | _(a completar)_ | _(a completar)_         | _(a completar)_ |
| `02.pdf` | _(a completar)_ | _(a completar)_         | _(a completar)_ |
| `03.pdf` | _(a completar)_ | _(a completar)_         | _(a completar)_ |
| `04.pdf` | ~9 MB    | Con gráficos/capas             | _(a completar)_ |

Al agregarlos, renombrarlos así y completar la tabla con el nombre original, de dónde se
descargaron y el tamaño exacto (`ls -l tests/stress/pdfs`).

## Uso desde los scripts

- k6 (`tests/stress/k6/spike.js`) los abre con rutas relativas al script: `../pdfs/<archivo>`.
- Vegeta (`tests/stress/vegeta/attack.sh`) los busca en `$(dirname attack.sh)/../pdfs`.

En los dos casos la carpeta se puede cambiar con `PDF_DIR` y la lista de archivos con
`PDF_FILES`, por ejemplo para probar los scripts con otros PDFs sin tocar esta carpeta.
Cómo correrlos: ver [../README.md](../README.md).
