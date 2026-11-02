# Sonarr i Radarr

3Cat Downloader es presenta a Sonarr i Radarr com dues peces que ja coneixen:

- un **indexador Newznab**, on busquen sèries i pel·lícules de 3cat;
- un **client de descàrrega SABnzbd**, que rep el que han triat, ho descarrega de 3cat i
  els diu on és perquè ho importin a la biblioteca.

No hi ha Usenet pel mig: els fitxers `.nzb` només porten l'identificador del vídeo de 3cat.
Amb això, quan demanes una sèrie a Seerr o a Sonarr, si és a 3cat es descarrega sola. Els
capítols nous d'una sèrie en emissió també es baixen sols amb la sincronització RSS.

## Requisits

- 3Cat Downloader en **mode servidor**: Docker, Linux natiu o `--host 0.0.0.0`.
- Sonarr i Radarr han de veure la **mateixa carpeta de descàrregues al mateix camí**. Amb
  l'[stack de Docker](../deploy/arr-stack/README.md) ja és així: `/data/downloads/3cat`.
- La **clau d'API** de 3Cat Downloader, que trobaràs a Configuració → Integració amb
  Sonarr / Radarr. La mateixa pàgina mostra les adreces que cal escriure.

Als exemples, `3cat-downloader` és el nom del contenidor dins la xarxa de Docker. Si no fas
servir Docker, posa-hi l'adreça IP del servidor.

## 1. Indexador

A Sonarr: **Settings → Indexers → + → Newznab → Custom**.

| Camp | Valor |
|---|---|
| Name | `3Cat` |
| Enable RSS / Automatic Search / Interactive Search | sí |
| URL | `http://3cat-downloader:8765/arr/newznab` |
| API Path | `/api` |
| API Key | la clau d'API |
| Categories | `5030,5040` |

A Radarr és igual, amb **Categories** `2030,2040`.

Prem **Test**. Ha de sortir en verd: el canal RSS retorna els últims capítols i pel·lícules
de 3cat.

Si gestiones els indexadors amb **Prowlarr**, pots afegir-lo allà com a **Generic Newznab**
amb els mateixos valors i deixar que el sincronitzi. També funciona, però directament a
Sonarr i Radarr és més senzill.

## 2. Client de descàrrega

A Sonarr: **Settings → Download Clients → + → SABnzbd**.

| Camp | Valor |
|---|---|
| Name | `3Cat Downloader` |
| Host | `3cat-downloader` |
| Port | `8765` |
| URL Base | `sabnzbd` |
| API Key | la clau d'API |
| Category | `tv` |
| Client Priority | `1` (o un número més baix que el de qBittorrent) |

A Radarr és igual, amb **Category** `movies`.

Prem **Test** i desa.

Els fitxers es desen a `<carpeta de descàrregues>/tv/<nom del llançament>/` (o `movies/`).
Quan Sonarr o Radarr els han importat, demanen esborrar-los i 3Cat Downloader els elimina.

## 3. Comprovar-ho

1. A Sonarr, afegeix una sèrie que sigui a 3cat, per exemple *Merlí* o *Polseres vermelles*.
2. Obre la sèrie i prem **Interactive Search** en un capítol. Han d'aparèixer resultats de
   l'indexador `3Cat`, amb noms com `Merli.S01E01.Els.peripatetics.CATALAN.1080p.WEB-DL…`.
3. Baixa'n un. El veuràs a la cua de Sonarr (**Activity → Queue**) i a la pantalla de
   descàrregues de 3Cat Downloader.
4. Quan acabi, Sonarr l'importa a la biblioteca i Jellyfin el mostra.

## Com troba les sèries

- Sonarr busca primer per l'**identificador de TheTVDB**. 3Cat Downloader demana a TVmaze el
  nom original de la sèrie i busca a 3cat el programa amb aquest nom. Així troba les sèries
  que TheTVDB té amb el títol en anglès: *Genesis (2026)* a Sonarr és *Gènesi* a 3cat.
- Si això no funciona, Sonarr busca pel títol. 3Cat Downloader només respon si hi ha un
  programa de 3cat amb **exactament el mateix títol**, sense tenir en compte accents,
  majúscules ni signes. Així no es baixa mai un programa equivocat.
- Si tot i així no la troba, descarrega aquella sèrie des de la pàgina de 3Cat Downloader.
  La cerca per identificador necessita que el servidor pugui accedir a `api.tvmaze.com`.
- Les temporades i els números de capítol són els de 3cat. Normalment coincideixen amb
  TheTVDB; si no, fes servir la cerca interactiva.
- En una cerca de temporada surt també un **paquet de temporada** amb tots els capítols.
- Els programes diaris (informatius, magazins) es reconeixen per la data.
- Radarr busca pel títol i l'any. Les pel·lícules s'agafen del catàleg de pel·lícules de 3cat.

## Problemes habituals

| Missatge | Solució |
|---|---|
| Una sèrie de 3cat no surt a la cerca | Si acabes d'actualitzar 3Cat Downloader, reinicia Sonarr perquè torni a llegir què admet l'indexador. Comprova que la sèrie de TheTVDB és la mateixa que la de 3cat (canal TV3, any). |
| *Unable to connect* | Comprova que 3Cat Downloader està en mode servidor i que el host i el port són correctes. |
| *API Key Incorrect* | Copia de nou la clau d'API des de Configuració. |
| La importació no troba els fitxers | Sonarr no veu la carpeta al mateix camí. Fes servir la mateixa carpeta `/data` als dos contenidors, o configura un **Remote Path Mapping** a Sonarr. |
| La descàrrega falla amb un error de 3cat | Llegeix el missatge a **Activity → History**. Sovint és una restricció geogràfica: el servidor ha de sortir a internet des d'Espanya. |
