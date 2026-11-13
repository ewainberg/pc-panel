"""Opt-in native Windows media and toast integration test; creates test UI entries."""
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(Path(__file__).parent))
from test_sender import validate
from winrt.windows.media.playback import MediaPlayer
from winrt.windows.media import MediaPlaybackType,MediaPlaybackStatus,SystemMediaTransportControlsTimelineProperties
from winrt.windows.ui.notifications import ToastNotificationManager,ToastNotification
from winrt.windows.data.xml.dom import XmlDocument
from winrt.windows.ui.notifications.management import UserNotificationListener

async def main():
 receiver=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
 receiver.bind(('127.0.0.1',0));receiver.setblocking(False)
 process=subprocess.Popen([sys.executable,str(ROOT/'host/rmine_telemetry_windows.py'),'127.0.0.1','--port',str(receiver.getsockname()[1]),'--duration','14','--no-audio'])
 player=MediaPlayer();player.command_manager.is_enabled=False
 controls=player.system_media_transport_controls
 controls.is_enabled=True;controls.is_play_enabled=True;controls.is_pause_enabled=True
 updater=controls.display_updater;updater.type=MediaPlaybackType.MUSIC
 timeline=SystemMediaTransportControlsTimelineProperties()
 timeline.start_time=timedelta(0);timeline.end_time=timedelta(seconds=180)
 timeline.min_seek_time=timedelta(0);timeline.max_seek_time=timedelta(seconds=180)
 def track(title,position,status):
  updater.music_properties.artist='PC Panel Test'
  updater.music_properties.title=title;updater.update()
  controls.playback_status=status
  timeline.position=timedelta(seconds=position)
  controls.update_timeline_properties(timeline)
 try:
  packets=[];started=time.monotonic();changed=paused=toasted=False
  track('First test track',12,MediaPlaybackStatus.PLAYING)
  while time.monotonic()-started<13:
   elapsed=time.monotonic()-started
   if elapsed>3 and not changed:
    track('Second test track',25,MediaPlaybackStatus.PLAYING);changed=True
   if elapsed>6 and not paused:
    track('Second test track',28,MediaPlaybackStatus.PAUSED);paused=True
   if elapsed>9 and not toasted:
    document=XmlDocument()
    document.load_xml('<toast><visual><binding template="ToastGeneric"><text>PC Panel test</text><text>Notification forwarding test</text></binding></visual></toast>')
    toast=ToastNotification(document);toast.tag='pcpanel-test';toast.group='pcpanel-test'
    ToastNotificationManager.create_toast_notifier('Windows PowerShell').show(toast)
    toasted=True
   try:
    raw,_=await asyncio.wait_for(asyncio.get_running_loop().sock_recvfrom(receiver,8192),.3)
    p=json.loads(raw.decode('utf-8'));validate(p);packets.append(p)
   except asyncio.TimeoutError:pass
  process.wait(timeout=4)
  assert process.returncode==0
  playing=[p for p in packets if p['track_title']=='First test track' and p['track_status']=='Playing']
  assert playing,'Windows media metadata not captured'
  assert all(p['track_duration']==180 for p in playing)
  assert playing[-1]['track_position']>playing[0]['track_position']+.5,[(p['track_position']) for p in playing[::10]]
  music=[p['notification'] for p in packets if p['notification'] and p['notification']['kind']=='music']
  assert any(n['message']=='PC Panel Test - Second test track' for n in music),'track-change notice missing'
  paused_packets=[p for p in packets if p['track_status']=='Paused']
  assert paused_packets and paused_packets[-1]['track_paused_for']>4
  assert all(p['audio_state']=='paused' and p['track_position']==28 for p in paused_packets)
  notices=[p['notification'] for p in packets if p['notification'] and p['notification']['kind']=='system']
  assert any(n['title']=='PC Panel test' and n['message']=='Notification forwarding test' for n in notices),'native test toast not forwarded'
  print('NATIVE INTEGRATION:',json.dumps({'packets':len(packets),'track_metadata':True,'duration_seconds':180,'playing_position_advances':True,'paused_position_stable':True,'pause_age':True,'track_change_notification':True,'native_toast_forwarded':True}))
  (Path(__file__).parent/'last-native-test.json').write_text(json.dumps({'track_metadata':True,'duration_seconds':180,'playing_position_advances':True,'paused_position_stable':True,'pause_age':True,'track_change_notification':True,'native_toast_forwarded':True},indent=2))
 finally:
  controls.is_enabled=False;player.close();receiver.close()
  if process.poll() is None:process.terminate();process.wait(timeout=3)
  # Remove only this test's toast from Action Center.
  try:ToastNotificationManager.history.remove_group('pcpanel-test','Windows PowerShell')
  except Exception:pass

asyncio.run(main())
