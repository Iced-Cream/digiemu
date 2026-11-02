"""Synthetic eSDHC/eDMA data-path coverage; no firmware input required."""

import os
import struct
import tempfile
import unittest

from emu.edma import (
    BITER,
    CITER,
    CSR,
    DADDR,
    NBYTES,
    SADDR,
    SOFF,
    TCD_BASE,
)
from emu.esdhc import BASE, CMDARG, DPSEL, DTDSEL, Esdhc, Card


class FakeUc:
    def __init__(self):
        self.memory = {}

    def hook_add(self, *args, **kwargs):
        return 1

    def mem_read(self, addr, size):
        return bytes(self.memory.get(addr + offset, 0) for offset in range(size))

    def mem_write(self, addr, data):
        for offset, value in enumerate(data):
            self.memory[addr + offset] = value


class FakeMachine:
    def __init__(self):
        self.uc = FakeUc()

    def ensure(self, addr):
        pass


def put16(uc, addr, value):
    uc.mem_write(addr, struct.pack(">H", value))


def put32(uc, addr, value):
    uc.mem_write(addr, struct.pack(">I", value))


def get32(uc, addr):
    return struct.unpack(">I", uc.mem_read(addr, 4))[0]


class EsdhcBulkReadTest(unittest.TestCase):
    def test_cmd18_moves_backing_sector_and_posts_both_data_completions(self):
        machine = FakeMachine()
        image = bytes((i & 0xFF) for i in range(1024))
        cmd_sem, data_sem, dma_sem = 0x50001000, 0x50001010, 0x50001020
        drv_status, destination = 0x50001030, 0x50002000
        model = Esdhc(
            machine,
            card=Card(image=image),
            drv_status=drv_status,
            cmd_sem=cmd_sem,
            data_sem=data_sem,
            dma_sem=dma_sem,
        )

        channel = 59
        tcd = TCD_BASE + channel * 0x20
        put32(machine.uc, tcd + SADDR, BASE + 0x20)
        put32(machine.uc, tcd + NBYTES, 4)
        put32(machine.uc, tcd + DADDR, destination)
        put16(machine.uc, tcd + CITER, 128)
        put16(machine.uc, tcd + BITER, 128)
        put16(machine.uc, tcd + CSR, 0)
        put32(machine.uc, BASE + CMDARG, 1)

        model._on_serq(None, None, 0, 1, channel, None)
        model._on_serq(None, None, 0, 1, 35, None)
        self.assertEqual(model.armed, channel)
        model._on_xfertyp(
            None, None, 0, 4, (18 << 24) | DPSEL | DTDSEL, None
        )

        self.assertEqual(machine.uc.mem_read(destination, 512), image[512:])
        self.assertEqual(get32(machine.uc, dma_sem), 1)
        self.assertEqual(get32(machine.uc, data_sem), 1)
        self.assertEqual(get32(machine.uc, cmd_sem), 1)
        self.assertEqual(get32(machine.uc, drv_status), 0)
        self.assertEqual(get32(machine.uc, tcd + DADDR), destination + 512)
        csr = struct.unpack(">H", machine.uc.mem_read(tcd + CSR, 2))[0]
        self.assertEqual(csr & 0x80, 0x80)

    def test_cmd25_consumes_host_buffer_and_is_visible_to_cmd18(self):
        machine = FakeMachine()
        data_sem, dma_sem = 0x50001010, 0x50001020
        source = 0x50002000
        payload = bytes((255 - i) & 0xFF for i in range(512))
        model = Esdhc(machine, data_sem=data_sem, dma_sem=dma_sem)

        channel = 59
        tcd = TCD_BASE + channel * 0x20
        machine.uc.mem_write(source, payload)
        put32(machine.uc, tcd + SADDR, source)
        put16(machine.uc, tcd + SOFF, 16)
        put32(machine.uc, tcd + NBYTES, 16)
        put32(machine.uc, tcd + DADDR, BASE + 0x20)
        put16(machine.uc, tcd + CITER, 32)
        put16(machine.uc, tcd + BITER, 32)
        put16(machine.uc, tcd + CSR, 0)
        put32(machine.uc, BASE + CMDARG, 7)

        model._on_serq(None, None, 0, 1, channel, None)
        model._on_xfertyp(None, None, 0, 4, (25 << 24) | DPSEL, None)

        self.assertEqual(model.card.data_for(18, 7, 512), payload)
        self.assertEqual(get32(machine.uc, dma_sem), 1)
        self.assertEqual(get32(machine.uc, data_sem), 1)

        restored = Esdhc(FakeMachine())
        restored.restore_checkpoint_state(model.checkpoint_state())
        self.assertEqual(restored.card.data_for(18, 7, 512), payload)


class EsdhcCheckpointCardTest(unittest.TestCase):
    """What a snapshot carries of the card depends on where the card lives."""

    def test_in_memory_card_carries_its_writes(self):
        model = Esdhc(FakeMachine(), card=Card())
        model.card.write_data(25, 3, b'\x5A' * 512)
        model.card.erase(0, 512)
        state = model.checkpoint_state()
        self.assertEqual(len(state['card_overlay']), 512)
        self.assertEqual(state['card_erased'], [[0, 512]])
        self.assertEqual(len(model.card.overlay), 512)

    def test_file_backed_card_flushes_and_carries_nothing(self):
        # The file is the truth once flushed. A snapshot that carried the
        # overlay replayed it over the file on every restore, hiding anything
        # written there later.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'plusdrive.img')
            with open(path, 'wb') as f:
                f.write(b'\xEE' * 4096)
            card = Card(path=path)
            model = Esdhc(FakeMachine(), card=card)
            card.write_data(25, 3, b'\x5A' * 512)
            card.erase(0, 1024)
            state = model.checkpoint_state()
            self.assertEqual(state['card_overlay'], {})
            self.assertEqual(state['card_erased'], [])
            self.assertEqual(card.overlay, {})
            self.assertEqual(card.erased, [])
            self.assertEqual(card.data_for(18, 3, 512), b'\x5A' * 512)
            self.assertEqual(card.data_for(18, 0, 512), bytes(512))
            card.close()

            # A later session writes to the file; restoring the snapshot must
            # not put the old bytes back over it.
            with open(path, 'r+b') as f:
                f.seek(3 * 512)
                f.write(b'\x77' * 512)
            card2 = Card(path=path)
            restored = Esdhc(FakeMachine(), card=card2)
            restored.restore_checkpoint_state(state)
            self.assertEqual(card2.data_for(18, 3, 512), b'\x77' * 512)
            self.assertEqual(card2.data_for(18, 2, 512), b'\xEE' * 512)
            card2.close()


if __name__ == "__main__":
    unittest.main()


class IrqstatWriteOneToClearTest(unittest.TestCase):
    """IRQSTAT is write-1-to-clear: acking CC must leave TC set for a poll."""

    def test_store_of_cc_clears_cc_only(self):
        from emu.esdhc import CC, IRQSTAT, TC
        machine = FakeMachine()
        esd = Esdhc(machine, card=Card(image=bytes(512)))
        esd._set_bits(IRQSTAT, CC | TC)
        # the guest's `move.l #1, IRQSTAT`: the hook first, then the store lands
        esd._on_irqstat_write(machine.uc, None, BASE + IRQSTAT, 4, CC, None)
        put32(machine.uc, BASE + IRQSTAT, CC)
        esd._on_irqstat_read(machine.uc, None, BASE + IRQSTAT, 4, 0, None)
        self.assertEqual(get32(machine.uc, BASE + IRQSTAT), TC)

    def test_byte_store_and_model_set_after_it(self):
        from emu.esdhc import CC, IRQSTAT, TC
        machine = FakeMachine()
        esd = Esdhc(machine, card=Card(image=bytes(512)))
        esd._set_bits(IRQSTAT, CC | TC)
        # a byte store of 0x03 to the low byte clears CC and TC
        esd._on_irqstat_write(machine.uc, None, BASE + IRQSTAT + 3, 1, CC | TC, None)
        machine.uc.mem_write(BASE + IRQSTAT + 3, bytes([CC | TC]))
        esd._set_bits(IRQSTAT, TC)        # the next command, before any read
        esd._on_irqstat_read(machine.uc, None, BASE + IRQSTAT, 4, 0, None)
        self.assertEqual(get32(machine.uc, BASE + IRQSTAT), TC)
