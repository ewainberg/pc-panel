"""Protocol/state tests plus a real Windows UDP + output-loopback integration test."""
import ast
from io import BytesIO
import json
import math
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'host'))
import rmine_telemetry_windows as win


def reference_keys():
    tree = ast.parse((ROOT / 'host/rmine_telemetry.py').read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'msg' for t in node.targets):
            return {k.value for k in node.value.keys}
    raise AssertionError('Linux msg dictionary not found')


def validate(packet):
    assert set(packet) == reference_keys(), set(packet) ^ reference_keys()
    for key, value in packet.items():
        if key in ('hostname','track_artist','track_title','track_status','audio_state'):
            assert isinstance(value, str), key
        elif key == 'notification':
            assert value is None or (set(value) == {'id','kind','title','message'} and
                type(value['id']) is int and value['kind'] in ('music','system') and
                isinstance(value['title'],str) and isinstance(value['message'],str)), key
        elif key == 'audio_bands':
            assert len(value) == 32 and all(type(v) in (float,int) and math.isfinite(v) and 0 <= v <= 100 for v in value)
        elif key.startswith('gpu_') and value is None:
            continue
        else:
            assert type(value) in (float,int) and math.isfinite(value) and value >= 0, (key,value)
    for key in ('cpu','mem','gpu_util','gpu_mem','audio_left','audio_right','audio_bass','audio_overall'):
        assert packet[key] is None or packet[key] <= 100, key
    assert packet['audio_state'] in ('playing','paused','standby')
    assert packet['track_status'] in ('Playing','Paused','Stopped','')
    assert len(json.dumps(packet,ensure_ascii=False).encode('utf-8')) < 8192


class ProtocolTests(unittest.TestCase):
    def test_reference_audio_dsp_parity(self):
        # Execute only the Linux AudioMonitor class, with a finite fake parec stream.
        tree = ast.parse((ROOT / 'host/rmine_telemetry.py').read_text())
        node = next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name == 'AudioMonitor')
        namespace = {'np':np}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<linux-audio>','exec'),namespace)
        t = np.arange(1200) / 48000.0
        pcm = (np.column_stack((0.12*np.sin(2*np.pi*120*t),0.04*np.sin(2*np.pi*1000*t)))*32767).astype('<i2')
        linux = namespace['AudioMonitor'].__new__(namespace['AudioMonitor'])
        linux.levels,linux.bands,linux.bass,linux.overall=(0.0,0.0),[0.0]*32,0.0,0.0
        linux.process = types.SimpleNamespace(stdout=BytesIO(pcm.tobytes()))
        linux.read_levels()
        actual = win.AudioDSP().feed(pcm.astype(np.float32)/32768)
        np.testing.assert_allclose(actual[0],linux.levels,atol=1e-5)
        np.testing.assert_allclose(actual[1],linux.bands,atol=1e-5)
        self.assertAlmostEqual(actual[2],linux.bass,places=5)
        self.assertAlmostEqual(actual[3],linux.overall,places=5)

    def test_track_changes_pause_and_notifications(self):
        s=win.PanelState()
        silent=((0.0,0.0),[0.0]*32,0.0,0.0)
        a=('Artist','First',2.0,200.0,'Playing')
        b=('Artist','Second',0.0,180.0,'Playing')
        self.assertIsNone(s.update(10,a,silent)['notification'])
        self.assertEqual(s.update(11,b,silent)['notification']['kind'],'music')
        self.assertEqual(s.update(12,b,silent,('Hello','World'))['notification']['kind'],'system')
        paused=(*b[:4],'Paused')
        self.assertEqual(s.update(13,paused,silent)['track_paused_for'],0)
        self.assertEqual(s.update(15,paused,silent)['track_paused_for'],2)
        self.assertEqual(s.update(17,paused,silent)['audio_state'],'paused')
        self.assertIsNone(s.update(17,paused,silent)['notification'])
        self.assertEqual(s.update(18,win.EMPTY_TRACK,silent)['audio_state'],'standby')
        loud=((50.,50.),[30.]*32,40.,50.)
        self.assertEqual(s.update(19,win.EMPTY_TRACK,loud)['audio_state'],'playing')
        self.assertEqual(s.update(21,win.EMPTY_TRACK,silent)['audio_state'],'playing')
        self.assertEqual(s.update(23,win.EMPTY_TRACK,silent)['audio_state'],'standby')

    def test_optional_audio_dependency_failure(self):
        h=win.Health()
        with patch.dict(sys.modules,{'pyaudiowpatch':None}):
            a=win.AudioMonitor(threading.Event(),h,enabled=False)
            a.run()
        self.assertIn('missing',h.states['audio'])
        self.assertEqual(a.snapshot()[3],0)

    def test_gpu_unavailable(self):
        with patch.object(win.shutil,'which',return_value='nvidia-smi'), patch.object(win.subprocess,'check_output',return_value='N/A, N/A, N/A, N/A'):
            self.assertEqual(win.gpu_stats(),(None,None,None))

    def test_poll_failure_does_not_escape(self):
        stop=threading.Event()
        def fail():
            stop.set()
            raise OSError('sensor disconnected')
        p=win.Poller('test',fail,0.,1.,stop,win.Health())
        time.sleep(.05)
        self.assertEqual(p.snapshot(),0.)

    def test_actual_pi_receiver_and_stale_fallback(self):
        # Use the receiver class from the committed Pi code, without OLED imports.
        tree=ast.parse((ROOT/'pi/rmine_panel.py').read_text())
        node=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='TelemetryReceiver')
        scope={'socket':socket,'json':json,'time':time,'TELEMETRY_PORT':7355,'TELEMETRY_STALE':2.5,
               'LocalTelemetry':lambda:types.SimpleNamespace(sample=lambda:{'source':'LOCAL'})}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<pi-receiver>','exec'),scope)
        receiver=scope['TelemetryReceiver'](port=0)
        sender=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        try:
            sender.sendto(json.dumps({'hostname':'Windows 測試','notification':None}).encode('utf-8'),('127.0.0.1',receiver.sock.getsockname()[1]))
            time.sleep(.02)
            packet=receiver.poll()
            self.assertEqual(packet['hostname'],'Windows 測試')
            self.assertEqual(packet['source'],'HOST')
            receiver.latest_at-=3
            self.assertEqual(receiver.poll()['source'],'LOCAL')
        finally:
            sender.close();receiver.close()


def integration():
    import winsound
    receiver=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    receiver.bind(('127.0.0.1',0))
    receiver.settimeout(.25)
    process=subprocess.Popen([sys.executable,str(ROOT/'host/rmine_telemetry_windows.py'),'127.0.0.1',
                              '--port',str(receiver.getsockname()[1]),'--duration','8'],creationflags=win.NO_WINDOW)
    packets,times=[],[]
    # A short quiet stereo test tone through the default output, never microphone.
    def tone():
        import wave
        import io
        time.sleep(3)
        t=np.arange(48000*2)/48000.
        samples=(np.column_stack((.06*np.sin(2*np.pi*120*t),.025*np.sin(2*np.pi*1000*t)))*32767).astype('<i2')
        buffer=io.BytesIO()
        with wave.open(buffer,'wb') as wav:
            wav.setnchannels(2);wav.setsampwidth(2);wav.setframerate(48000);wav.writeframes(samples.tobytes())
        winsound.PlaySound(buffer.getvalue(),winsound.SND_MEMORY)
    worker=threading.Thread(target=tone)
    worker.start()
    try:
        deadline=time.monotonic()+12
        while time.monotonic()<deadline:
            try:
                raw,_=receiver.recvfrom(8192)
                packet=json.loads(raw.decode('utf-8'));validate(packet)
                packets.append(packet);times.append(time.monotonic())
            except socket.timeout:
                if process.poll() is not None:break
        process.wait(timeout=3)
        worker.join(timeout=4)
        assert process.returncode==0,process.returncode
        assert len(packets)>=145,len(packets)
        hz=(len(times)-1)/(times[-1]-times[0])
        assert 18<=hz<=22,hz
        maximum=max(p['audio_overall'] for p in packets)
        assert maximum>10,maximum
        assert any(p['audio_left']>p['audio_right']+5 for p in packets)
        assert any(max(p['audio_bands'])>10 for p in packets)
        assert any(p['audio_bass']>10 for p in packets)
        assert any(p['gpu_util'] is not None for p in packets)
        report={'packets':len(packets),'rate_hz':round(hz,2),'max_audio_overall':round(maximum,2),
                'gpu_readings':True,'schema_keys':len(reference_keys()),'pi_receiver_class':True,
                'last_cpu_temp':packets[-1]['cpu_temp']}
        print('INTEGRATION:',json.dumps(report))
        (ROOT/'host/windows/last-test.json').write_text(json.dumps(report,indent=2))
    finally:
        receiver.close()
        if process.poll() is None:process.terminate();process.wait(timeout=3)


if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(ProtocolTests)
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():sys.exit(1)
    integration()
