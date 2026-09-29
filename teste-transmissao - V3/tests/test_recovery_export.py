import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_engine as engine
import recording_exports as exports

FFMPEG = os.environ.get('TEST_FFMPEG') or shutil.which('ffmpeg')
FFPROBE = os.environ.get('TEST_FFPROBE') or shutil.which('ffprobe')
REAL_RUN = subprocess.run

def run_ff(args, **kwargs):
    args = list(args)
    if args[0] == 'ffmpeg': args[0] = FFMPEG
    return REAL_RUN(args, **kwargs)

class Features(unittest.TestCase):
    def setUp(self):
        import uuid
        self.root = Path(tempfile.gettempdir()).resolve() / ('live-test-' + uuid.uuid4().hex)
        self.root.mkdir(mode=0o755)
        self.data = self.root / 'data'
        self.patches = [patch.object(engine, 'DATA', self.data), patch.object(engine, 'LOGS', self.root/'logs'),
                        patch.object(exports, 'DATA', self.data)]
        for p in self.patches: p.start()
        self.s = engine.LiveSession('https://example.test/live', log=lambda _: None)
    def tearDown(self):
        for p in self.patches: p.stop()
        assert self.root.parent == Path(tempfile.gettempdir()).resolve() and self.root.name.startswith('live-test-')
        shutil.rmtree(self.root)
    def test_stop_during_resolution_does_not_restart(self):
        class Process:
            returncode = 1
            stdin = None
            def poll(self): return 1
        self.s.ffmpeg = Process()
        self.s.resolve_stream = lambda: (self.s.stop() or ['https://example.test/media'])
        with patch.object(engine, 'RECOVERY_DELAY_SEC', 0), patch.object(self.s, 'start_ffmpeg') as spawn:
            self.s._watch_ffmpeg()
            spawn.assert_not_called()
        self.assertEqual(self.s.status, 'stopped')
    def test_bounded_retries(self):
        class Process:
            returncode = 1
            stdin = None
            def poll(self): return 1
        self.s.ffmpeg = Process()
        with patch.object(engine, 'RECOVERY_DELAY_SEC', 0), patch.object(self.s, 'resolve_stream', side_effect=RuntimeError('offline')) as resolve:
            self.s._watch_ffmpeg()
        self.assertEqual(resolve.call_count, 5)
        self.assertEqual(self.s.status, 'dead')
    def test_recovery_resolves_and_spawns(self):
        class Process:
            returncode = 1
            stdin = None
            def poll(self): return 1
        self.s.ffmpeg = Process()
        def spawn(urls):
            self.assertEqual(urls, ['video', 'audio'])
            self.s._stop.set()
        with patch.object(engine, 'RECOVERY_DELAY_SEC', 0), patch.object(self.s, 'resolve_stream', return_value=['video','audio']) as resolve, patch.object(self.s, 'start_ffmpeg', side_effect=spawn):
            self.s._watch_ffmpeg()
        resolve.assert_called_once()
        self.assertEqual(self.s.recovery_attempts, 1)
    def test_invalid_and_empty_export(self):
        with self.assertRaises(ValueError): exports.start_export('../escape')
        self.s.m3u8.write_text('#EXTM3U\n', encoding='utf-8')
        with self.assertRaises(ValueError): exports.start_export(self.s.id)
    @unittest.skipUnless(FFMPEG and FFPROBE, 'FFmpeg e ffprobe necessários')
    def test_append_and_export_real_media(self):
        source = self.root/'source.mp4'
        run_ff(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=25',
                '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','6',
                '-c:v','libx264','-c:a','aac',str(source)],check=True,capture_output=True)
        run_ff(self.s._ffmpeg_cmd([str(source)]),check=True,capture_output=True)
        old = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.s.hls_dir.glob('*.ts')}
        before = self.s.m3u8.read_text()
        self.assertNotIn('#EXT-X-ENDLIST', before)
        self.s.recovery_attempts = 1
        run_ff(self.s._ffmpeg_cmd([str(source)]),check=True,capture_output=True)
        after = self.s.m3u8.read_text()
        self.assertIn('#EXT-X-MEDIA-SEQUENCE:0', after)
        self.assertGreater(after.count('#EXTINF:'), before.count('#EXTINF:'))
        self.assertIn('#EXT-X-DISCONTINUITY', after)
        for name,digest in old.items():
            self.assertEqual(hashlib.sha256((self.s.hls_dir/name).read_bytes()).hexdigest(), digest)
            self.assertIn(name, after)
        # Exporta snapshot enquanto playlist ainda está aberta, sem tocar no HLS.
        with patch.object(exports.subprocess, 'run', side_effect=run_ff):
            exports.start_export(self.s.id)
            deadline=time.monotonic()+30
            while exports._jobs[self.s.id]['status']=='running' and time.monotonic()<deadline: time.sleep(.05)
        job=exports._jobs[self.s.id]
        self.assertEqual(job['status'],'ready',job)
        self.assertEqual(self.s.m3u8.read_text(),after)
        mp4=self.s.dir/'exports'/job['file']
        result=REAL_RUN([FFPROBE,'-v','error','-show_streams','-show_format','-of','json',str(mp4)],capture_output=True,text=True,check=True)
        meta=json.loads(result.stdout)
        self.assertEqual({s['codec_type'] for s in meta['streams']},{'video','audio'})
        self.assertGreater(float(meta['format']['duration']),11)
        self.assertLess(float(meta['format']['duration']),14)
        run_ff(['ffmpeg','-v','error','-i',str(mp4),'-f','null','-'],check=True,capture_output=True)
        # Fecha playlist e exporta novamente sem sobrescrever o MP4 anterior.
        self.s.stop()
        self.assertIn('#EXT-X-ENDLIST',self.s.m3u8.read_text())
        self.assertTrue(mp4.is_file())
        self.assertEqual(exports.list_recordings()[0]['files'],[mp4.name])
        with patch.object(exports.subprocess, 'run', side_effect=run_ff):
            exports.start_export(self.s.id)
            deadline=time.monotonic()+30
            while exports._jobs[self.s.id]['status']=='running' and time.monotonic()<deadline: time.sleep(.05)
        self.assertEqual(exports._jobs[self.s.id]['status'], 'ready')
        self.assertEqual(len(exports.list_recordings()[0]['files']), 2)
        self.assertTrue(mp4.is_file())

    def test_failed_export_preserves_source(self):
        segment = self.s.hls_dir/'seg_00000.ts'
        segment.write_bytes(b'not-valid-media')
        self.s.m3u8.write_text('#EXTM3U\n#EXTINF:2,\nseg_00000.ts\n',encoding='utf-8')
        before = self.s.m3u8.read_bytes()
        with patch.object(exports.subprocess,'run',return_value=subprocess.CompletedProcess([],1)):
            exports.start_export(self.s.id)
            deadline=time.monotonic()+5
            while exports._jobs[self.s.id]['status']=='running' and time.monotonic()<deadline: time.sleep(.01)
        self.assertEqual(exports._jobs[self.s.id]['status'],'error')
        self.assertEqual(self.s.m3u8.read_bytes(),before)
        self.assertEqual(segment.read_bytes(),b'not-valid-media')
        self.assertEqual(exports.list_recordings()[0]['files'],[])

if __name__=='__main__': unittest.main(verbosity=2)
