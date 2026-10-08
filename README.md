# dashai-plugins (índice de prueba)

Índice de la tienda de plugins de dashAI. **No guarda plugins:** cada archivo de `plugins/`
apunta al repositorio de GitHub de un plugin. `index.json` es lo que dashAI descarga.

`index.json` y `readmes/` los genera el workflow **Build index** (`scripts/validate.py`) al
hacer merge, cada día y a mano (*Actions → Build index → Run workflow*). Cada PR pasa por el
workflow **Check**. Los dos revisan lo que se puede sin dashAI: la entrada, el repo (público,
no archivado, licencia), las releases (archivo, tamaño, atestación) y el manifiesto (campos,
`id`, versión igual al tag, tipos, que cada clase exista). Lo que necesita dashAI (choques con
sus componentes, dependencias, cargar el código) lo vuelve a revisar dashAI al instalar.

- `plugins/<id>.json`: una entrada por plugin (`id` y `repo`).
- `removed.json`: plugins retirados, con el motivo.
- `index.json`: lo genera **Build index**; no se edita a mano.
- `readmes/<id>.md`: el README de la release revisada de cada plugin (lo genera **Build index**).
- `schema/`: el formato de cada archivo (JSON Schema).
- `scripts/validate.py` y `.github/workflows/`: el bot.

Para publicar un plugin, ver [CONTRIBUTING.md](CONTRIBUTING.md).
