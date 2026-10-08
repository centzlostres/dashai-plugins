# dashai-plugins (índice de prueba)

Índice de la tienda de plugins de dashAI. **No guarda plugins:** cada archivo de `plugins/`
apunta al repositorio de GitHub de un plugin. `index.json` es lo que dashAI descarga.

Mientras se decide el bot, `index.json` y `readmes/` se generan en local con
`build_index.py` (la vista previa de dashAI sobre las últimas 5 releases de cada plugin, sin
instalar nada) y se suben con un commit.

- `plugins/<id>.json`: una entrada por plugin (`id` y `repo`).
- `removed.json`: plugins retirados, con el motivo.
- `index.json`: lo genera `build_index.py` (después, el bot); no se edita a mano.
- `readmes/<id>.md`: el README de la release revisada de cada plugin (lo genera `build_index.py`).
- `schema/`: el formato de cada archivo (JSON Schema).

Para publicar un plugin, ver [CONTRIBUTING.md](CONTRIBUTING.md).
