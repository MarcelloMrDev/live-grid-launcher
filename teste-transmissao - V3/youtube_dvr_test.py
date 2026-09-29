#!/usr/bin/env python3
"""PoC independente: amostra finita dos fragmentos DVR fornecidos pelo yt-dlp.
Não importa nenhum módulo do projeto e não acessa seu live.m3u8.
"""
from __future__ import annotations
import argparse
import itertools
import json
import math
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs


class TestFailure(Exception):
    def __init__(self, case, message):
        self.case = case
        super().__init__(message)


def redact(message):
    return re.sub(r'https?://[^\s\]"<>]+', '[URL omitida]', str(message))


class Logger:
    def __init__(self, path): self.path = path
    def write(self, message):
        line = f'{datetime.now(timezone.utc).isoformat()} {redact(message)}'
        print(line, flush=True)
        with self.path.open('a', encoding='utf-8') as out: out.write(line + '\n')
    def debug(self, message):
        if not message.startswith('[debug]'): self.write(message)
    info = write
    warning = write
    error = write


def youtube_url(url):
    u = urlparse(url)
    host = (u.hostname or '').lower()
    if u.scheme not in ('https', 'http') or host not in (
        'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be', 'www.youtu.be'):
        raise TestFailure('not_youtube', 'Informe uma URL de vídeo/live do YouTube.')
    video_id = (u.path.strip('/') if host.endswith('youtu.be') else
                parse_qs(u.query).get('v', [''])[0] if u.path == '/watch' else
                u.path.split('/')[2] if u.path.startswith(('/live/', '/shorts/')) else '')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{11}', video_id):
        raise TestFailure('invalid_url', 'Use o link direto da live, com o ID do vídeo.')
    return f'https://www.youtube.com/watch?v={video_id}'


def classify(info):
    state = info.get('live_status')
    if state == 'is_upcoming': return 'upcoming'
    if state in ('was_live', 'post_live') or info.get('was_live'): return 'ended'
    if state == 'is_live' or info.get('is_live'): return 'live'
    if state == 'not_live' or info.get('is_live') is False: return 'not_live'
    return 'unknown_live_status'


def format_summary(fmt):
    return {k: fmt.get(k) for k in (
        'format_id', 'protocol', 'is_from_start', 'target_duration',
        'vcodec', 'acodec', 'height', 'ext', 'format_note')}


def choose_formats(formats):
    candidates = [f for f in formats if f.get('is_from_start')
                  and f.get('protocol') == 'http_dash_segments_generator'
                  and callable(f.get('fragments')) and float(f.get('target_duration') or 0) > 0]
    videos = [f for f in candidates if str(f.get('vcodec', '')).startswith('avc1')
              and f.get('acodec') == 'none' and 0 < (f.get('height') or 0) <= 720]
    audios = [f for f in candidates if str(f.get('acodec', '')).startswith('mp4a')
              and f.get('vcodec') == 'none']
    if not videos or not audios:
        raise TestFailure('history_unavailable',
            'Não foram oferecidos formatos DVR nativos compatíveis de vídeo H.264 e áudio AAC. '
            'Isso não prova que o DVR do navegador está desativado; HLS/SABR e outras rotas não são testadas nesta PoC.')
    video = min(videos, key=lambda f: (abs((f.get('height') or 0) - 360), f.get('fps') or 0))
    audio = max(audios, key=lambda f: f.get('abr') or 0)
    return [video, audio]


def finite_fragments(fmt, seconds, start, head):
    duration = float(fmt['target_duration'])
    count = math.ceil(seconds / duration) + 1
    if count > 40:
        raise TestFailure('request_limit', 'Amostra exigiria mais de 40 fragmentos por faixa; reduza --seconds.')
    generator = fmt['fragments']({'start': start, 'fragment_index': 0})
    try:
        first = next(generator)
        first_seq = int(parse_qs(urlparse(first['url']).query)['sq'][0])
        advertised_head = int(first.get('fragment_count', head + 1)) - 1
        edge = min(head, advertised_head)
        # Não chegar à borda e não deixar o gerador esperar novos fragmentos.
        if first_seq + count >= edge:
            raise TestFailure('history_too_short', 'Histórico anunciado é curto demais para separar a amostra da borda ao vivo.')
        fragments = [first] + list(itertools.islice(generator, count - 1))
        sequences = [int(parse_qs(urlparse(f['url']).query)['sq'][0]) for f in fragments]
        if sequences != list(range(first_seq, first_seq + count)):
            raise TestFailure('history_unavailable', 'Sequência de fragmentos incompleta; não será tratada como prova.')
        return fragments, {
            'format_id': fmt['format_id'], 'first_sequence': first_seq,
            'last_sample_sequence': sequences[-1], 'live_edge_sequence': edge,
            'target_duration_sec': duration, 'fragment_count_sample': count,
            'available_history_estimate_sec': (edge - first_seq + 1) * duration,
            'sample_distance_from_edge_estimate_sec': (edge - sequences[-1]) * duration,
        }
    finally:
        if hasattr(generator, 'close'): generator.close()


def resolve_binary(value, default):
    candidate = Path(value) if value else None
    if candidate and candidate.is_dir(): candidate = candidate / (default + ('.exe' if sys.platform == 'win32' else ''))
    found = str(candidate) if candidate and candidate.is_file() else shutil.which(value or default)
    if not found: raise TestFailure('ffmpeg_error', f'{default} não encontrado. Use --{default} com o caminho completo.')
    return found


def make_sample(video, audio, output, seconds, ffmpeg, ffprobe, log_path):
    with log_path.open('a', encoding='utf-8') as log:
        result = subprocess.run([
            ffmpeg, '-hide_banner', '-loglevel', 'warning', '-nostdin', '-n',
            '-i', str(video), '-i', str(audio), '-map', '0:v:0', '-map', '1:a:0',
            '-t', str(seconds), '-c', 'copy', '-movflags', '+faststart', str(output),
        ], stdout=log, stderr=log, timeout=90)
    if result.returncode:
        raise TestFailure('ffmpeg_error', 'FFmpeg não conseguiu montar a amostra; consulte test.log.')
    result = subprocess.run([ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(output)],
                            capture_output=True, text=True, timeout=30)
    if result.returncode: raise TestFailure('ffmpeg_error', 'ffprobe não conseguiu validar o MP4.')
    data = json.loads(result.stdout)
    kinds = {s['codec_type'] for s in data.get('streams', [])}
    duration = float(data.get('format', {}).get('duration') or 0)
    if not {'video', 'audio'} <= kinds or not seconds * .8 <= duration <= seconds + 5:
        raise TestFailure('ffmpeg_error', 'MP4 não possui ambas as faixas ou a duração esperada.')
    with log_path.open('a', encoding='utf-8') as log:
        result = subprocess.run([ffmpeg, '-hide_banner', '-v', 'error', '-xerror', '-nostdin',
                                 '-i', str(output), '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'],
                                stdout=log, stderr=log, timeout=90)
    if result.returncode: raise TestFailure('ffmpeg_error', 'Amostra gerada, mas falhou na decodificação completa.')
    return {'duration_sec': duration, 'streams': [
        {k: s.get(k) for k in ('codec_type', 'codec_name', 'start_time', 'duration', 'width', 'height')}
        for s in data['streams']]}


def run(args, report, log):
    import yt_dlp
    from yt_dlp.version import __version__
    from yt_dlp.networking import Request
    from yt_dlp.downloader.dash import DashSegmentsFD
    report['yt_dlp_version'] = __version__
    url = youtube_url(args.url)
    report['url'] = url
    ffmpeg = resolve_binary(args.ffmpeg, 'ffmpeg')
    ffprobe = resolve_binary(args.ffprobe, 'ffprobe')
    start = time.time()
    options = dict(quiet=True, logger=log, noplaylist=True, live_from_start=True,
                   socket_timeout=10, retries=0, fragment_retries=0, extractor_retries=0,
                   concurrent_fragment_downloads=1, skip_unavailable_fragments=False, noprogress=True,
                   continuedl=False, nopart=False, fixup='never', cachedir=False)
    def byte_limit(status):
        if status.get('downloaded_bytes', 0) > 64 * 1024 * 1024:
            raise TestFailure('download_limit', 'Limite de 64 MiB por faixa atingido.')
    options['progress_hooks'] = [byte_limit]
    class BoundedYoutubeDL(yt_dlp.YoutubeDL):
        # Limite global adicional: metadados + HEADs + amostra, sem loops ilimitados.
        requests = 0
        def urlopen(self, request):
            if self.requests >= 100 or time.time() - start > 240:
                raise TestFailure('request_limit', 'Limite global de requisições/tempo atingido.')
            self.requests += 1
            report['http_requests'] = self.requests
            return super().urlopen(request)

    with BoundedYoutubeDL(options) as ydl:
        log.write('Analisando metadados com live_from_start=True; nenhum download integral será iniciado.')
        try:
            info = ydl.extract_info(url, download=False, process=False)
        except yt_dlp.utils.ExtractorError as exc:
            raise TestFailure('yt_dlp_error', redact(exc)) from exc
        if not isinstance(info, dict): raise TestFailure('yt_dlp_error', 'Metadados ausentes.')
        report['metadata'] = {k: info.get(k) for k in ('id', 'title', 'live_status', 'is_live', 'was_live', 'release_timestamp', 'duration')}
        report['formats'] = [format_summary(f) for f in info.get('formats', [])]
        state = classify(info)
        report['live_state'] = state
        if state != 'live':
            labels = {'ended': 'Live encerrada; não prova recuperação DVR durante transmissão.',
                      'upcoming': 'Live agendada, ainda não começou.', 'not_live': 'URL de vídeo que não está em transmissão ao vivo.'}
            raise TestFailure(state, labels.get(state, 'Não foi possível confirmar que a URL está ao vivo.'))
        if info.get('is_live_dvr_enabled') is False:
            raise TestFailure('no_dvr', 'Metadados indicam explicitamente DVR desativado.')
        chosen = choose_formats(info.get('formats', []))
        report['selected_formats'] = [format_summary(f) for f in chosen]
        # HEAD só consulta a URL normal oferecida pelo extrator; não inventa tokens ou caminhos.
        try:
            with ydl.urlopen(Request(chosen[0]['url'], method='HEAD', headers=chosen[0].get('http_headers', {}))) as response:
                head = int(response.headers['X-Head-Seqnum'])
        except Exception as exc:
            raise TestFailure('history_unavailable', 'Não foi possível ler X-Head-Seqnum: ' + redact(exc)) from exc
        report['head_sequence_observed'] = head
        report['tracks'] = []
        plans = []
        for fmt in chosen:
            try: fragments, evidence = finite_fragments(fmt, args.seconds, start, head)
            except TestFailure: raise
            except Exception as exc: raise TestFailure('history_unavailable', 'Falha no gerador DVR: ' + redact(exc)) from exc
            report['tracks'].append(evidence)
            plans.append((fmt, fragments))
        if report['tracks'][0]['first_sequence'] != report['tracks'][1]['first_sequence'] or chosen[0]['target_duration'] != chosen[1]['target_duration']:
            raise TestFailure('history_unavailable', 'Inícios de vídeo e áudio não coincidem; alinhamento não comprovado.')
        report['earliest_exposed_sequence'] = report['tracks'][0]['first_sequence']
        report['beginning_outside_exposed_window'] = report['earliest_exposed_sequence'] > 0
        report['estimated_history_sec'] = min(t['available_history_estimate_sec'] for t in report['tracks'])
        log.write(f"Histórico anunciado: aproximadamente {report['estimated_history_sec'] / 60:.1f} minutos; "
                  f"primeira sequência {report['earliest_exposed_sequence']}, borda {head}.")
        if args.analyze_only:
            report.update(case='analysis_only', conclusion='C', message='Somente análise: fragmentos anunciados, mas acesso/MP4 não comprovados.')
            return
        paths = []
        for index, (fmt, fragments) in enumerate(plans):
            path = args.output / ('video_historico.mp4' if index == 0 else 'audio_historico.m4a')
            finite = {**info, **fmt, 'fragments': fragments, 'protocol': 'http_dash_segments',
                      'is_live': False, 'is_from_start': False}
            for key in ('requested_formats', 'requested_downloads'): finite.pop(key, None)
            log.write(f"Baixando apenas {len(fragments)} fragmentos da faixa {index + 1}.")
            try:
                ok, _ = DashSegmentsFD(ydl, options).download(str(path), finite)
            except TestFailure: raise
            except Exception as exc:
                report['failed_first_sequence'] = report['tracks'][index]['first_sequence']
                raise TestFailure('history_unavailable', 'Fragmentos históricos anunciados não puderam ser baixados: ' + redact(exc)) from exc
            if not ok: raise TestFailure('history_unavailable', 'Download histórico não foi concluído; sem ignorar fragmentos ausentes.')
            paths.append(path)
        output = args.output / 'inicio_teste.mp4'
        report['sample'] = make_sample(*paths, output, args.seconds, ffmpeg, ffprobe, args.output/'test.log')
        partial = report['beginning_outside_exposed_window']
        report.update(case='partial_history' if partial else 'dvr_accessible', conclusion='B' if partial else 'A',
                      message=('Acessamos parte do histórico; a sequência inicial da live não foi oferecida.' if partial else
                               'Acessamos a sequência zero e baixamos uma amostra histórica com vídeo e áudio.'),
                      sample_file=str(output), proved_historical=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument('--seconds', type=int, choices=(30, 60), default=30)
    parser.add_argument('--output', type=Path, default=None)
    parser.add_argument('--ffmpeg', help='Executável ou pasta bin do FFmpeg')
    parser.add_argument('--ffprobe', help='Executável ou pasta bin do ffprobe')
    parser.add_argument('--analyze-only', action='store_true')
    args = parser.parse_args()
    args.output = (args.output or Path('youtube_dvr_test')/datetime.now().strftime('%Y%m%d_%H%M%S')).resolve()
    if args.output.exists() and any(args.output.iterdir()): parser.error('--output precisa ser uma pasta nova ou vazia para preservar testes anteriores')
    args.output.mkdir(parents=True, exist_ok=True)
    log = Logger(args.output/'test.log')
    report = {'started_utc': datetime.now(timezone.utc).isoformat(), 'proved_historical': False,
              'conclusion': 'C', 'requested_seconds': args.seconds,
              'scope': 'DASH live-from-start nativo; sem sondagem de sequências não oferecidas; sem integração ao projeto'}
    try:
        run(args, report, log)
    except TestFailure as exc: report.update(case=exc.case, message=str(exc))
    except subprocess.TimeoutExpired: report.update(case='ffmpeg_error', message='Tempo limite do FFmpeg/ffprobe atingido.')
    except ImportError as exc: report.update(case='dependency_error', message='Instale yt-dlp: python -m pip install yt-dlp. ' + str(exc))
    except KeyboardInterrupt: report.update(case='cancelled', message='Teste cancelado; nenhuma conclusão de acesso ao DVR.')
    except Exception as exc: report.update(case='yt_dlp_error', message=redact(exc))
    report['finished_utc'] = datetime.now(timezone.utc).isoformat()
    (args.output/'info.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    log.write(f"Conclusão {report['conclusion']} — {report.get('case')}: {report.get('message')}")
    log.write('Relatório: ' + str(args.output/'info.json'))
    return 0 if report.get('proved_historical') or report.get('case') == 'analysis_only' else 2


if __name__ == '__main__':
    raise SystemExit(main())
