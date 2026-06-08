#! /usr/bin/env python3
'''
Generate low-flash AV offset measurement clips.
'''

# pylint: disable=too-many-instance-attributes,too-many-arguments,too-many-locals

import argparse
import array
import math
import os
import shutil
import subprocess
import sys
from fractions import Fraction
from PIL import Image, ImageDraw, ImageFont
from qrutil import make_qr_image

MARKER_MS = 80
MARKER_F0 = 500.0
MARKER_F1 = 2500.0
MARKER_TICK_MS = 6
MARKER_CHIRP_GAIN = 0.72
MARKER_TICK_GAIN = 0.28
SYMBOL_MS = 70
GUARD_MS = 60
PAYLOAD_SYMBOLS = 5
DTMF_ROWS = (697.0, 770.0, 852.0, 941.0)
DTMF_COLS = (1209.0, 1336.0, 1477.0)
PAYLOAD_BASE = len(DTMF_ROWS) * len(DTMF_COLS)


def parse_fraction(value):
    '''
    Parse an integer or n/d string into Fraction.
    '''
    if '/' in value:
        num, den = value.split('/', 1)
        return Fraction(int(num), int(den))
    return Fraction(int(value), 1)


def parse_size(value):
    '''
    Parse WxH video size.
    '''
    width, height = value.split('x', 1)
    return int(width), int(height)


def crc8_u8(value):
    '''
    CRC-8 over one 8-bit event code.
    '''
    crc = value & 0xFF
    for _ in range(8):
        if crc & 0x80:
            crc = ((crc << 1) ^ 0x07) & 0xFF
        else:
            crc = (crc << 1) & 0xFF
    return crc


def ms_to_samples(sample_rate, ms_value):
    '''
    Convert milliseconds to integer samples.
    '''
    return sample_rate * ms_value // 1000


def add_sample(samples, index, value):
    '''
    Mix and clamp one floating-point sample into an s16 array.
    '''
    if index < 0 or index >= len(samples):
        return
    mixed = samples[index] + round(value * 32767.0)
    samples[index] = max(-32768, min(32767, mixed))


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


class ClipGenerator:
    '''
    Generate v2 AV offset clip video frames and audio.
    '''

    def __init__(self, args):
        self.workdir = os.path.abspath(args.workdir)
        self.output = args.output
        self.fps = parse_fraction(args.vr)
        self.sample_rate = args.ar
        self.width, self.height = parse_size(args.size)
        self.duration = args.duration
        self.interval = args.interval
        self.qr_lead = args.qr_lead
        self.phase = args.phase_ms / 1000.0
        self.amplitude = args.amplitude
        self.contrast = args.contrast
        self.audio_codec = args.audio_codec
        self.video_codec = args.video_codec
        self.intra_video = args.intra_video
        self.images = []
        self.font = ImageFont.load_default(size=max(16, min(self.width, self.height) // 12))

    def frame_count(self):
        '''
        Number of video frames in the output.
        '''
        return math.ceil(self.duration * self.fps.numerator / self.fps.denominator)

    def audio_count(self):
        '''
        Number of audio samples in the output.
        '''
        return math.ceil(self.duration * self.sample_rate)

    def event_times(self):
        '''
        Measurement event times in seconds.
        '''
        first_event = max(0.25, self.qr_lead + self.phase + 0.1)
        marker_center = MARKER_MS / 2000.0
        packet_after_event = (
            MARKER_MS / 2 + GUARD_MS + PAYLOAD_SYMBOLS * (SYMBOL_MS + GUARD_MS)
        ) / 1000.0
        events = []
        event = max(first_event, marker_center + 0.05)
        while event + packet_after_event < self.duration:
            events.append(event)
            event += self.interval
        return events

    def qrcode_image(self, sequence):
        '''
        QR frame carrying the v2 sequence and sparse-marker timing metadata.
        '''
        payload = f'p=2,s={sequence},q={round(self.phase * 1000)},i={sequence & 0xFF},I=0,m=1,t=0'
        qr_img = make_qr_image(payload)
        size = min(self.width, self.height)
        qr_img = qr_img.resize((size, size))
        base = Image.new('RGB', (self.width, self.height), (127, 127, 127))
        offset = ((self.width - size) // 2, (self.height - size) // 2)
        base.paste(qr_img, offset)
        draw = ImageDraw.Draw(base)
        draw.text(
            xy=(20, self.height - 20),
            text=f'{sequence}',
            font=self.font,
            anchor='lb',
            fill=(40, 40, 40),
        )
        return base

    def reference_points(self):
        '''
        Approximate analyzer sample regions after QR-corner adjustment.
        '''
        size = min(self.width, self.height)
        offset_x = (self.width - size) // 2
        offset_y = (self.height - size) // 2
        corners = (
            (offset_x, offset_y),
            (offset_x + size, offset_y),
            (offset_x + size, offset_y + size),
            (offset_x, offset_y + size),
        )
        center = (offset_x + size // 2, offset_y + size // 2)
        points = []
        for x_pos, y_pos in corners:
            points.append(((x_pos * 15 + center[0] * 9) // 24, (y_pos * 15 + center[1] * 9) // 24))
        return points

    def marker_image(self, phase_index):
        '''
        Sparse checkerboard event frame preserving opposite-corner differential timing.
        '''
        base = Image.new('RGB', (self.width, self.height), (127, 127, 127))
        draw = ImageDraw.Draw(base)
        radius = max(12, min(self.width, self.height) // 8)
        high = int(127 + self.contrast)
        low = int(127 - self.contrast)
        for index, (x_pos, y_pos) in enumerate(self.reference_points()):
            is_high = (index % 2) == phase_index
            value = high if is_high else low
            draw.ellipse((x_pos - radius, y_pos - radius, x_pos + radius, y_pos + radius),
                         fill=(value, value, value))
        return base

    def neutral_image(self):
        '''
        Non-marker frame.
        '''
        return Image.new('RGB', (self.width, self.height), (127, 127, 127))

    def image_for_frame(self, frame_index, events):
        '''
        Return the image for one frame.
        '''
        timestamp = frame_index * self.fps.denominator / self.fps.numerator
        for sequence, event in enumerate(events, start=1):
            qr_start = event - self.qr_lead - self.phase
            phase0_start = event - self.phase
            phase1_end = event + self.phase
            if qr_start <= timestamp < phase0_start:
                return self.qrcode_image(sequence)
            if phase0_start <= timestamp < event:
                return self.marker_image(0)
            if event <= timestamp < phase1_end:
                return self.marker_image(1)
        return self.neutral_image()

    def write_video_frames(self, events):
        '''
        Write all frame PNGs and return the printf-style path for ffmpeg.
        '''
        frame_dir = os.path.join(self.workdir, 'avoffset-frames')
        shutil.rmtree(frame_dir, ignore_errors=True)
        os.makedirs(frame_dir)
        frame_pattern = os.path.join(frame_dir, 'frame-%06d.png')
        for frame_index in range(self.frame_count()):
            image_path = frame_pattern % frame_index
            self.image_for_frame(frame_index, events).save(image_path)
            self.images.append(image_path)
        return frame_pattern

    def add_marker(self, samples, start, reference):
        '''
        Add the centered acquisition marker. The reference center is the event.
        '''
        for index, sample in enumerate(reference):
            add_sample(samples, start + index, sample * self.amplitude)

    def add_payload(self, samples, start, sequence):
        '''
        Add DTMF-style base-12 event code symbols.
        '''
        code = sequence & 0xFF
        payload = (code << 8) | crc8_u8(code)
        digits = [0] * PAYLOAD_SYMBOLS
        for index in range(PAYLOAD_SYMBOLS - 1, -1, -1):
            digits[index] = payload % PAYLOAD_BASE
            payload //= PAYLOAD_BASE
        if payload:
            raise ValueError('Payload does not fit in the configured DTMF symbol count')
        symbol_samples = ms_to_samples(self.sample_rate, SYMBOL_MS)
        guard_samples = ms_to_samples(self.sample_rate, GUARD_MS)
        stride = symbol_samples + guard_samples
        for symbol_index, digit in enumerate(digits):
            row = DTMF_ROWS[digit // len(DTMF_COLS)]
            col = DTMF_COLS[digit % len(DTMF_COLS)]
            symbol_start = start + symbol_index * stride
            for index in range(symbol_samples):
                time = index / self.sample_rate
                ramp = min(
                    1.0,
                    index / max(1, guard_samples),
                    (symbol_samples - index - 1) / max(1, guard_samples),
                )
                tone = 0.5 * (
                    math.sin(2.0 * math.pi * row * time) +
                    math.sin(2.0 * math.pi * col * time)
                )
                add_sample(samples, symbol_start + index, tone * ramp * self.amplitude)

    def write_audio(self, events):
        '''
        Write the mono s16le audio file.
        '''
        samples = array.array('h', [0]) * self.audio_count()
        marker = marker_reference(self.sample_rate)
        marker_samples = len(marker)
        marker_center_samples = marker_samples // 2
        guard_samples = ms_to_samples(self.sample_rate, GUARD_MS)
        for sequence, event in enumerate(events, start=1):
            event_sample = round(event * self.sample_rate)
            packet_start = event_sample - marker_center_samples
            self.add_marker(samples, packet_start, marker)
            self.add_payload(samples, packet_start + marker_samples + guard_samples, sequence)
        if sys.byteorder != 'little':
            samples.byteswap()
        audio_path = os.path.join(self.workdir, 'avoffset.pcm')
        with open(audio_path, 'wb') as output:
            samples.tofile(output)
        return audio_path

    def output_clip(self):
        '''
        Generate the final clip.
        '''
        os.makedirs(self.workdir, exist_ok=True)
        events = self.event_times()
        if not events:
            raise ValueError('Duration is too short for one complete AV offset event')
        frame_pattern = self.write_video_frames(events)
        audio_path = self.write_audio(events)
        if self.fps.denominator == 1:
            fps_arg = str(self.fps.numerator)
        else:
            fps_arg = f'{self.fps.numerator}/{self.fps.denominator}'
        command = [
            'ffmpeg', '-hide_banner', '-loglevel', 'warning',
            '-framerate', fps_arg, '-i', frame_pattern,
            '-channel_layout', 'mono', '-f', 's16le', '-ac', '1',
            '-ar', str(self.sample_rate), '-i', audio_path,
            '-pix_fmt', 'yuv420p',
        ]
        if self.video_codec:
            command += ['-c:v', self.video_codec]
        elif self.intra_video:
            command += ['-c:v', 'libx264']
        if self.intra_video:
            command += ['-x264-params', 'keyint=1:min-keyint=1:scenecut=0:bframes=0']
        if self.audio_codec:
            command += ['-c:a', self.audio_codec]
        else:
            command += ['-b:a', '192k']
        if self.output.endswith('.mp4'):
            command += ['-movflags', '+faststart']
        command += ['-y', self.output]
        subprocess.run(command, check=True)
        os.unlink(audio_path)
        shutil.rmtree(os.path.dirname(frame_pattern), ignore_errors=True)
        print(f'Generated {self.output} with {len(events)} AV offset events')


def main():
    '''
    CLI entrypoint.
    '''
    parser = argparse.ArgumentParser(description='Generate low-flash AV offset test clips')
    parser.add_argument('-w', '--workdir', default='.', help='Temporary working directory')
    parser.add_argument('--vr', default='30', help='Video frame rate, e.g. 30 or 60000/1001')
    parser.add_argument('--ar', type=int, default=48000, help='Audio sample rate')
    parser.add_argument('--size', default='1280x720', help='Video size')
    parser.add_argument('--duration', type=float, default=30.0, help='Clip duration in seconds')
    parser.add_argument(
        '--interval', type=float, default=2.0, help='Seconds between measurement events'
    )
    parser.add_argument(
        '--qr-lead', type=float, default=0.5, help='Seconds to show QR before each marker'
    )
    parser.add_argument(
        '--phase-ms', type=int, default=100, help='Checker phase duration in milliseconds'
    )
    parser.add_argument('--amplitude', type=float, default=0.5, help='Audio marker amplitude')
    parser.add_argument(
        '--contrast', type=int, default=58, help='Sparse checker luminance offset from gray'
    )
    parser.add_argument(
        '--audio-codec', help='ffmpeg audio codec, e.g. aac or pcm_s16le'
    )
    parser.add_argument(
        '--video-codec', help='ffmpeg video codec, e.g. libx264'
    )
    parser.add_argument(
        '--intra-video',
        action='store_true',
        help='Encode each video frame independently with no B-frames',
    )
    parser.add_argument('-o', '--output', default='av-offset-pattern.mp4', help='Output clip path')
    args = parser.parse_args()
    ClipGenerator(args).output_clip()


if __name__ == '__main__':
    main()
