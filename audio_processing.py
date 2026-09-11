"""Prepare large meeting recordings for the transcription API without truncation.

Small uploads retain their original encoding. Large uploads are decoded once and
split on audio frame boundaries, in chronological order. Temporary MP3 files live
only for the duration of ``prepare_audio_parts`` (including API failures).
"""
from contextlib import contextmanager
import math
from pathlib import Path
import selectors
import subprocess
import tempfile
import time
import os
import json
import hashlib
import shutil


MAX_AUDIO_BYTES = 100_000_000
AUDIO_DIRECT_LIMIT = 24_000_000
SEGMENT_SECONDS = 15 * 60
MAX_AUDIO_DURATION_SECONDS = 8 * 60 * 60
CONVERSION_TIMEOUT_SECONDS = 20 * 60
MAX_OUTPUT_BYTES = 256 * 1024 * 1024
MAX_PART_BYTES = 24_000_000
RESUME_SEGMENT_SECONDS = 20 * 60
RESUME_FORMAT = 'mp3-16k-64k-1200s-v1'


class AudioProcessingError(Exception):
    """An actionable error safe to display to the meeting author."""


def _ffmpeg_executable():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError) as exc:
        raise AudioProcessingError(
            '대용량 녹음파일 변환 도구를 준비하지 못했습니다. 관리자에게 문의해 주세요.'
        ) from exc


def transcoder_ready():
    """Check the deployment's encoder executable without starting a conversion."""
    try:
        executable = Path(_ffmpeg_executable())
        return executable.is_file() and os.access(executable, os.X_OK)
    except AudioProcessingError:
        return False


def _check_outputs(directory, segment_seconds=None):
    segment_seconds = segment_seconds or SEGMENT_SECONDS
    parts = sorted(directory.glob('part-*.mp3'))
    # One trailing frame-sized part can occur exactly at the duration boundary.
    if len(parts) > math.ceil(MAX_AUDIO_DURATION_SECONDS / segment_seconds) + 1:
        raise AudioProcessingError('대용량 녹음파일은 8시간 이하로 나누어 업로드해 주세요.')
    sizes = [part.stat().st_size for part in parts]
    if sum(sizes) > MAX_OUTPUT_BYTES:
        raise AudioProcessingError('변환 결과가 너무 큽니다. 녹음파일을 나누어 업로드해 주세요.')
    if any(size > MAX_PART_BYTES for size in sizes):
        raise AudioProcessingError('녹음파일을 전송 가능한 크기로 변환하지 못했습니다. 파일을 나누어 업로드해 주세요.')
    return parts


def _run_conversion(input_path, directory, progress, segment_seconds=None):
    segment_seconds = segment_seconds or SEGMENT_SECONDS
    command = [
        _ffmpeg_executable(), '-hide_banner', '-nostdin', '-y',
        '-loglevel', 'error', '-xerror', '-max_alloc', '67108864',
        '-threads', '1', '-filter_threads', '1',
        # Do not accept playlists or allow uploaded containers to fetch URLs.
        '-protocol_whitelist', 'file,pipe',
        '-format_whitelist', 'mov,mp4,m4a,3gp,3g2,mj2,mp3,wav,matroska,webm,mpeg',
        '-i', str(input_path), '-map', '0:a:0', '-vn', '-sn', '-dn',
        '-map_metadata', '-1', '-map_chapters', '-1',
        '-af', 'asetpts=PTS-STARTPTS', '-ac', '1', '-ar', '16000',
        '-c:a', 'libmp3lame', '-b:a', '64k', '-threads', '1',
        '-f', 'segment', '-segment_time', str(segment_seconds),
        '-segment_format', 'mp3', '-reset_timestamps', '1',
        # Encoding is continuous across parts. Repeating a Xing gapless header
        # would make decoders skip encoder-delay samples at EVERY boundary.
        '-segment_format_options', 'write_xing=0',
        '-progress', 'pipe:1', '-stats_period', '0.5',
        str(directory / 'part-%05d.mp3'),
    ]
    process = None
    completed = False
    last_time = 0.0
    last_report = -1
    pending = b''
    started = time.monotonic()
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() - started > CONVERSION_TIMEOUT_SECONDS:
                    raise AudioProcessingError('녹음파일 변환 시간이 초과되었습니다. 파일을 나누어 다시 업로드해 주세요.')
                _check_outputs(directory, segment_seconds)
                for key, _ in selector.select(timeout=0.25):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    pending += chunk
                    while b'\n' in pending:
                        line, pending = pending.split(b'\n', 1)
                        name, _, value = line.partition(b'=')
                        if name == b'out_time_us':
                            try:
                                last_time = max(last_time, int(value) / 1_000_000)
                            except ValueError:
                                continue
                            # Allow codec frame padding, never intentionally stop
                            # at a time limit and present an incomplete result.
                            if last_time > MAX_AUDIO_DURATION_SECONDS + 0.25:
                                raise AudioProcessingError('대용량 녹음파일은 8시간 이하로 나누어 업로드해 주세요.')
                            minutes = int(last_time // 60)
                            if progress and minutes != last_report:
                                progress(f'대용량 녹음 변환 중 · {minutes}분 처리')
                                last_report = minutes
                        elif name == b'progress' and value.strip() == b'end':
                            completed = True
                    if len(pending) > 8192:
                        raise AudioProcessingError('녹음파일 변환 상태를 확인하지 못했습니다. 다시 시도해 주세요.')
        remaining = max(0.1, CONVERSION_TIMEOUT_SECONDS - (time.monotonic() - started))
        if process.wait(timeout=remaining) != 0 or not completed or last_time <= 0:
            raise AudioProcessingError('녹음파일을 읽을 수 없거나 오디오가 없습니다. 정상 재생되는 파일인지 확인해 주세요.')
    except subprocess.TimeoutExpired as exc:
        raise AudioProcessingError('녹음파일 변환 시간이 초과되었습니다. 파일을 나누어 다시 업로드해 주세요.') from exc
    except OSError as exc:
        raise AudioProcessingError('녹음파일 변환 중 파일 처리에 실패했습니다. 잠시 후 다시 시도해 주세요.') from exc
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout:
                process.stdout.close()
    parts = _check_outputs(directory, segment_seconds)
    if not parts or any(part.stat().st_size == 0 for part in parts):
        raise AudioProcessingError('변환된 녹음이 비어 있습니다. 정상 재생되는 파일인지 확인해 주세요.')
    return parts


def prepare_persistent_parts(file_path, directory, progress=None):
    """Always split by duration, even for long, low-bitrate files under 24 MB.

    A manifest published after conversion is the commit marker. Incomplete
    conversion directories are discarded; completed parts survive API errors.
    The caller holds the per-job file lock throughout preparation/transcription.
    """
    source, directory = Path(file_path), Path(directory)
    if not source.is_file() or not 0 < source.stat().st_size <= MAX_AUDIO_BYTES:
        raise AudioProcessingError('원본 녹음파일이 없거나 용량을 확인할 수 없습니다. 다시 업로드해 주세요.')
    digest = hashlib.sha256()
    with source.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            digest.update(chunk)
    identity = digest.hexdigest()
    manifest_path = directory / 'manifest.json'
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
            if manifest['source_sha256'] != identity or manifest['format'] != RESUME_FORMAT:
                raise ValueError('source mismatch')
            parts = []
            for index, item in enumerate(manifest['parts']):
                name = f'part-{index:05d}.mp3'
                path = directory / name
                if item['name'] != name or path.stat().st_size != item['size']:
                    raise ValueError('part mismatch')
                if not 0 < item['size'] <= MAX_PART_BYTES:
                    raise ValueError('part size')
                parts.append((path, 'audio/mpeg'))
            if not parts:
                raise ValueError('empty manifest')
            return parts
        except (OSError, KeyError, TypeError, ValueError) as exc:
            # Never silently regenerate a different layout under saved transcripts.
            raise AudioProcessingError('저장된 음성 구간을 확인할 수 없습니다. 새 분석으로 원본 파일을 올려 주세요.') from exc
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(mode=0o700, parents=True)
    try:
        paths = _run_conversion(source, directory, progress, RESUME_SEGMENT_SECONDS)
        # Frame padding can create a sub-second tail at an exact 20-minute
        # boundary. Join a <2s tail to its predecessor rather than transcribing
        # an almost empty extra segment (no audio samples are discarded).
        if len(paths)>1 and paths[-1].stat().st_size<16000:
            join_list = directory / 'join.txt'
            joined = directory / 'joined.mp3'
            join_list.write_text(''.join("file '"+p.name+"'\n" for p in paths[-2:]))
            subprocess.run([_ffmpeg_executable(),'-hide_banner','-loglevel','error','-nostdin',
                '-f','concat','-safe','1','-i',str(join_list),'-map_metadata','-1',
                '-c','copy','-write_xing','0',str(joined)],check=True,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60)
            joined.replace(paths[-2])
            paths.pop().unlink()
            join_list.unlink()
        manifest = {'source_sha256': identity, 'format': RESUME_FORMAT,
            'parts': [{'name': p.name, 'size': p.stat().st_size} for p in paths]}
        pending = directory / 'manifest.tmp'
        with pending.open('w') as fh:
            json.dump(manifest, fh)
            fh.flush()
            os.fsync(fh.fileno())
        pending.replace(manifest_path)
        return [(p, 'audio/mpeg') for p in paths]
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


@contextmanager
def prepare_audio_parts(file_path, mime, progress=None):
    """Yield ordered ``[(Path, MIME)]``; clean temporary outputs on every exit.

    ``progress`` is an optional callback accepting a Korean status string. The
    8-hour and conversion resource limits apply only to files being converted.
    The original upload remains owned by the caller and is never removed here.
    """
    input_path = Path(file_path).resolve()
    try:
        size = input_path.stat().st_size
    except OSError as exc:
        raise AudioProcessingError('업로드한 녹음파일을 찾을 수 없습니다. 다시 업로드해 주세요.') from exc
    if not input_path.is_file() or size == 0:
        raise AudioProcessingError('녹음파일이 비어 있습니다. 다른 파일을 선택해 주세요.')
    if size > MAX_AUDIO_BYTES:
        raise AudioProcessingError('녹음파일은 100 MB 이하로 업로드해 주세요.')
    if size <= AUDIO_DIRECT_LIMIT:
        yield [(input_path, mime)]
        return
    if progress:
        progress('대용량 녹음파일을 변환하고 나누는 중')
    # Keep each job isolated and remove partial files if conversion/transcription
    # fails or the caller exits before consuming every part.
    with tempfile.TemporaryDirectory(prefix='audio-parts-', dir=input_path.parent) as temp:
        parts = _run_conversion(input_path, Path(temp), progress)
        yield [(part, 'audio/mpeg') for part in parts]
