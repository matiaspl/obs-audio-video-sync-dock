'''
QR image generation helpers.
'''

import importlib
import os
import subprocess
import tempfile
import warnings
from PIL import Image


def _make_with_python_qrcode(payload):
    qrcode = importlib.import_module('qrcode')
    return qrcode.make(
        payload,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
    ).get_image()


def _make_with_qrencode(payload):
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as temp:
        temp_name = temp.name
    try:
        subprocess.run(
            ['qrencode', '-t', 'PNG', '-l', 'M', '-o', temp_name, payload],
            check=True,
        )
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            image = Image.open(temp_name).convert('RGB')
            image.load()
        return image
    finally:
        os.unlink(temp_name)


def make_qr_image(payload):
    '''
    Return a PIL image for a QR payload.
    '''
    try:
        return _make_with_python_qrcode(payload)
    except ModuleNotFoundError:
        return _make_with_qrencode(payload)
