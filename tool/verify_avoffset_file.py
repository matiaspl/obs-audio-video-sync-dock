#! /usr/bin/env python3
'''
Verify v2 AV offset clip timing directly from the media file.
'''

# pylint: disable=duplicate-code,too-many-locals,too-many-branches,too-many-statements,too-many-arguments

import argparse
import array
import json
import math
import statistics
import struct
import subprocess
import sys
from fractions import Fraction

MARKER_MS = 80
MARKER_F0 = 500.0
MARKER_F1 = 2500.0
MARKER_TICK_MS = 6
MARKER_CHIRP_GAIN = 0.72
MARKER_TICK_GAIN = 0.28
RGB_PIXEL_SIZE = 3


def parse_fraction(value):
    '''
    Parse an ffprobe fraction.
    '''
    if '/' in value:
        num, den = value.split('/', 1)
        return Fraction(int(num), int(den))
    return Fraction(int(value), 1)


def run_json(command):
    '''
    Run a command that prints JSON.
    '''
    return json.loads(subprocess.check_output(command, text=True))


def probe_media(path, ffprobe):
    '''
    Return basic stream metadata needed by the verifier.
    '''
    data = run_json([
        ffprobe,
        '-v', 'error',
        '-print_format', 'json',
        '-show_streams',
        '-show_format',
        path,
    ])
    video = next(stream for stream in data['streams'] if stream['codec_type'] == 'video')
    audio = next(stream for stream in data['streams'] if stream['codec_type'] == 'audio')
    fps_text = video.get('avg_frame_rate') or video.get('r_frame_rate')
    return {
        'width': int(video['width']),
        'height': int(video['height']),
        'fps': parse_fraction(fps_text),
        'sample_rate': int(audio['sample_rate']),
    }


def ms_to_samples(sample_rate, ms_value):
    '''
    Convert milliseconds to integer samples.
    '''
    return sample_rate * ms_value // 1000


def marker_reference(sample_rate):
    '''
    Build the centered v2 timing marker. Sample count / 2 is the event instant.
    '''
    count = ms_to_samples(sample_rate, MARKER_MS)
    center = count // 2
    first_count = max(1, center)
    second_count = max(1, count - center)
    first_duration = first_count / sample_rate
    second_duration = second_count / sample_rate
    up_slope = (MARKER_F1 - MARKER_F0) / first_duration
    down_slope = (MARKER_F1 - MARKER_F0) / second_duration
    phase_center = 2.0 * math.pi * (
        MARKER_F0 * first_duration + 0.5 * up_slope * first_duration * first_duration
    )
    tick_sigma = (MARKER_TICK_MS / 1000.0) / 6.0
    tick_radius = MARKER_TICK_MS / 2000.0
    values = []

    for index in range(count):
        if index < center:
            time = index / sample_rate
            phase = 2.0 * math.pi * (MARKER_F0 * time + 0.5 * up_slope * time * time)
        else:
            time = (index - center) / sample_rate
            phase = phase_center + 2.0 * math.pi * (
                MARKER_F1 * time - 0.5 * down_slope * time * time
            )

        window = 0.5 - 0.5 * math.cos(2.0 * math.pi * index / max(1, count - 1))
        chirp = math.sin(phase) * window
        center_time = (index - center) / sample_rate
        tick = 0.0
        if abs(center_time) <= tick_radius:
            x_val = center_time / tick_sigma
            tick = (1.0 - x_val * x_val) * math.exp(-0.5 * x_val * x_val)
        values.append(MARKER_CHIRP_GAIN * chirp + MARKER_TICK_GAIN * tick)

    peak = max(abs(value) for value in values) or 1.0
    return [value / peak for value in values]


def reference_points(width, height):
    '''
    Return the same sparse-marker sample points used by the generated clip.
    '''
    size = min(width, height)
    offset_x = (width - size) // 2
    offset_y = (height - size) // 2
    corners = (
        (offset_x, offset_y),
        (offset_x + size, offset_y),
        (offset_x + size, offset_y + size),
        (offset_x, offset_y + size),
    )
    center = (offset_x + size // 2, offset_y + size // 2)
    return [((x_pos * 15 + center[0] * 9) // 24,
             (y_pos * 15 + center[1] * 9) // 24) for x_pos, y_pos in corners]


def frame_score(frame, width, points):
    '''
    Score sparse checker polarity for one RGB frame.
    '''
    values = []
    for x_pos, y_pos in points:
        offset = (y_pos * width + x_pos) * RGB_PIXEL_SIZE
        values.append(sum(frame[offset:offset + RGB_PIXEL_SIZE]) / RGB_PIXEL_SIZE)
    return (values[1] + values[3]) - (values[0] + values[2])


def find_video_events(path, ffmpeg, metadata, event_count):
    '''
    Decode video and return detected event timestamps in seconds.
    '''
    width = metadata['width']
    height = metadata['height']
    fps = metadata['fps']
    frame_bytes = width * height * RGB_PIXEL_SIZE
    points = reference_points(width, height)
    events = []
    command = [
        ffmpeg,
        '-hide_banner', '-loglevel', 'error',
        '-i', path,
        '-an',
        '-f', 'rawvideo',
        '-pix_fmt', 'rgb24',
        '-',
    ]

    with subprocess.Popen(command, stdout=subprocess.PIPE) as process:
        prev_time = None
        prev_score = None
        frame_index = 0
        while len(events) < event_count:
            frame = process.stdout.read(frame_bytes)
            if len(frame) < frame_bytes:
                break
            timestamp = float(Fraction(frame_index, 1) / fps)
            score = frame_score(frame, width, points)
            if prev_score is not None and prev_score < 0 <= score:
                interval = timestamp - prev_time
                # Mirrors sync-test-output: zero crossing plus half a frame.
                offset = interval * (score - prev_score * 3.0) / ((score - prev_score) * 2.0)
                events.append({
                    'timestamp': prev_time + offset,
                    'score': score - prev_score,
                })
            prev_time = timestamp
            prev_score = score
            frame_index += 1

        process.kill()
        process.wait()

    return events


def decode_audio(path, ffmpeg, sample_rate):
    '''
    Decode the media audio as mono s16le samples.
    '''
    pcm = subprocess.check_output([
        ffmpeg,
        '-hide_banner', '-loglevel', 'error',
        '-i', path,
        '-vn',
        '-f', 's16le',
        '-ac', '1',
        '-ar', str(sample_rate),
        '-',
    ])
    samples = array.array('h')
    samples.frombytes(pcm)
    if struct.pack('=h', 1) != struct.pack('<h', 1):
        samples.byteswap()
    return samples


def marker_score(samples, start, reference, ref_energy):
    '''
    Return normalized marker correlation at a sample offset.
    '''
    end = start + len(reference)
    if start < 0 or end > len(samples):
        return 0.0
    corr = 0.0
    energy = 0.0
    for sample, ref_sample in zip(samples[start:end], reference):
        value = sample / 32768.0
        corr += value * ref_sample
        energy += value * value
    if energy <= 1e-18 or ref_energy <= 1e-18:
        return 0.0
    return abs(corr) / math.sqrt(energy * ref_energy)


def find_audio_event(samples, sample_rate, reference, video_timestamp, search_ms):
    '''
    Search near one video event and return the audio marker center timestamp.
    '''
    ref_energy = sum(value * value for value in reference)
    marker_center = len(reference) // 2
    center_sample = round(video_timestamp * sample_rate)
    radius = ms_to_samples(sample_rate, search_ms)
    start_min = max(0, center_sample - radius - marker_center)
    start_max = min(len(samples) - len(reference), center_sample + radius - marker_center)
    coarse_step = max(1, sample_rate // 3000)
    best_start = start_min
    best_score = -1.0

    for start in range(start_min, start_max + 1, coarse_step):
        score = marker_score(samples, start, reference, ref_energy)
        if score > best_score:
            best_start = start
            best_score = score

    refine_min = max(start_min, best_start - coarse_step * 2)
    refine_max = min(start_max, best_start + coarse_step * 2)
    for start in range(refine_min, refine_max + 1):
        score = marker_score(samples, start, reference, ref_energy)
        if score > best_score:
            best_start = start
            best_score = score

    return {
        'timestamp': (best_start + marker_center) / sample_rate,
        'score': best_score,
    }


def print_results(results):
    '''
    Print per-event and summary timing.
    '''
    for index, result in enumerate(results, start=1):
        print(
            f"EVENT {index} video_s={result['video']:.6f} "
            f"audio_s={result['audio']:.6f} offset_ms={result['offset_ms']:.3f} "
            f"video_score={result['video_score']:.1f} audio_score={result['audio_score']:.6f}"
        )

    offsets = [result['offset_ms'] for result in results]
    if offsets:
        median = statistics.median(offsets)
        deviations = [abs(offset - median) for offset in offsets]
        print(
            f"SUMMARY events={len(offsets)} median_ms={median:.3f} "
            f"mad_ms={statistics.median(deviations):.3f} "
            f"min_ms={min(offsets):.3f} max_ms={max(offsets):.3f}"
        )
    else:
        print('SUMMARY events=0')


def main():
    '''
    CLI entrypoint.
    '''
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('media', help='AV offset media file')
    parser.add_argument('--events', type=int, default=4, help='Number of events to verify')
    parser.add_argument('--search-ms', type=int, default=120, help='Audio search radius per event')
    parser.add_argument(
        '--tolerance-ms', type=float, default=1.0, help='Maximum absolute median offset'
    )
    parser.add_argument('--ffmpeg', default='ffmpeg', help='ffmpeg executable')
    parser.add_argument('--ffprobe', default='ffprobe', help='ffprobe executable')
    args = parser.parse_args()

    metadata = probe_media(args.media, args.ffprobe)
    video_events = find_video_events(args.media, args.ffmpeg, metadata, args.events)
    samples = decode_audio(args.media, args.ffmpeg, metadata['sample_rate'])
    reference = marker_reference(metadata['sample_rate'])
    results = []
    for video in video_events:
        audio = find_audio_event(samples, metadata['sample_rate'], reference,
                                 video['timestamp'], args.search_ms)
        results.append({
            'video': video['timestamp'],
            'audio': audio['timestamp'],
            'offset_ms': (audio['timestamp'] - video['timestamp']) * 1000.0,
            'video_score': video['score'],
            'audio_score': audio['score'],
        })

    print_results(results)

    if len(results) < args.events:
        print(f'ERROR expected {args.events} events, found {len(results)}', file=sys.stderr)
        return 1

    median = statistics.median(result['offset_ms'] for result in results)
    if abs(median) > args.tolerance_ms:
        print(
            f'ERROR median offset {median:.3f} ms exceeds tolerance '
            f'{args.tolerance_ms:.3f} ms',
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
