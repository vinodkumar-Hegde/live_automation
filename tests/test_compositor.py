import shutil
import subprocess

import pytest

from office_recorder.compositor import Layout, RecordingError, compose, concatenate, probe


@pytest.mark.parametrize('values', [
    {'width': 1919}, {'fps': 0}, {'camera_position': 'outside'},
    {'background': 'white;movie=/etc/passwd'}, {'camera_width': 1920}, {'margin': -1},
])
def test_layout_rejects_invalid_dimensions_and_filter_injection(values):
    with pytest.raises(ValueError):
        Layout(**values)


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg integration requires ffmpeg')
def test_real_mp4_composition_and_segment_join(tmp_path):
    screen = tmp_path / 'screen.mkv'
    camera = tmp_path / 'camera.mkv'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=white:s=640x360:r=30',
                    '-t', '0.7', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(screen)], check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=blue:s=320x180:r=30',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '0.7',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'pcm_s16le', str(camera)], check=True)
    output = tmp_path / 'part.mp4'
    compose(screen, camera, output, Layout(width=640, height=360, camera_width=160, camera_height=90, margin=16))
    metadata = probe(output)
    assert {s['codec_name'] for s in metadata['streams']} == {'h264', 'aac'}
    video = next(s for s in metadata['streams'] if s['codec_type'] == 'video')
    assert (video['width'], video['height']) == (640, 360)
    frame = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(output), '-frames:v', '1',
                            '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'], capture_output=True, check=True).stdout
    def pixel(x, y):
        start = (y * 640 + x) * 3
        return tuple(frame[start:start + 3])
    assert min(pixel(100, 100)) > 240  # The slide remains white and fills the canvas.
    red, green, blue = pixel(550, 290)
    assert blue > 220 and red < 40 and green < 40  # Faculty inset is at lower right.
    joined = tmp_path / 'complete.mp4'
    concatenate([output, output], joined)
    duration = float(probe(joined)['format']['duration'])
    assert 1.3 <= duration <= 1.7
    with pytest.raises(RecordingError, match='overwrite'):
        compose(screen, camera, output, Layout(width=640, height=360, camera_width=160, camera_height=90))
