# Submission di Ugo.Agent al catalogo winget-pkgs

Come portare il pacchetto nel catalogo community di winget, così che per tutti
funzioni semplicemente:

```bat
winget install Ugo.Agent
```

Stato del kit (tutto già preparato in questa repo):

| Cosa | Dove | Stato |
|---|---|---|
| Manifest 1.6.0 validati | `packaging/winget-pkgs/0.3.2/` | ✅ `winget validate` OK |
| Installer pubblicato + SHA256 | release v0.3.2 (asset zip) | ✅ hash verificato all'installazione reale |
| File LICENSE (MIT) | `/LICENSE` | ✅ aggiunto |
| Testo PR / issue | questo file | ✅ pronto da copiare |

## 1. Prerequisiti (una tantum)

- **Account GitHub** con 2FA attivo (richiesto dal catalogo)
- **git** e (consigliato) **GitHub CLI**: `winget install GitHub.cli`
- Il repo `giovannimazza/ugo-local-ai-agent` è **pubblico** ✅ e ora ha il LICENSE ✅

## 2. Verifica identità del publisher (richiesta dal bot una volta)

Apri una issue nel repo `microsoft/winget-pkgs` col titolo esatto:

```
Publisher request: Giovanni Mazza <giovanni@example.com>
```

(nei manifest deve comparire l'email che usi per i commit GitHub; sostituisci
`giovanni@example.com` con quella vera). Nel corpo dichiari che sei il titolare
del "publisher name" Giovanni Mazza e che i pacchetti sono firmati/pubblicati
dalla stessa identità (link al repo e alla release v0.3.2). Non serve screenshot
per domini personali: il profilo GitHub + repo pubblico bastano come verifica.

## 3. Fork e struttura

```bash
gh repo fork microsoft/winget-pkgs --clone
cd winget-pkgs
git checkout -b ugo-agent-0.3.2
mkdir -p manifests/u/Ugo/Agent/0.3.2
cp <percorso>/ugo-local-ai-agent/packaging/winget-pkgs/0.3.2/*.yaml \
   manifests/u/Ugo/Agent/0.3.2/
```

La cartella **deve** rispecchiare l'identificatore: `u` (iniziale del
publisher) / `Ugo` / `Agent` / `0.3.2`, e i file si chiamano
`Ugo.Agent.yaml`, `Ugo.Agent.defaultLocale.yaml`, `Ugo.Agent.installer.yaml`.

## 4. Commit e PR

```bash
git add manifests/u/Ugo/Agent/0.3.2
git commit -m "Add Ugo.Agent version 0.3.2"
git push -u origin ugo-agent-0.3.2
gh pr create --repo microsoft/winget-pkgs \
  --title "Add Ugo.Agent version 0.3.2" \
  --body-file <percorso>/ugo-local-ai-agent/packaging/winget-pkgs/PR_BODY.md
```

## 5. Validation bot e review

1. Appena aperta la PR, un bot applica l'etichetta **Validation- Automated** e
   scarica/verifica davvero l'installer (URL raggiungibile, SHA256 combaciante,
   installazione pulita). Il tempo di coda varia: controlla la PR ogni tanto.
2. Se il bot trova problemi, commenta lui stesso i fix da fare; correggi e
   pusha nello stesso branch (la PR si aggiorna da sola).
3. Poi passa a un revisore umano (`Microsoft- winget-pkgs`): di solito chiedono
   conferma della identità publisher (punto 2) e che i metadati combacino con
   il prodotto. Rispondi direttamente nei commenti della PR.

## 6. Dopo il merge

- `winget install Ugo.Agent` funziona per TUTTI (niente più manifest locali,
  niente `LocalManifestFiles`)
- **Auto-update del catalogo**: ogni release futura con asset + manifest
  allineati (il nostro workflow di release li genera già a ogni tag `v*`) può
  essere aggiornata nel catalogo con una nuova PR, oppure si può abilitare
  [Microsoft.WinGet.PKU](https://github.com/microsoft/winget-pkgs#adding-your-package-to-the-winget-community-repo)
  (pull-upstream) per farlo automaticamente
- Aggiornare in futuro = ripetere il punto 3 con la nuova cartella di versione

## Note di conformità già soddisfatte

- ✅ Nessun file ridondante: i manifest stanno SOLO in winget-pkgs, qui resta il kit
- ✅ Installer scaricabile da URL pubblico permanente (GitHub Releases)
- ✅ SHA256 esatto e riproducibile (zip deterministico nella build)
- ✅ `ElevationRequirement: elevationProhibited` (portable, testato reale)
- ✅ Nessuna telemetria nel prodotto (dichiarabile in review senza problem)
- ✅ Licenza MIT esplicita con file LICENSE nel repo
