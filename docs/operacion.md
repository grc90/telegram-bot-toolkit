# Guía de operación diaria

Referencia práctica para operar el sistema local de bots de Telegram.

---

## Inicio rápido

```powershell
# Verificar que todo esté en orden antes de arrancar
python dev.py health
python dev.py check-env imageboard_notifier
python dev.py check-env examplechannel
python dev.py check-env imageboard_search

# Arrancar cada bot en su propia terminal (uno por entry point)
py -3.14 imageboard_notifier_bot.py
py -3.14 example_posting_bot.py
py -3.14 imageboard_search_bot.py
```

Cada bot es un proceso independiente. Si agregás más canales copiando
`example_posting_bot.py`, cada copia se arranca igual y usa su propio conjunto de
archivos runtime (ver `bot_name` en la copia).

---

## Comandos de `dev.py` — inspección local segura

Todas las operaciones son **de solo lectura**. No modifican queues, state ni recent history.

### `health` — estado general del sistema

```powershell
python dev.py health
```

Verifica:
- Todos los archivos runtime JSON existen y son JSON válido
- Los archivos de log existen
- El archivo `.env` existe

### `check-env` — validar configuración de un bot

```powershell
python dev.py check-env imageboard_notifier
python dev.py check-env examplechannel
python dev.py check-env imageboard_search
```

Verifica que todas las variables de entorno requeridas estén presentes y con formato válido.
Muestra el token parcialmente enmascarado para evitar exponerlo.

### `queue` — inspeccionar la queue de un bot de posteo

```powershell
# Ver primeros 5 posts (default)
python dev.py queue examplechannel

# Solo el conteo
python dev.py queue examplechannel --count

# Ver primeros N posts
python dev.py queue examplechannel --preview 10

# Buscar por URL, file_id, caption
python dev.py queue examplechannel --search "danbooru.donmai.us"
python dev.py queue examplechannel --search "pixiv"
```

### `filter-test` — probar si un tag sería bloqueado

```powershell
python dev.py filter-test imageboard_notifier --tag ai-generated
python dev.py filter-test imageboard_notifier --tag "my_little_pony"
python dev.py filter-test imageboard_notifier --tag animated
```

Muestra qué patrón de la blacklist bloquea el tag (soporta wildcards `*`).
También muestra el score threshold activo y los ratings permitidos para ese bot.

### `recent` — historial reciente de posts publicados

```powershell
python dev.py recent examplechannel
python dev.py recent examplechannel --limit 20
```

Muestra los últimos N posts publicados (por `record_published_post`).
Útil para verificar que un post se publicó sin revisar el log a mano.

### `stats` — resumen del último ciclo desde el log

```powershell
python dev.py stats imageboard_notifier
python dev.py stats examplechannel --lines 500
```

Parsea las últimas N líneas del log y muestra:
- Posts publicados / enviados
- Duplicados y skips por "publicado recientemente"
- Errores de API / polling
- Cooldowns activos
- Eventos de bootstrap (bots Danbooru)

---

## Comandos de Telegram — imageboard_search (búsqueda manual en Danbooru)

El flujo es interactivo: el bot te guía paso a paso por tag → age → rating → order antes de hacer el fetch.

| Comando | Descripción |
|---------|-------------|
| `/start` o `/fetch` | Iniciar una nueva búsqueda (pide tag primero) |
| `/help` | Instrucciones de uso con los 7 pasos del flujo |
| `/cancel` | Cancelar el flujo de búsqueda en curso (antes del fetch) |
| `/stop` | Cortar una tirada ya iniciada (para en el post actual) |
| `/status` | Ver paso actual del flujo y estado de la tirada activa |

**Flujo típico:**
1. `/fetch`
2. Mandás una tag (ej. `remilia`) — el bot ofrece sugerencias inline de Danbooru
3. Elegís la sugerencia o usás la tag tal cual
4. Mandás un age (ej. `7d`, `<7d`, `24h`, `3mo`)
5. Mandás un rating: `g`, `s`, `q`, `e` o combinaciones como `g,s`
6. Mandás un order: `updated`, `score`, `favcount`, `rank`, `id`
7. El bot trae hasta 100 posts y los envía por el chat

Si mandás varias palabras en el paso de la tag (ej. `remilia scarlet`), el bot pregunta
si es una sola tag compuesta (`remilia_scarlet`) o varias tags combinadas en AND
(`remilia scarlet`), en vez de asumir una de las dos.

En los pasos de age, rating y order hay botones rápidos (además de poder escribir tu
propio valor) y un botón "< Volver" para corregir el paso anterior sin reiniciar todo
el flujo desde `/fetch`.

`/help` y `/status` funcionan en cualquier momento, incluso a mitad de un paso —
no hace falta cancelar el flujo para consultarlos.

**Diferencia entre `/cancel` y `/stop`:**
- `/cancel` — aborta el flujo de configuración (antes de que empiece el fetch)
- `/stop` — corta una tirada ya en marcha (el bot para después del post que esté procesando)

---

## Comandos de Telegram — bot de monitoreo (imageboard_notifier)

| Comando | Descripción |
|---------|-------------|
| `/help` | Lista todos los comandos disponibles |
| `/subscribe` | Activar notificaciones de nuevos posts |
| `/unsubscribe` | Desactivar notificaciones |
| `/stop` | No hace nada destructivo: aclara que no hay tirada para frenar y sugiere `/unsubscribe` si la intención era esa |
| `/check` | Forzar revisión inmediata de Danbooru |
| `/test` | Enviar el post más reciente que coincida con los filtros |
| `/last6` o `/g6` | Posts `rating:g` con score 5–10 de las últimas 6 horas |
| `/top` | Últimas imágenes con score alto |
| `/hot` | Tendencias del día (order:rank) |
| `/blacklist` | Ver todos los tags bloqueados |
| `/blacklist test <tag>` | Verificar si un tag específico está bloqueado |
| `/status` | Estado del bot: suscriptores, última revisión, config activa |

---

## Comandos de Telegram — bots de posteo (en este repo: examplechannel)

| Comando | Descripción |
|---------|-------------|
| `/post_now` | Publicar el siguiente lote inmediatamente |
| `/post_now N` | Publicar N posts ahora |
| `/post_now xN` | Publicar N tandas de efectivos ahora (N × posts por lote resueltos en ese momento) |
| `/pause` | **Pausar publicaciones** sin detener el bot |
| `/resume` | **Reanudar publicaciones** pausadas |
| `/stop` | Interrumpir el lote en curso |
| `/posts_per_batch` | Cambiar el tamaño del lote |
| `/posts_per_batch_auto` | Modo automático (1% de la queue por lote) |
| `/posts_per_batch_auto2` | Modo automático 2x |
| `/special` | Activar modo especial (lote especial) |
| `/done` | Cerrar modo especial y publicar el lote especial |
| `/queue` | Ver tamaño de la queue |
| `/delete <url>` | Sacar de la queue el post que tenga esa url como source |
| `/status` | Estado completo del bot |

### `/delete <url>`

Busca en la queue el post cuyo source sea esa url (el campo `source_url`, o el `[Source](...)` del caption si no lo tiene) y lo borra. La comparación normaliza igual que el dedupe (host, barra final, `twitter.com` → `x.com`) e ignora además el esquema, así que `http://`, `https://` o pegar la url sin esquema dan el mismo resultado. Si no hay coincidencia no toca el archivo y lo avisa.

### Diferencia entre `/pause` y `/stop`

- `/pause` — detiene la publicación programada permanentemente hasta que uses `/resume`. El bot sigue activo, acepta nuevos posts y responde comandos.
- `/stop` — interrumpe solo el lote **en curso** (ya arrancado). La siguiente publicación programada funciona normal.

---

## Dry-run — modo seguro de verificación

### Posting bots

```powershell
# Ver qué publicaría el próximo lote sin publicar nada
py -3.14 example_posting_bot.py --dry-run

# Con conteo específico
py -3.14 example_posting_bot.py --dry-run --dry-run-count 5
```

Garantías:
- No llama a `bot.send_photo()`, `bot.send_media_group()`, ni `bot.send_message()`
- No modifica la queue
- No escribe en `recent_posts_*.json`
- Verificado por tests en `tests/test_posting_dry_run.py`

### Bot de monitoreo (imageboard_notifier)

```powershell
# Ver qué posts coinciden con los filtros activos, sin enviar nada
py -3.14 imageboard_notifier_bot.py --dry-run
```

Muestra:
- Total de posts que coinciden con el filtro actual
- Cuántos ya fueron procesados vs. nuevos
- Preview de los primeros 10 posts nuevos (ID, score, rating)
- No modifica `*_state.json`

**Ojo:** este dry-run solo ejercita fetch + filtrado; nunca llega a
`send_post_to_chat()`. Un dry-run limpio no dice nada sobre el camino de envío
(elección de variante de foto, retries, skip vs. fail) — eso lo cubren los tests
unitarios.

---

## Blacklist — gestión

### Ver tags bloqueados

El listado completo solo se consulta desde Telegram con `/blacklist`; en el
código vive en el set `BLACKLISTED_TAGS` de `tag_blacklist.py`. Desde la CLI se
testea de a un tag por vez (ver abajo).

### Testear si un tag está bloqueado

```powershell
# CLI (recomendado para verificar antes de editar)
python dev.py filter-test imageboard_notifier --tag "mi_tag"

# Telegram
/blacklist test mi_tag
```

### Agregar un tag a la blacklist

1. Verificar que no esté ya (`python dev.py filter-test imageboard_notifier --tag <tag>`)
2. Editar `tag_blacklist.py` — agregar el tag al set `BLACKLISTED_TAGS`
3. Para wildcards: usar `*_sufijo` (ej: `*_(meme)`)
4. Verificar que el cambio funcione: `python dev.py filter-test imageboard_notifier --tag <tag>`
5. Reiniciar los bots Danbooru para que tomen el nuevo filtro
6. El primer ciclo después del restart hace re-bootstrap automáticamente

**Nota:** Los bots detectan automáticamente que cambió `filter_signature` y hacen re-bootstrap (no envían posts del ciclo actual).

---

## Recuperación ante problemas

### El bot arrancó pero no envía posts

1. Verificar que el bot esté bootstrapped: revisar `logs/imageboard_notifier.log` para "bootstrapped N current matching post(s)"
2. Si fue bootstrapped recién, espera el siguiente ciclo (por defecto 600 segundos)
3. Forzar revisión inmediata: `/check` desde Telegram

### La queue de un posting bot parece vacía o rota

```powershell
# Ver estado actual (solo lectura)
python dev.py queue examplechannel --count
python dev.py health

# Si el archivo está corrupto, restaurar desde backup
ls backups/
# Copiar el archivo de estado deseado manualmente
```

### Un bot no arranca — error de env var

```powershell
# Diagnosticar
python dev.py check-env <nombre_bot>

# Ver el error exacto
py -3.14 imageboard_notifier_bot.py
```

Errores comunes:
- `Missing required environment variable` → falta la var en `.env`
- `does not look like a valid Telegram token` → token mal copiado (espacio, corte, etc.)

### El bot está enviando en loop / enviando duplicados

1. Usar `/stop` desde Telegram para frenar el lote en curso
2. Revisar `recent_posts_<bot>.json` con `python dev.py recent <bot>`
3. Si el problema persiste, hacer backup y reiniciar el bot

### Hacer backup manual del estado runtime

```powershell
python backup_state.py
# Crea backups/state_<YYYYMMDD_HHMMSS>/
```

### Retención de backups viejos (24h) — automática

Cada bot de posteo (en este repo, `examplechannel`) corre `backup_loop()` cada 6 horas: crea un backup nuevo en `backups/state_<timestamp>/` y **después borra automáticamente las carpetas de backup completas con más de 24 horas** (`posting_bot_shared.py: run_backup_retention_cleanup`). La edad se decide por el timestamp del nombre de la carpeta (`state_YYYYMMDD_HHMMSS`), no por su fecha de modificación — borrar archivos de adentro actualiza el `mtime` de la carpeta, así que usar `mtime` haría que las carpetas viejas parezcan "recientes" para siempre. Es seguro que varios bots de posteo corran esta limpieza en paralelo: si dos la disparan casi al mismo tiempo, una carpeta ya borrada por la otra simplemente se ignora.

La limpieza la hacen solo los bots de posteo: si ninguno está corriendo, no se ejecuta y hay que forzarla a mano con los comandos de abajo. Si falla borrar alguna carpeta puntual (ej. un lock momentáneo), queda un aviso en el log/alerta del bot y no frena el resto del lote.

**Comandos manuales** (para forzar la limpieza fuera del ciclo de 6h, o para el caso más chico de solo queues):

```powershell
# Dry-run (default): solo muestra qué se eliminaría — carpeta completa
python backup_state.py --clean-old-backups

# Aplicar de verdad
python backup_state.py --clean-old-backups --apply

# Cambiar la ventana de retención (en horas)
python backup_state.py --clean-old-backups --apply --retention-hours 48

# Variante mas chica: borrar solo los *_queue.json de carpetas viejas,
# conservando state/settings/recent_posts como historial
python backup_state.py --clean-queue-backups --apply
```

---

## Variables de entorno opcionales útiles

| Variable | Bot | Default | Descripción |
|----------|-----|---------|-------------|
| `IMAGEBOARD_NOTIFIER_SCORE_THRESHOLD` | imageboard_notifier | 10 | Score mínimo para notificación automática |
| `IMAGEBOARD_NOTIFIER_TOP_SCORE_THRESHOLD` | imageboard_notifier | 20 | Score mínimo para `/top` |
| `IMAGEBOARD_NOTIFIER_HOT_SCORE_THRESHOLD` | imageboard_notifier | 10 | Score mínimo para `/hot` |
| `IMAGEBOARD_NOTIFIER_POLL_INTERVAL_SECONDS` | imageboard_notifier | 600 | Intervalo entre ciclos (segundos) |
| `IMAGEBOARD_NOTIFIER_DANBOORU_PAGES` | imageboard_notifier | 10 | Páginas de Danbooru a consultar por ciclo |
| `IMAGEBOARD_NOTIFIER_MAX_DOWNLOAD_BYTES` | imageboard_notifier | 51380224 | Límite de descarga por archivo (bytes) |
| `IMAGEBOARD_SEARCH_MAX_DOWNLOAD_BYTES` | imageboard_search | 51380224 | Límite de descarga por archivo (bytes) |
| `IMAGEBOARD_SEARCH_SEND_DELAY_SECONDS` | imageboard_search | 0.8 | Pausa entre envíos de una tirada |
| `EXAMPLECHANNEL_BOT_TIMEZONE` | examplechannel | UTC | Timezone para horarios de publicación |

El nombre de la variable de timezone lo fija cada bot de posteo en su
`timezone_env` (ver `example_posting_bot.py`), así que una copia para otro canal
usa su propio prefijo. La lista completa de variables está en `.env.example`.
