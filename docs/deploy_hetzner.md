# Desplegar los 6 bots en Hetzner Cloud

Guía para que los bots corran 24/7 en un VPS de Hetzner, sin depender de tu laptop.
Sin fricción de antifraude (a diferencia de Oracle Free Tier): tarjeta normal, servidor
activo en minutos. No hay puertos entrantes que abrir: los bots hacen *polling* saliente
a Telegram, nada escucha desde afuera.

El setup en el server (systemd, `install.sh`, estructura de archivos) es el mismo que en
[deploy_oracle.md](deploy_oracle.md); lo único que cambia es cómo se crea la máquina.

## Resumen del enfoque

- Un VPS **CX22** de Hetzner (~€4.5/mes) corriendo los 6 bots. Ubuntu 24.04, x86_64 —
  evita cualquier tema de disponibilidad de paquetes ARM que sí existía con Oracle.
- Cada bot es un servicio **systemd** → arranca al bootear, se reinicia solo si crashea,
  logs en `journalctl`.
- El repo va a `/opt/telegram-bots`. El `.env` y los JSON de estado se **copian a mano**
  (están git-ignorados) para que los bots sigan donde quedaron.

---

## 1. Crear la cuenta y el servidor en Hetzner

1. Registrate en https://console.hetzner.cloud (tarjeta o PayPal; sin la verificación
   agresiva de Oracle).
2. Creá un **Project** (cualquier nombre, ej. "telegram-bots").
3. **Add Server**:
   - Location: la más cercana (ej. Falkenstein/Nuremberg si estás en LatAm da igual de
     latencia, no importa para polling).
   - Image: **Ubuntu 24.04**.
   - Type: **Shared vCPU → CX22** (2 vCPU, 4 GB RAM) alcanza de sobra para 6 bots de polling.
     Si querés ahorrar más, CX12 (2 GB RAM) también debería andar bien.
   - SSH key: subí tu clave pública (o generá una nueva — Hetzner tiene el botón para eso).
   - Dejá firewall/backups como está por defecto (no hace falta abrir puertos).
4. Creá el servidor. Anotá la **IP pública** que te asigna.

---

## 2. Conectarte y traer el código

Desde tu laptop (PowerShell o Git Bash):

```bash
ssh root@<IP_PUBLICA>
```

> Hetzner usa `root` por defecto (a diferencia de Oracle que usa `ubuntu`). Si preferís
> un usuario sin privilegios, creá uno (`adduser bots && usermod -aG sudo bots`) y ajustá
> `User=` en `deploy/telegram-bot@.service` de acuerdo.

En la VM, cloná el repo en `/opt/telegram-bots`:

```bash
mkdir -p /opt/telegram-bots
git clone https://github.com/<tu-usuario>/telegram-bot-toolkit.git /opt/telegram-bots
```

---

## 3. Copiar `.env` y el estado (paso clave)

Estos archivos están git-ignorados, así que el clone **no** los trae. Copialos desde
tu laptop para que los bots continúen con su cola/estado actuales y no re-notifiquen todo.

Desde tu laptop (Git Bash), parado en la carpeta del repo local:

```bash
cd /ruta/a/tu/repo/local

# .env (tokens, channel IDs, BOTS_ALLOWED_USER_ID, timezones, thresholds)
scp .env root@<IP_PUBLICA>:/opt/telegram-bots/

# Estado runtime de todos los bots
scp *_state.json *_queue.json *_settings.json recent_posts_*.json \
    root@<IP_PUBLICA>:/opt/telegram-bots/
```

> Antes de copiar, en la laptop conviene correr `py -3.14 backup_state.py` por las dudas.

---

## 4. Instalar y arrancar todo

En la VM:

```bash
cd /opt/telegram-bots
chmod +x deploy/install.sh
```

`deploy/install.sh` asume el usuario `ubuntu` (venía pensado para Oracle). En Hetzner con
`root`, corré en cambio:

```bash
sed -i 's/^User=ubuntu$/User=root/' deploy/telegram-bot@.service
./deploy/install.sh
```

El script instala Python 3.14 (PPA deadsnakes — en x86_64 esto es confiable, a diferencia
de ARM), crea el `.venv`, instala `requirements.txt`, instala la unit de systemd y levanta
los 6 bots.

> **Importante:** antes de arrancarlos en el server, **apagá los mismos bots en tu laptop**.
> Telegram no permite dos *pollers* con el mismo token — el segundo recibe un `409 Conflict`.
> Una vez que confirmes que corren en la VM, ya no vuelvas a abrirlos en VS Code.

---

## 5. Operación diaria

```bash
# Estado de todos
systemctl status 'telegram-bot@*'

# Logs en vivo de un bot
journalctl -u telegram-bot@example_posting_bot -f

# Reiniciar / detener uno
systemctl restart telegram-bot@example_posting_bot
systemctl stop    telegram-bot@example_posting_bot
```

Los 6 nombres de instancia:

| Servicio systemd | Bot |
|---|---|
| `telegram-bot@imageboard_search_bot` | fetch manual |
| `telegram-bot@imageboard_notifier_bot` | notifier automático |
| `telegram-bot@example_posting_bot` | posteo al canal |
| `telegram-bot@example_posting_bot` | posteo al canal |

---

## 6. Actualizar el código más adelante

```bash
cd /opt/telegram-bots
git pull
./.venv/bin/pip install -r requirements.txt   # solo si cambió requirements.txt
systemctl restart 'telegram-bot@*'
```

---

## Notas

- **Costo**: CX22 son ~€4.5/mes, se cobra por hora si querés apagarlo y prenderlo, pero
  para uso 24/7 sale la tarifa mensual completa.
- **Backups**: los posting bots ya corren su propia limpieza de backups cada 6 h, quedan en
  `/opt/telegram-bots/backups/`. Hetzner también ofrece snapshots/backups de disco completo
  como add-on pago si querés esa capa extra.
- **Zona horaria de la VM**: no afecta a los bots (usan las TZ de su `.env`), pero podés
  fijarla con `timedatectl set-timezone America/Argentina/Buenos_Aires`.
- **Firewall de Hetzner**: opcional, pero si querés reforzar podés crear un Firewall en el
  panel que bloquee todo el tráfico entrante salvo SSH (22) — los bots no necesitan nada más.
