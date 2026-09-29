import subprocess
import json
import sys
from datetime import timedelta

URL_PADRAO = "https://www.youtube.com/watch?v=0wHWHAFnNh0"

HORAS_TESTE = [1, 6, 8, 12, 18, 24]


def executar(cmd):
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def obter_info(url):
    print("\n[1/2] Consultando a live no yt-dlp...")

    resultado = executar([
        "yt-dlp",
        "--live-from-start",
        "--dump-single-json",
        "--skip-download",
        url,
    ])

    if resultado.returncode != 0:
        print("\nERRO ao consultar a live:")
        print(resultado.stderr)
        sys.exit(1)

    try:
        return json.loads(resultado.stdout)
    except json.JSONDecodeError:
        print("Nao consegui interpretar a resposta do yt-dlp.")
        sys.exit(1)


def formatar_tempo(segundos):
    return str(timedelta(seconds=int(segundos)))


def analisar_formatos(info):
    print("\n[2/2] Analisando formatos/fragmentos...\n")

    print("=" * 65)
    print("TESTE DE HISTORICO DA LIVE")
    print("=" * 65)

    print("Titulo:", info.get("title"))
    print("ID:", info.get("id"))
    print("Status:", info.get("live_status"))
    print("Inicio/timestamp:", info.get("timestamp"))
    print()

    formatos = info.get("formats") or []

    candidatos = []

    for formato in formatos:

        if not isinstance(formato, dict):
            continue

        fragments = formato.get("fragments")

        if not isinstance(fragments, list):
            continue

        quantidade = len(fragments)

        if quantidade == 0:
            continue

        duracao_total = 0
        duracoes_validas = 0

        for fragmento in fragments:

            # Algumas versoes/formatos podem retornar strings
            if not isinstance(fragmento, dict):
                continue

            duracao = fragmento.get("duration")

            if isinstance(duracao, (int, float)):
                duracao_total += duracao
                duracoes_validas += 1

        # Caso os fragmentos nao tragam duracao individual
        if duracao_total == 0:

            fragment_duration = formato.get("fragment_duration")

            if isinstance(fragment_duration, (int, float)):
                duracao_total = quantidade * fragment_duration

        candidatos.append({
            "format_id": formato.get("format_id"),
            "ext": formato.get("ext"),
            "resolution": formato.get("resolution"),
            "fragments": quantidade,
            "seconds": duracao_total,
            "valid_durations": duracoes_validas,
        })

    if not candidatos:

        print("Nenhuma lista estatica utilizavel de fragmentos encontrada.")
        print()
        print("Isso significa que teremos que analisar o manifesto")
        print("dinamico da transmissao diretamente.")
        print()
        print("NAO significa que a live tenha pouco historico.")

        return

    candidatos.sort(
        key=lambda x: (x["seconds"], x["fragments"]),
        reverse=True
    )

    print("Formatos com fragmentos encontrados:\n")

    for item in candidatos[:10]:

        print(
            f"ID={item['format_id']} | "
            f"fragmentos={item['fragments']} | "
            f"duracao={formatar_tempo(item['seconds'])}"
        )

    melhor = candidatos[0]

    print()
    print("=" * 65)
    print("MAIOR HISTORICO DETECTADO")
    print("=" * 65)

    print("Format ID:", melhor["format_id"])
    print("Fragmentos:", melhor["fragments"])
    print("Duracao calculada:", formatar_tempo(melhor["seconds"]))

    if melhor["seconds"] <= 0:

        print()
        print("Os fragmentos existem, mas nao possuem duracao suficiente")
        print("para calcular o historico dessa forma.")
        print()
        print("Proximo passo: analisar manifesto dinamico.")

        return

    horas = melhor["seconds"] / 3600

    print()
    print("=" * 65)
    print("TESTES")
    print("=" * 65)

    for teste in HORAS_TESTE:

        if horas >= teste:
            print(f"{teste:>2}h atras : OK")
        else:
            print(f"{teste:>2}h atras : NAO CONFIRMADO")

    print()
    print(f"Historico detectado: {horas:.2f} horas")

    if horas >= 12:
        print("\n>>> SUCESSO: pelo menos 12 horas detectadas.")

    elif horas >= 6:
        print("\n>>> Passamos de 6 horas, mas ainda nao chegamos a 12.")

    else:
        print("\n>>> Ainda nao confirmamos mais de 6 horas.")


def main():

    url = sys.argv[1] if len(sys.argv) > 1 else URL_PADRAO

    print("=" * 65)
    print("DIAGNOSTICO DE HISTORICO YOUTUBE LIVE")
    print("=" * 65)
    print("URL:", url)

    info = obter_info(url)

    analisar_formatos(info)

    print("\nTeste finalizado.")


if __name__ == "__main__":
    main()