"""tools/usbrun.py's stdin commands: the card copy that pairs with a saved
snapshot, the command reader, and the USB MIDI idle test a save waits for."""
import io
import os
import queue
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools'))

import usbrun  # noqa: E402
from emu import usbmidi  # noqa: E402


def test_copy_card_keeps_bytes_and_size(tmp_path):
    src = tmp_path / 'plusdrive.img'
    data = bytearray(3 * usbrun.CHUNK + 1000)
    data[5] = 1
    data[2 * usbrun.CHUNK + 7] = 0xAA
    data[-1] = 0x55
    src.write_bytes(bytes(data))
    dst = tmp_path / 'setup.snap.card'
    usbrun.copy_card(str(src), str(dst))
    assert dst.read_bytes() == bytes(data)
    assert not os.path.exists(str(dst) + '.tmp')


def test_copy_card_all_zero_tail(tmp_path):
    src = tmp_path / 'c.img'
    src.write_bytes(b'\x01' + bytes(2 * usbrun.CHUNK))
    dst = tmp_path / 'd.img'
    usbrun.copy_card(str(src), str(dst))
    assert dst.read_bytes() == src.read_bytes()


def test_read_commands_skips_blank_lines():
    q = queue.Queue()
    usbrun.read_commands(io.StringIO('save\n\n  save /x/y.snap  \n'), q)
    assert [q.get(), q.get()] == ['save', 'save /x/y.snap']
    assert q.empty()


def test_usb_idle_follows_input_and_transfers():
    u = usbmidi.UsbMidi()
    assert u.idle
    u.put(bytes([0x90, 60, 100]))
    assert not u.idle                   # input waiting
    u._packets.clear()
    u._busy.add(0x1000)
    assert not u.idle                   # a transfer in the guest's hands
    u._busy.clear()
    u._calls[(1, 2)] = 3
    assert not u.idle                   # a post's call frame on the stack
    u._calls.clear()
    assert u.idle
