# Windows

## Instal·lador (recomanat)

1. Descarrega `3CatDownloader-x.y.z-setup.exe` de la pàgina de
   [versions](https://github.com/Uriracer/3cat-downloader/releases/latest).
2. Executa'l. No cal ser administrador: s'instal·la només per al teu usuari.
3. Obre **3Cat Downloader** des del menú d'inici.

Windows SmartScreen pot avisar que l'aplicació no és coneguda, perquè no està signada.
Prem «Més informació» i després «Executa igualment».

## Versió portable

Descarrega `3CatDownloader-x.y.z-windows-x64.zip`, descomprimeix-lo on vulguis i executa
`3CatDownloader.exe`.

## On es desa tot

| Què | On |
|---|---|
| Vídeos | `Vídeos\3Cat` (es pot canviar a Configuració) |
| Configuració, cua i registres | `%APPDATA%\3CatDownloader\` |

Desinstal·lar el programa no esborra ni la configuració ni els vídeos.

## Requisits

- Windows 10 o 11 de 64 bits.
- Microsoft Edge WebView2 Runtime, que ja ve amb Windows 11. Si no hi és, la interfície
  s'obre al navegador.

## Fer-lo accessible des d'altres dispositius

L'aplicació d'escriptori només escolta al mateix ordinador. Si la vols fer servir des del
mòbil, arrenca-la en mode servidor des de PowerShell. Demanarà contrasenya.

```powershell
& "$env:LOCALAPPDATA\Programs\3Cat Downloader\3CatDownloader.exe" --host 0.0.0.0
```

En mode servidor no s'obre cap finestra. Obre `http://<ip-de-l'ordinador>:8765/` des de
l'altre dispositiu. Windows et pot demanar permís al tallafoc. Per aturar-lo, tanca
`3CatDownloader.exe` des de l'Administrador de tasques.

Per a un ús continu és millor un servidor: mira [docker.md](docker.md) o [proxmox.md](proxmox.md).
