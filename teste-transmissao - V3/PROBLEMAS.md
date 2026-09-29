# Problemas da transmissão (estado atual → o que este teste isola)

## Atualização após os testes — 26/09/2026

- [x] Áudio separado do yt-dlp: duas entradas preservadas e mapeadas no FFmpeg; HLS com H.264 e AAC.
- [x] Player: reprodução explícita com som, reconexão e mensagens de estado. Confirmado pelo usuário.
- [x] Recuperação automática após saída **não zero** do FFmpeg: nova resolução da URL, até **5 tentativas por sessão**, espera de 3/6/9/12/15 segundos. Estado `reconnecting` no dashboard.
- [x] Continuação do HLS: `append_list`, nomes diferentes por tentativa, discontinuidades e sequência preservada; arquivos antigos não são substituídos.
- [x] STOP cancela reconexões e solicita encerramento ao FFmpeg antes de fechar a playlist. Sessão encerrada continua visível.
- [x] Exportação MP4: selecionar gravação e clicar **Exportar MP4**. Executa em segundo plano e disponibiliza **Baixar** quando pronto. Arquivos em `data/<id>/exports/`.
- [x] Exportação durante a live usa apenas os segmentos concluídos no momento do clique. Não altera nem apaga o HLS. Para exportar tudo, primeiro parar a captura.
- [x] Gravações anteriores e MP4 concluídos são listados mesmo após reiniciar o servidor.
- [ ] Validar reconexão em transmissão real do YouTube/Sports e sincronismo depois da queda.
- [x] Alerta de disco no dashboard, inclusive sem sessão ativa: amarelo abaixo de 10 GB livres e vermelho abaixo de 2 GB (GB decimais). Consulta o volume de data a cada atualização de status (~2,5s), sem varrer arquivos, apagar ou parar a captura. Falha de consulta aparece como indisponível.
- [ ] Teste DVR superior a 6 horas e robustez adicional dos grids.

Limites: término do FFmpeg com código zero é tratado como fim normal; recuperação não cobre reinício/crash do Python. Uma URL M3U8 direta é reutilizada — para renovar tokens, fornecer a página original que o yt-dlp resolve. Se o processo permanecer vivo sem produzir segmentos, ainda não existe detector de travamento. MP4 usa remux sem recodificação; mudanças de codecs entre tentativas podem exigir tratamento adicional. Manter o servidor aberto durante a exportação. MP4 parcial nunca é oferecido para download. O trecho sem sinal durante uma queda não pode ser recuperado automaticamente.

Testes automatizados: `python -m unittest discover -s tests -v` (FFmpeg e ffprobe no PATH, ou TEST_FFMPEG/TEST_FFPROBE com caminhos completos). Incluem mídia real gerada para testar append, preservação de segmentos, MP4 com áudio e vídeo, limite de tentativas e cancelamento durante resolução. Validação executada: seis testes novos e quatro de regressão do áudio passaram. A sessão teste_1790464043 foi exportada pelo dashboard; MP4 com H.264/AAC, duração de 134,58s, download validado por hash. Houve ajuste de timestamp numa fronteira de segmentos da gravação antiga; a decodificação completa terminou com código zero, mas sincronismo perceptível ainda deve ser conferido pelo usuário. Os tópicos abaixo registram o diagnóstico original; esta seção descreve o estado atualizado.

---

Documento para o editor (Marcello) debugar **só** a pipeline de live.
Nada de auth, API cloud, Workers, etc. — só URL → stream → HLS → grids.

---

## Como rodar

```bat
INICIAR.bat
```

ou:

```bash
pip install -r requirements.txt
python run.py
```

Abre **http://127.0.0.1:8877**, cola o link, olha o **terminal** (log completo) + painel Log no dashboard.
(8765 é do Home Scraper — este teste usa **8877**.)

Dependências de sistema: **ffmpeg** e **yt-dlp** no PATH (ou `pip install yt-dlp`).

---

## Fluxo esperado (espelho do Home Creators)

```
URL (YouTube / M3U8 / Sports / DaddyLive / …)
  → yt-dlp -g          (resolve URL direta do stream)
  → ffmpeg             (re-encode → HLS EVENT, seg 2s, list_size=0)
  → live.m3u8 local    (player hls.js no dashboard)
  → a cada 2s: frame
  → a cada 4 frames (8s): grid 2×2 (xstack)
  → toggle: gerar grids ON/OFF sem matar a live
```

Números (iguais à produção em espírito):

| Constante | Valor | Nota |
|-----------|-------|------|
| `hls_time` | 2s | segmento |
| frames/grid | 4 | 4×2s = **8s** por grid |
| batch log | a cada 5 grids | ~40s (safe); docs antigas citam ~32s |

---

## Problema 1 — YouTube DVR / “wipe” de ~6 horas

**Sintoma:** no player do YouTube (ou embed), só dá para voltar ~6h. Live de 20h não dá para scrubar o começo.

**Causa:** limite do próprio YouTube (DVR window), não do nosso código.

**O que este teste faz:** grava **HLS EVENT local** com `hls_list_size=0` desde o `START`. O `live.m3u8` + `seg_*.ts` no disco crescem sem wipe. Scrub / download local não dependem da janela do YouTube.

**Ainda quebra se:**
- a sessão Python cair e reiniciar (partes novas — ver Problema 3);
- disco encher (20h × bitrate ≈ vários GB);
- o ffmpeg morrer no meio (rede / source cai).

**Como validar:** deixar rodar >6h, confirmar que `data/<id>/hls/` ainda lista segmentos do início e o player consegue seek para trás via playlist local.

---

## Problema 2 — M3U8 “salvando por partes” / vídeo final inconsistente

**Sintoma na produção:** captura reinicia, cada restart vira `.partN.mkv`, remux final falha ou fica incompleto; HLS às vezes sem `#EXT-X-ENDLIST`.

**Causa (código real `servidor_live_capture.py`):**
- Matroska **não** faz append → cada restart do ffmpeg = parte nova.
- Playlist EVENT sem fechar (`ENDLIST`) → remux trava esperando live.
- Sports/M3U8 de terceiros mudam URL / token e o ffmpeg cai.

**Neste teste:** só HLS (sem MKV paralelo) para isolar. No `STOP` escrevemos `#EXT-X-ENDLIST`. Se o ffmpeg cair, status vira `dead` e o log mostra o código.

**Checklist debug:**
1. Abrir `logs/<id>_ffmpeg.log` — EOF? 403? 404 no source?
2. Contar `seg_*.ts` vs duração esperada.
3. Se source for M3U8 de Sports: URL expirou? Precisa re-resolver yt-dlp periodicamente? (hoje **não** re-resolve — gap conhecido.)

---

## Problema 3 — yt-dlp devolve vídeo + áudio separados

**Sintoma:** live sem áudio, ou ffmpeg “Stream map '0:a:0' matches no streams”.

**Causa:** `-g` com `bv*+ba` pode imprimir **duas** URLs (video-only + audio-only). O engine atual pega **uma** URL (prefere m3u8).

**Fix candidato:** passar `-i video -i audio -map 0:v -map 1:a` ou forçar formato já muxado (`-f b` / `best`).

**Como ver no log:** linha `AVISO: yt-dlp devolveu video+audio separados`.

---

## Problema 4 — Grids não geram / atrasam / falham

**Sintoma:** live toca, mas `grids_done=0` ou frames falham.

**Causas comuns:**
1. `generate_grids=False` (toggle).
2. Grab no `live.m3u8` EVENT **aberto** — ffmpeg às vezes espera; em produção usam playlist `_grab.m3u8` limpa / último `.ts` fechado.
3. Seek `-ss` antes do segmento existir (live ainda não chegou naquele `t`).
4. `xstack` falha se algum frame <400 bytes.

**Neste teste:** grab direto no m3u8 + retry; log por frame/grid. Se falhar muito, próximo passo = copiar `_write_grab_playlist` / `_grab_live_frame` do `servidor_live_capture.py` (lógica de “último .ts fechado”).

**Ritmo safe:** 1 grid / 8s; checkpoint log a cada 5 grids. Não precisa ser mais agressivo no browser — o gargalo é ffmpeg local.

---

## Problema 5 — Sports / links não-YouTube

**Sintoma:** yt-dlp falha ou resolve URL que morre em minutos (token CDN).

**Debug:**
```bat
yt-dlp -g -f "bv*+ba/b" "URL_COLADA"
ffmpeg -i "URL_DIRETA" -t 10 -c copy test.ts
```

Se o `-g` funciona mas a live cai aos 5–15 min → **re-resolve + restart** (não implementado aqui de propósito — é o bug a observar).

---

## Problema 6 — Player no browser não sobe

**Checklist:**
- `status=live` mas sem vídeo → abrir `/media/<id>/hls/live.m3u8` no browser: tem `#EXTINF`?
- CORS: server manda `Access-Control-Allow-Origin: *`.
- hls.js CDN bloqueado? (rede).
- Windows Defender / antivírus lockando `.ts` no `data/`.

---

## O que NÃO está neste pacote (de propósito)

- Auth / JWT / API cloud / Workers / Pages
- DaddyLive scraper específico (só URL genérica + yt-dlp)
- Remux final MP4/MKV de produção
- UI do Home Creators (Reels, Render, etc.)

Objetivo: o editor cola o **mesmo link do dia**, vê **onde** quebra (resolve / ffmpeg / grab / grid), e manda o trecho do log + `PROBLEMAS.md` atualizado.

---

## Arquivos-chave

| Arquivo | Papel |
|---------|--------|
| `run.py` | HTTP + dashboard + log no terminal |
| `live_engine.py` | yt-dlp → ffmpeg HLS → frames → grids |
| `dashboard/index.html` | UI: URL, toggle grids, player, log, thumbs |
| `data/<id>/hls/` | `live.m3u8` + `seg_*.ts` (DVR local) |
| `data/<id>/grids/` | `grid_XXXXX.jpg` |
| `logs/` | log da sessão + stderr do ffmpeg |

---

## Prioridades originais — consultar atualização no início

1. **Re-resolve periódico** do yt-dlp quando ffmpeg exit ≠ 0 (Sports/token).
2. **Dual-input** video+audio quando `-g` retorna 2 URLs.
3. **Grab robusto** via último `.ts` fechado (copiar de produção).
4. **Remux** `data/<id>/hls` → MP4 no STOP (opcional, para validar “salvou o vídeo”).
5. Medir disco: alertar se `bytes` > N GB em lives longas.
