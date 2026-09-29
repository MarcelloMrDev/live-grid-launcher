# Teste Transmissão

Pacote isolado para debugar **só** a pipeline de live do Home Creators:

- Colar YouTube / M3U8 / Sports / qualquer URL
- Ver no browser (HLS local, DVR sem wipe de 6h do YouTube)
- Toggle gerar grids (2×2 / 8s)
- Logs no terminal + no dashboard

## Setup

1. Instalar [ffmpeg](https://ffmpeg.org/) no PATH
2. `pip install -r requirements.txt`
3. Duplo clique em `INICIAR.bat` **ou** `python run.py`
4. Abrir http://127.0.0.1:8877

## Uso rápido

1. Cola o link da transmissão de hoje
2. Marca/desmarca **Gerar grids**
3. **Iniciar** — acompanha o terminal
4. Se quebrar: copia o log + atualiza `PROBLEMAS.md`

## Conta / repo

Repo privado compartilhado só com o editor de testes.
Ver `PROBLEMAS.md` para o mapa dos bugs atuais.
