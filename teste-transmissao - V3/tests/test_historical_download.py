"""Local real DASH generator + yt-dlp + FFmpeg tests, without YouTube/network access."""
import functools
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import historical_download as history
from historical_worker import arguments, validate_media


def synthetic_worker(folder, port, media, slow):
    """Only extraction is substituted; native DASH downloading and muxing run unchanged."""
    import yt_dlp
    from historical_worker import run
    def download(ydl, urls):
        def fragments(track):
            yield {"url": f"http://127.0.0.1:{port}/init-stream{track}.m4s"}
            for p in sorted(Path(media).glob(f"chunk-stream{track}-*.m4s")):
                if slow:
                    time.sleep(0.3)
                yield {"url": f"http://127.0.0.1:{port}/{p.name}"}
        formats = []
        for track in (0, 1):
            formats.append({"format_id": str(track), "url": f"http://127.0.0.1:{port}/manifest.mpd",
                "ext": "mp4" if track == 0 else "m4a", "protocol": "http_dash_segments_generator",
                "fragments": fragments(track), "is_from_start": True,
                "vcodec": "h264" if track == 0 else "none", "acodec": "none" if track == 0 else "aac",
                "width": 160 if track == 0 else None, "height": 90 if track == 0 else None})
        ydl.process_ie_result({"id": "local-live", "title": "Local live", "extractor": "local",
            "webpage_url": urls[0], "is_live": True, "live_status": "is_live", "formats": formats}, download=True)
        return 0
    with patch.object(yt_dlp.YoutubeDL, "download", download):
        return run(folder)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg/ffprobe required')
class HistoricalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import uuid
        cls.root = Path(tempfile.gettempdir()).resolve() / ('historical-test-' + uuid.uuid4().hex)
        cls.root.mkdir(mode=0o755)
        cls.media = cls.root / 'media'
        cls.media.mkdir()
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=25',
            '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','24',
            '-c:v','libx264','-g','50','-keyint_min','50','-sc_threshold','0','-c:a','aac',
            '-f','dash','-seg_duration','2','manifest.mpd'],cwd=cls.media,check=True,capture_output=True)
        cls.server = ThreadingHTTPServer(('127.0.0.1',0), functools.partial(QuietHandler, directory=str(cls.media)))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        from urllib.request import urlopen
        assert (cls.media/'init-stream0.m4s').is_file(), list(cls.media.iterdir())
        with urlopen(f'http://127.0.0.1:{cls.server.server_port}/init-stream0.m4s') as response:
            assert response.read()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        assert cls.root.parent == Path(tempfile.gettempdir()).resolve() and cls.root.name.startswith('historical-test-')
        shutil.rmtree(cls.root)

    def job(self, stop=False):
        import uuid
        folder = self.root / uuid.uuid4().hex
        folder.mkdir()
        history.write_state(folder, {'id':folder.name,'url':'https://example.test/live','status':'starting', 'tracks':{}})
        with (folder/'download.log').open('w',encoding='utf-8') as log:
            p = subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker',str(folder),
                str(self.server.server_port),str(self.media),str(int(stop))],stdout=log,stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            try:
                if stop:
                    deadline = time.monotonic()+20
                    while time.monotonic() < deadline:
                        state = history.read_state(folder)
                        if len(state.get('tracks',{})) == 2 and all((t.get('fragment_index') or 0) >= 3 for t in state['tracks'].values()):
                            break
                        if p.poll() is not None:
                            self.fail((folder/'download.log').read_text())
                        time.sleep(.05)
                    else:
                        self.fail('Both tracks did not progress')
                    (folder/'STOP').touch()
                p.wait(timeout=40)
            finally:
                if p.poll() is None:
                    p.kill()
                    p.wait()
        state = history.read_state(folder)
        self.assertEqual(p.returncode,0,(state,(folder/'download.log').read_text()))
        self.assertEqual(state['status'],'stopped' if stop else 'completed',state)
        self.assertEqual(state['file'],'live.mkv')
        self.assertGreater(validate_media(folder/'live.mkv'), 2)
        subprocess.run(['ffmpeg','-v','error','-i',str(folder/'live.mkv'),'-f','null','-'],check=True,capture_output=True)
        self.assertEqual(len(state['tracks']),2)
        self.assertTrue(all(t['protocol']=='http_dash_segments_generator' for t in state['tracks'].values()))
        self.assertTrue(all(t['from_start'] for t in state['tracks'].values()))
        if stop:
            self.assertLess(state['duration'],24)
        else:
            self.assertGreater(state['duration'],23)
        return folder

    def test_native_generator_natural_end(self):
        self.job()

    def test_native_generator_safe_stop_and_mux(self):
        self.job(stop=True)

    def test_reject_audio_only_final(self):
        audio = self.root/'only-audio.m4a'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=duration=1',str(audio)],check=True,capture_output=True)
        with self.assertRaisesRegex(RuntimeError,'áudio e vídeo'):
            validate_media(audio)

    def test_manager_validation_stop_isolation_and_logs(self):
        with patch.object(history,'STORE',self.root/'store'):
            manager = history.HistoricalDownloads()
            for url in ('', 'file:///secret', '--exec bad'):
                with self.assertRaises(ValueError): manager.start(url)
            with self.assertRaises(ValueError): history.folder_for('../escape')
            folder = history.STORE / ('a'*32)
            folder.mkdir(parents=True)
            history.write_state(folder,{'id':folder.name,'status':'downloading','heartbeat':time.time()})
            (folder/'download.log').write_text('x'*50000)
            sentinel = self.root/'dvr.ts'
            sentinel.write_bytes(b'DVR unchanged')
            digest = hashlib.sha256(sentinel.read_bytes()).digest()
            with self.assertRaises(ValueError): manager.start('https://example.test/live')
            manager.stop(folder.name)
            self.assertTrue((folder/'STOP').exists())
            self.assertEqual(manager.list_jobs()[0]['status'],'stopping')
            self.assertLessEqual(len(''.join(manager.list_jobs()[0]['logs'])),24000)
            self.assertEqual(hashlib.sha256(sentinel.read_bytes()).digest(),digest)


if __name__ == '__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--worker':
        sys.exit(synthetic_worker(sys.argv[2],int(sys.argv[3]),sys.argv[4],bool(int(sys.argv[5]))))
    unittest.main()
