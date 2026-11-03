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
- Quan Sonarr busca pel títol, 3Cat Downloader busca primer un programa de 3cat amb
  **exactament el mateix títol**, sense tenir en compte accents, majúscules ni signes. Si no
  n'hi ha cap, busca a TheTVDB les sèries amb aquest títol exacte, any inclòs, i en mira el
  nom original. Per exemple, *Dirty Linen* és *Plats bruts*. Així no es baixa mai un
  programa equivocat.
- **Numeració**: quan la sèrie s'ha trobat a TheTVDB, els capítols es numeren com hi surten.
  Si 3cat ho té en una sola temporada i TheTVDB en diverses (o al revés), però el nombre de
  capítols és el mateix, es fan correspondre per ordre. Si no, cada capítol que no existeix a
  TheTVDB pren el número del capítol de TheTVDB emès el mateix dia.
- **Capítols nous**: la llista de novetats que Sonarr consulta cada pocs minuts fa servir el
  títol de TheTVDB i n'inclou l'identificador quan el programa ja s'ha identificat. Els
  programes nous s'identifiquen en segon pla: la primera vegada que surten encara porten el
  nom de 3cat, i a la sincronització següent ja porten el de TheTVDB. Informatius, magazins i
  esports no solen ser a TheTVDB i es queden amb el nom de 3cat.
- En una cerca de temporada surt també un **paquet de temporada** amb tots els capítols.
- Els programes diaris (informatius, magazins) es reconeixen per la data.

## Com troba les pel·lícules

- Radarr busca primer per l'**identificador de TMDB** (o d'IMDb). 3Cat Downloader en demana
  el títol original i les traduccions al català i al castellà al servei de metadades de
  Radarr, i busca al catàleg de pel·lícules de 3cat la que té un d'aquests títols. Així
  *13 Minutes* a Radarr és *13 minuts per matar Hitler* a 3cat.
- Quan Radarr busca pel títol i l'any, primer es busca una pel·lícula de 3cat amb aquest
  títol exacte. Si no n'hi ha cap, es busca a TMDB la pel·lícula amb aquest títol i any, i
  després se'n fan servir les traduccions.
- Els resultats porten el títol i l'any de Radarr i els identificadors de TMDB i IMDb.

## Serveis externs i registres

- Les cerques per identificador i per títol de TheTVDB/TMDB necessiten que el servidor pugui
  accedir a `api.tvmaze.com`, `skyhook.sonarr.tv` i `api.radarr.video`. Si no hi arriba,
  només funcionen els títols idèntics als de 3cat.
- El que es troba es desa a `arr_metadata.json`, a la carpeta de configuració. Així, després
  d'un reinici no cal tornar a consultar res, i una caiguda d'aquests serveis no afecta el
  que ja es coneix. Pots esborrar el fitxer sense por: es torna a crear.
- Cada cerca de Sonarr i Radarr queda als registres de 3Cat Downloader, amb el nombre de
  resultats: `Newznab t=tvsearch q='Dirty Linen' … -> 14 resultats`.
- Si una sèrie o pel·lícula no es troba, sempre la pots descarregar des de la pàgina de
  3Cat Downloader.

## Problemes habituals

| Missatge | Solució |
|---|---|
| Una sèrie o pel·lícula de 3cat no surt a la cerca | Mira els registres de 3Cat Downloader. Si no hi apareix cap línia `Newznab`, Sonarr o Radarr no arriben a l'indexador. Si diu `No s'ha pogut consultar …`, el servidor no té accés a internet o al DNS. Si acabes d'actualitzar, reinicia Sonarr i Radarr perquè tornin a llegir què admet l'indexador. |
| Sonarr baixa un capítol amb un número equivocat | La numeració de 3cat i la de TheTVDB no es corresponen i no s'han pogut fer coincidir. Fes servir la cerca interactiva i tria'l a mà. |
| *Unable to connect* | Comprova que 3Cat Downloader està en mode servidor i que el host i el port són correctes. |
| *API Key Incorrect* | Copia de nou la clau d'API des de Configuració. |
| La importació no troba els fitxers | Sonarr no veu la carpeta al mateix camí. Fes servir la mateixa carpeta `/data` als dos contenidors, o configura un **Remote Path Mapping** a Sonarr. |
| La descàrrega falla amb un error de 3cat | Llegeix el missatge a **Activity → History**. Sovint és una restricció geogràfica: el servidor ha de sortir a internet des d'Espanya. |
