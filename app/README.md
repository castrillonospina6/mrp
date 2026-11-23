# sammu-os (v0.1)

Control de piso para confección: **tablets en planta** (por operario y por módulo) y **tablero de oficina** en tiempo real.
Web pura: Node + Express + SQLite + HTML/JS. Sin Python y sin compilar nada.

## Requisitos
- Node.js **22.13 o superior** (recomendado 24). SQLite viene incluido en Node (`node:sqlite`).

## Correr en local
```bash
npm install
npm start
```
Abre http://localhost:3000. En **Configuración** pulsa "Cargar datos de ejemplo".
Las tablets entran por `http://IP-DEL-PC:3000/#operario` (misma red Wi-Fi; en Windows permite Node en el firewall).

## Variables de entorno
| Variable | Por defecto | Uso |
|---|---|---|
| `PORT` | 3000 | Puerto |
| `DB_FILE` | `./data/sammu.db` | Archivo de la base de datos |
| `ACCESS_KEY` | (vacía) | Si se define, la API pide esa clave (la página la pregunta una vez) |

## Respaldo
Copia `data/sammu.db` con el servidor detenido (en modo WAL, copia también `sammu.db-wal` si existe).

## Docker (recomendado para servidor)
```bash
cp .env.example .env        # pon tu ACCESS_KEY
docker compose up -d --build
```
Queda en `http://IP-DEL-SERVIDOR:3000` (o el `PORT` de tu `.env`). La base vive en el volumen `sammu-data` y sobrevive a reinicios y actualizaciones. El contenedor corre sin root y se reinicia solo.

| Tarea | Comando |
|---|---|
| Ver logs | `docker compose logs -f` |
| Estado y salud | `docker compose ps` (usa `/health`) |
| Actualizar el código | `docker compose up -d --build` |
| Detener | `docker compose stop` |
| Respaldo (detén antes) | `docker run --rm -v sammu-os_sammu-data:/data -v "$PWD":/backup alpine tar czf /backup/sammu-backup.tgz -C /data .` |
| Restaurar | `docker run --rm -v sammu-os_sammu-data:/data -v "$PWD":/backup alpine tar xzf /backup/sammu-backup.tgz -C /data` |

Para internet, pon Caddy o Nginx delante con HTTPS apuntando al puerto 3000.

## Sin Docker
`npm install --omit=dev` y deja `node server.js` corriendo con pm2 o systemd. Sirve cualquier plataforma con Node y disco persistente (Railway, Render, Fly.io, un VPS).

## Estructura
- `server.js`: API REST (`/api/<tabla>`), tiempo real por SSE (`/events`), `/health` y esquema SQLite.
- `Dockerfile`, `docker-compose.yml`, `.env.example`: despliegue con Docker.
- `public/index.html`: toda la interfaz (oficina, operario, módulo, configuración).
- `public/db.js`: cliente de datos de la interfaz contra la API.

## Pendiente (no está en esta versión)
Usuarios y roles reales (hoy solo hay `ACCESS_KEY` compartida), modo offline en tablets, órdenes de producción, incentivos, históricos (ayer y total) y las apps vooc, drillo, battu, tammi y obbo completas.
**No lo expongas a internet sin `ACCESS_KEY` y HTTPS.**
