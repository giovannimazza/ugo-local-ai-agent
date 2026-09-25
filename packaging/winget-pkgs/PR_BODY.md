# Add Ugo.Agent version 0.3.2

## What does this PR add?

This PR adds **Ugo.Agent 0.3.2** — Ugo, a 100% local and offline voice
assistant for Windows (faster-whisper STT, Qwen2.5 via Ollama, Piper TTS —
everything runs on-device, no cloud, no telemetry).

- **Package homepage**: https://github.com/giovannimazza/ugo-local-ai-agent
- **Installer URL**: https://github.com/giovannimazza/ugo-local-ai-agent/releases/download/v0.3.2/ugo-0.3.2-winget.zip
- **Installer type**: zip with a portable `ugo.exe` (command alias `ugo`,
  `ElevationRequirement: elevationProhibited`)
- **Installer SHA256**: `f27c34ba797491fa8e99b5e27502b77faaa79171a4d9cf94722315a4f4fb661a`
- **License**: MIT, with a LICENSE file in the repository root:
  https://github.com/giovannimazza/ugo-local-ai-agent/blob/main/LICENSE
- **Moniker**: `ugo`

## Checklist

- [x] Manifests validated locally with `winget validate` (schema 1.6.0)
- [x] Installer hash is correct and the zip was verified by a real
      installation from these exact manifests
- [x] Package version matches the tagged release `v0.3.2` and the
      pyproject version
- [x] The installer URL is stable and public (GitHub Release asset)
- [x] Publisher identity: Giovanni Mazza, GitHub profile
      https://github.com/giovannimazza (owner of the repository and of the
      release); publisher request issue to follow
- [x] No redundant manifests in my own repository: the copy in this repo is
      build tooling; the only submission to the catalog is this PR
- [x] No telemetry, no account requirement, on-device execution
- [x] Contains no malware / unwanted software: the zip contains a single
      ~9 KB open-source launcher (`boot/ugo_bootstrap.cs` compiles it, source
      in the repo); on first run it provisions Python/pip packages and local
      open-source models (Whisper CT2, Qwen2.5 via Ollama, Piper voices),
      exactly as documented in the README

## Have you verified the package installs correctly?

Yes — installed and uninstalled on Windows 11 (x64) with winget 1.29.380
using these exact manifests (`winget install --manifest`), alias `ugo`
created on the PATH, `ugo.exe version` → `ugo-agent 0.3.2`, portable ARP
entry visible in `winget list`.
