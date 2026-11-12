"""Compose real screen/camera recordings into a customized H.264/AAC MP4."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


class RecordingError(RuntimeError):
    pass


@dataclass(frozen=True)
class Layout:
    width: int = 1920
    height: int = 1080
    fps: int = 30
    camera_width: int = 576
    camera_height: int = 324
    margin: int = 24
    camera_position: str = 'bottom-right'
    background: str = 'ffffff'

    def __post_init__(self):
        for value, maximum in ((self.width, 3840), (self.height, 2160),
                               (self.camera_width, self.width), (self.camera_height, self.height)):
            if type(value) is not int or not 64 <= value <= maximum or value % 2:
                raise ValueError('Video dimensions must be even integers within the output canvas')
        if type(self.fps) is not int or not 5 <= self.fps <= 60:
            raise ValueError('Frame rate must be 5–60')
        if type(self.margin) is not int or self.margin < 0:
            raise ValueError('Margin must be a non-negative integer')
        if self.camera_width + self.margin * 2 > self.width or self.camera_height + self.margin * 2 > self.height:
            raise ValueError('Camera and margins must fit within the output canvas')
        if self.camera_position not in ('bottom-right', 'bottom-left', 'top-right', 'top-left'):
            raise ValueError('Invalid camera position')
        if not re.fullmatch(r'[0-9a-fA-F]{6}', self.background):
            raise ValueError('Background must be a six-digit hexadecimal color')

    @classmethod
    def from_dict(cls, values):
        return cls(**values)

    def to_dict(self):
        return asdict(self)

    def filters(self):
        x = self.width - self.camera_width - self.margin if self.camera_position.endswith('right') else self.margin
        y = self.height - self.camera_height - self.margin if self.camera_position.startswith('bottom') else self.margin
        return (
            f'[0:v]setpts=PTS-STARTPTS,scale={self.width}:{self.height}:'
            f'force_original_aspect_ratio=decrease:force_divisible_by=2,'
            f'pad={self.width}:{self.height}:(ow-iw)/2:(oh-ih)/2:color=0x{self.background},'
            f'setsar=1,fps={self.fps}[screen];'
            f'[1:v]setpts=PTS-STARTPTS,scale={self.camera_width}:{self.camera_height}:'
            f'force_original_aspect_ratio=increase:force_divisible_by=2,'
            f'crop={self.camera_width}:{self.camera_height},setsar=1,fps={self.fps}[camera];'
            f'[screen][camera]overlay={x}:{y}:eof_action=repeat:shortest=0[composite]'
        )


def require_ffmpeg():
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise RecordingError('FFmpeg and ffprobe are required on the office Mac. Install with: brew install ffmpeg')


def probe(path: Path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RecordingError('A recording track is incomplete or unreadable')
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RecordingError('Unable to inspect recording media') from exc


def run_encoder(arguments, output: Path, timeout=12 * 3600):
    if output.exists():
        raise RecordingError('Output file already exists; refusing to overwrite a saved recording')
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.stem + '.partial.mp4')
    log = output.with_suffix('.ffmpeg.log')
    with log.open('wb') as errors:
        result = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'warning', '-nostdin', '-y',
                                 *arguments, '-movflags', '+faststart', str(partial)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors, timeout=timeout)
    if result.returncode:
        raise RecordingError(f'Video encoding failed. Original tracks are preserved; see {log.name}')
    media = probe(partial)
    if not any(s.get('codec_type') == 'video' for s in media.get('streams', [])):
        raise RecordingError('Encoder did not produce a video track')
    if float(media.get('format', {}).get('duration', 0)) <= 0:
        raise RecordingError('Encoder produced an empty recording')
    partial.replace(output)
    return output


def compose(screen: Path, camera: Path, output: Path, layout: Layout, audio: Path | None = None):
    require_ffmpeg()
    screen_info = probe(screen)
    duration = float(screen_info.get('format', {}).get('duration') or 0)
    if duration <= 0:
        # MediaRecorder WebM files often have no duration metadata. Scan packet
        # timestamps with constant memory instead of estimating from frame count.
        scan = subprocess.Popen(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                                 '-show_entries', 'packet=pts_time,duration_time', '-of', 'csv=p=0', str(screen)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        first = None
        for line in scan.stdout:
            try:
                fields = line.strip().split(',')
                stamp = float(fields[0])
                span = float(fields[1]) if len(fields) > 1 and fields[1] != 'N/A' else 1 / layout.fps
            except (ValueError, IndexError):
                continue
            first = stamp if first is None else min(first, stamp)
            duration = max(duration, stamp + span)
        if scan.wait() or first is None:
            raise RecordingError('No screen frames were received')
        duration -= first
    source_audio = audio or camera
    if not any(s.get('codec_type') == 'audio' for s in probe(source_audio).get('streams', [])):
        raise RecordingError('Microphone audio is missing; refusing to silently create a recording without sound')
    arguments = ['-i', str(screen), '-i', str(camera)]
    audio_index = 1
    if audio:
        arguments += ['-i', str(audio)]
        audio_index = 2
    arguments += ['-filter_complex', layout.filters(), '-map', '[composite]', '-map', f'{audio_index}:a:0',
                  '-af', 'asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0,apad', '-t', str(duration),
                  '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
                  '-c:a', 'aac', '-b:a', '160k', '-ar', '48000']
    return run_encoder(arguments, output)


def concatenate(parts: list[Path], output: Path):
    require_ffmpeg()
    if not parts:
        raise RecordingError('No completed recording segments are available')
    manifest = output.with_suffix('.concat.txt')
    manifest.parent.mkdir(parents=True, exist_ok=True)
    def quoted(path):
        return str(path.resolve()).replace("'", "'\\''")
    manifest.write_text(''.join(f"file '{quoted(p)}'\n" for p in parts))
    return run_encoder(['-f', 'concat', '-safe', '0', '-i', str(manifest), '-c', 'copy'], output)
