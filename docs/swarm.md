# Docker Swarm

Si el teu servidor fa servir Docker Swarm (ordres `docker stack` i `docker service`), fes servir
la pila [deploy/swarm/3cat-downloader.yml](../deploy/swarm/3cat-downloader.yml) en lloc del
`docker-compose.yml`. Si no saps si fas servir Swarm, executa `docker info | grep Swarm`:
si diu `active`, és Swarm.

## Instal·lar

```bash
mkdir -p ~/3cat-downloader && cd ~/3cat-downloader
curl -fsSLO https://raw.githubusercontent.com/Uriracer/3cat-downloader/main/deploy/swarm/3cat-downloader.yml
sudo mkdir -p /srv/3cat-downloader/config /srv/media/3cat
sudo chown -R "$(id -u):$(id -g)" /srv/3cat-downloader /srv/media/3cat
PUID=$(id -u) PGID=$(id -g) docker stack deploy -c 3cat-downloader.yml mediaserver
```

Obre `http://<ip-del-servidor>:8765/` i crea la contrasenya. El servei es dirà
`mediaserver_3cat-downloader`.

Variables que pots posar davant de `docker stack deploy`:

| Variable | Per defecte | Per a què serveix |
|---|---|---|
| `TRESDL_TAG` | `latest` | versió de la imatge, per exemple `0.4.0` |
| `TRESDL_MEDIA` | `/srv/media/3cat` | carpeta dels vídeos |
| `TRESDL_CONFIG` | `/srv/3cat-downloader/config` | configuració, cua i registres |
| `TRESDL_PORT` | `8765` | port web |
| `PUID`, `PGID`, `TZ` | `1000`, `1000`, `Europe/Madrid` | usuari dels fitxers i fus horari |

Si ja tens una pila amb Sonarr i Radarr, pots copiar el servei `3cat-downloader` al teu
fitxer. Així Sonarr el troba pel nom: `http://3cat-downloader:8765`.

Swarm només accepta **camins absoluts** als volums, ignora `container_name` i `restart` i
sempre fa servir l'última imatge que li demanes. Per això la pila té una sola rèplica: la cua
i la configuració són fitxers, i dues còpies alhora les espatllarien.

## Actualitzar

1. **Espera que la versió estigui publicada.** Quan es publica una versió nova a GitHub, la
   imatge triga uns minuts a construir-se. Si l'actualitzes abans, Swarm diu
   `No such image` i deixa l'actualització en pausa. Comprova-ho amb:

   ```bash
   docker manifest inspect ghcr.io/uriracer/3cat-downloader:0.4.0 > /dev/null && echo publicada
   ```

2. **Actualitza el servei**:

   ```bash
   docker service update --image ghcr.io/uriracer/3cat-downloader:0.4.0 mediaserver_3cat-downloader
   ```

   Si la versió nova no arrenca, Swarm torna sol a l'anterior.

3. **Desa la versió a la pila.** `docker service update` no canvia el fitxer: el pròxim
   `docker stack deploy` tornaria a la versió que hi digui. Fes servir `latest`, o posa
   `TRESDL_TAG=0.4.0` quan facis `docker stack deploy`.

4. **Si fas servir Sonarr o Radarr**, reinicia'ls perquè tornin a llegir què admet
   l'indexador:

   ```bash
   docker service update --force mediaserver_sonarr
   docker service update --force mediaserver_radarr
   ```

## Problemes

| Què passa | Què fer |
|---|---|
| `No such image` i `update paused` | La imatge encara no existeix. Espera que es publiqui i torna a executar `docker service update --image …`: reprèn l'actualització. |
| Vols tornar a la versió anterior | `docker service rollback mediaserver_3cat-downloader` |
| El servei no arrenca | `docker service ps --no-trunc mediaserver_3cat-downloader` mostra l'error de cada intent. |
| `invalid mount config` | Un volum té un camí relatiu o la carpeta no existeix al node. |
| Veure els registres | `docker service logs -f mediaserver_3cat-downloader` |
| Quina versió funciona | `curl -s http://localhost:8765/api/health` |

Si el clúster té més d'un node, el servei ha de funcionar al node que té el disc dels vídeos.
Canvia la restricció `placement` del fitxer, per exemple `node.hostname == raspberrypi`.
