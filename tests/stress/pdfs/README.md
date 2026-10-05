# PDFs de las pruebas de carga

Set de 4 PDFs usado en todas las mediciones (k6 y Vegeta). Los nombres coinciden con los de los scripts de la cátedra.

| Archivo | Tamaño | Páginas | Texto extraído |
|---|---|---|---|
| `2020-Scrum-Guide-Spanish-Latin-South-American.pdf` | 0,32 MB | 16 | 35.538 caracteres |
| `Essential-Kanban-Condensed-Spanish.pdf` | 8,90 MB | 90 | 124.480 caracteres |
| `Filosofia_Lean.pdf` | 0,67 MB | 42 | 58.520 caracteres |
| `scrum_manager_historias_usuario.pdf` | 3,83 MB | 62 | 89.202 caracteres |

Notas:
- En los scripts de la cátedra el tercero se llama `Filosofia Lean.pdf` (con espacio). Acá se guarda con guion bajo para evitar problemas de comillas en la terminal; el contenido es el mismo.
- Origen: _(completar: de dónde se obtuvieron)_.
- `tests/stress/generate_pdfs.py` genera un set provisorio en `pdfs_provisorios/` (ignorado por Git). Solo sirve para diagnóstico, no para comparar con la cátedra.