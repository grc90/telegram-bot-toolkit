# Desplegar los 6 bots en Oracle Cloud (Always Free)

Guía para que los bots corran 24/7 en una VM gratis de Oracle Cloud, sin depender
de tu laptop. No hay puertos entrantes que abrir: los bots hacen *polling* saliente
a Telegram, nada escucha desde afuera.

## Resumen del enfoque

- Una VM ARM **Always Free** (Ubuntu) corriendo los 6 bots.
- Cada bot es un servicio **systemd** → arranca al bootear, se reinicia solo si crashea,
  logs en `journalctl`. Es lo que hoy hacés a mano con las tareas de VS Code, automatizado.
- El repo va a `/opt/telegram-bots`. El `.env` y los JSON de estado se **copian a mano**
  (están git-ignorados) para que los bots sigan donde quedaron.

---

## 1. Crear la VM en Oracle Cloud

1. Registrate en https://cloud.oracle.com (pide tarjeta para verificar, pero Always Free no cobra).
2. **Compute → Instances → Create instance**.
3. Image & shape:
   - Image: **Ubuntu 24.04**.
   - Shape: **Ampere (ARM)** — `VM.Standard.A1.Flex`. Con 1 OCPU y 6 GB RAM sobra
     de largo para 6 bots de polling. (Si "out of capacity", probá otra Availability Domain
     o región; es común en el tier gratis.)
4. **SSH keys**: subí tu clave pública (o generá una y guardá la privada).
5. Crear. Anotá la **IP pública**.

> No hace falta tocar Security Lists / ingress: los bots solo hacen conexiones salientes.

---

## 2. Conectarte y traer el código

Desde tu laptop (PowerShell o Git Bash):

```bash
ssh ubuntu@<IP_PUBLICA>
```

En la VM, cloná el repo en `/opt/telegram-bots`:

```bash
sudo mkdir -p /opt/telegram-bots
sudo chown ubuntu:ubuntu /opt/telegram-bots
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
scp .env ubuntu@<IP_PUBLICA>:/opt/telegram-bots/

# Estado runtime de todos los bots
scp *_state.json *_queue.json *_settings.json recent_posts_*.json \
    ubuntu@<IP_PUBLICA>:/opt/telegram-bots/
```

> Antes de copiar, en la laptop conviene correr `py -3.14 backup_state.py` por las dudas.

---

## 4. Instalar y arrancar todo

En la VM:

```bash
cd /opt/telegram-bots
chmod +x deploy/install.sh
./deploy/install.sh
```

El script instala Python 3.14 (PPA deadsnakes), crea el `.venv`, instala
`requirements.txt`, instala la unit de systemd y levanta los 6 bots.

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
sudo systemctl restart telegram-bot@example_posting_bot
sudo systemctl stop    telegram-bot@example_posting_bot
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
sudo systemctl restart 'telegram-bot@*'
```

---

## Notas

- **Backups**: los posting bots ya corren su propia limpieza de backups cada 6 h. Los backups
  quedan en `/opt/telegram-bots/backups/`. Si querés copia externa, un `cron` con `scp` de esa
  carpeta a otro lado alcanza.
- **Zona horaria de la VM**: no afecta a los bots (usan las TZ de su `.env`), pero podés fijarla
  con `sudo timedatectl set-timezone America/Argentina/Buenos_Aires`.
- **Si deadsnakes no tuviera 3.14 para ARM**: el código no usa nada exclusivo de 3.14; podés
  usar el Python del sistema (`python3`) editando `PY=` en `install.sh` y el `ExecStart` de la unit.
  Corré los tests en la VM (`./.venv/bin/python -m pytest -q`) para confirmar antes de depender de eso.
