# V2 — download separado desde o início

## Iniciar e testar

1. Extraia o ZIP em uma nova pasta. Use o Python e o FFmpeg/ffprobe que já usa na V2.
2. Abra `INICIAR.bat` e acesse http://127.0.0.1:8877.
3. Na seção **Baixar live completa — desde o início disponível**, cole a URL e clique em **Baixar desde o início**. O DVR não precisa estar ligado.
4. Confira o progresso de cada faixa e abra **Logs deste download**. Em uma live longa, o total de fragments pode aparecer como desconhecido. Isso não significa ausência de histórico.
5. Clique em **Parar e finalizar este download**. Aguarde **Parado** e o link **Baixar arquivo MKV**. Abra em um player compatível, como VLC. Se a live terminar sozinha, o status será **Concluído**.
6. Para o teste real de 12h, use uma live com mais de 12h e histórico disponível. Deixe recuperar esse histórico antes de parar; o botão salva o que foi baixado até a parada, não garante recuperar tudo instantaneamente. Confira a duração, o conteúdo do começo e os logs.
7. Teste junto com o DVR: inicie captura e grids; pause o player, use **Início da live**, volte ao vivo e exporte MP4. Iniciar/parar o histórico não controla esses recursos. O botão **Parar** antigo continua parando apenas o DVR/player-DVR.

O caminho `historical_downloads/<id>/live.mkv` contém o arquivo final. `download.log` contém o log completo; o painel mostra as últimas linhas. `status.json` guarda progresso e resultado. Os arquivos de cada download ficam separados dos dados do DVR.

## Como funciona e limites

- Usa as opções nativas do yt-dlp `--live-from-start`, `bv+ba/b`, união/remux para MKV, e o downloader nativo de fragments. Não calcula uma janela fixa de 6h e não exige uma lista estática de fragments.
- A disponibilidade é determinada pelo YouTube/yt-dlp. Se o yt-dlp anunciar as últimas 120h em uma live 24/7, essa mensagem fica no log; 120h não é uma garantia nem um limite imposto pelo aplicativo. O modo ainda é experimental no yt-dlp: https://github.com/yt-dlp/yt-dlp#usage-and-options.
- Enquanto a live continua, o downloader pode alcançar o presente e esperar novos fragments. Não existe duração máxima nem timeout global de gravação; há timeout de 20s por requisição e até 10 tentativas para falhas de download/fragments. Falhas definitivas aparecem como erro, sem declarar um arquivo incompleto como sucesso.
- Processo separado, progresso por faixa, atualizações em disco e leitura limitada de logs evitam carregar a live inteira na memória. Um download histórico ativo por vez; DVR e histórico compartilham rede/disco, então acompanhe espaço e velocidade.
- A parada solicita uma única interrupção ao yt-dlp, que encerra as faixas DASH e executa a união nativa. Não mata FFmpeg durante a finalização. Pode demorar enquanto uma requisição termina ou a união de um arquivo grande ocorre.
- Só aparece um link de download após o ffprobe confirmar áudio, vídeo e duração positiva. MKV evita recodificar codecs incompatíveis com MP4. A exportação MP4 original permanece disponível para o DVR.
- Os arquivos das faixas são preservados, inclusive após sucesso, para permitir recuperação. Reserve espaço para as faixas e o MKV (aproximadamente duas vezes o tamanho da mídia), além do DVR. Não há limpeza automática.
- Para encerrar com segurança, pare pelo painel e aguarde o arquivo. Ctrl+C no launcher também solicita a parada do histórico; seu processo pode continuar finalizando em segundo plano. Fechar à força, desligar o computador ou esgotar o disco pode deixar apenas parciais. Eles não são apagados. Não há retomada automática após desligamento; um novo download cria outra pasta.
- Versão testada: yt-dlp 2026.08.19, Python 3.13, FFmpeg/ffprobe disponíveis no PATH. Se necessário, atualize com `python -m pip install -U yt-dlp`. A extração no YouTube depende também dos requisitos da versão instalada e das restrições da live.

## Validação realizada

`python -m unittest discover -s tests -v`: **10 testes aprovados**.

- Os 6 testes originais verificam recuperação de captura, preservação dos segmentos e exportação MP4 com áudio/vídeo real.
- Os 4 novos testes incluem término natural e parada segura no protocolo real `http_dash_segments_generator`, com áudio/vídeo DASH gerados localmente. Só a extração da URL foi substituída por uma fonte local; download, fragments, interrupção, união e verificação de mídia usam yt-dlp e FFmpeg reais. Ambos os MKVs foram decodificados pelo FFmpeg.
- Validação de URL, isolamento da solicitação de parada, limite da leitura de logs e rejeição de arquivo final só com áudio.
- Verificação do dashboard no navegador e download de mídia local através dos novos controles.
- **Não foi executado um teste de 12h/120h no YouTube nem um teste contínuo de muitas horas.** Os testes locais não comprovam a disponibilidade de histórico de uma live real. A navegação do player e os grids numa live real precisam ser conferidos com o roteiro acima.

## Arquivos alterados em relação ao ZIP original

Modificados:

- `run.py`: rotas independentes de iniciar/parar/status/download; solicitação de parada ao encerrar o launcher.
- `dashboard/index.html`: seção independente, controles, progresso por faixa, logs e link MKV.
- `live_engine.py`: **uma linha** adicionando `append_list` apenas na reconexão. O teste original falhava também no ZIP original porque a playlist perdia as entradas anteriores. A correção preserva essas entradas; o teste original agora passa.

Adicionados:

- `historical_download.py`: gerenciamento e estado persistente dos downloads.
- `historical_worker.py`: processo do yt-dlp, parada e validação do arquivo.
- `tests/test_historical_download.py`: testes locais da função nova.
- `LEIA-ME-HISTORICO.md`: este guia.

Todos os demais arquivos do ZIP original foram mantidos byte a byte, inclusive `INICIAR.bat`, `requirements.txt`, `youtube_player_dvr.py`, `recording_exports.py` e o teste original. Mídia e logs gerados nos testes não fazem parte do ZIP entregue.
