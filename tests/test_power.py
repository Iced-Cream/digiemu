"""emu/power.py: the supply and hold-up capacitor pins, and the power-off edge."""
from unicorn import UC_ARCH_M68K, UC_MODE_BIG_ENDIAN, Uc
from unicorn.m68k_const import UC_M68K_REG_SR

from emu import power


class Machine:
    """Just what PowerSupply uses: an engine, peek/poke, raise_vector."""

    def __init__(self):
        self.uc = Uc(UC_ARCH_M68K, UC_MODE_BIG_ENDIAN)
        for base in (0xEC094000, 0xFC048000, 0xFC090000, 0x40000000):
            self.uc.mem_map(base, 0x1000)
        self.raised = []

    def peek(self, addr, n):
        return bytes(self.uc.mem_read(addr, n))

    def poke(self, addr, data):
        self.uc.mem_write(addr, data)

    def raise_vector(self, vec, level=None):
        self.raised.append((vec, level))
        return True


class Esdhc:
    def __init__(self):
        self.log = []


def run_reads(m, *addrs):
    """Execute one `move.b (addr).l,d0` per address, so the read hooks fire."""
    code = b''.join(b'\x10\x39' + a.to_bytes(4, 'big') for a in addrs)
    m.uc.mem_write(0x40000000, code)
    m.uc.emu_start(0x40000000, 0x40000000 + len(code))


def arm(m, level=7):
    m.poke(power.EPORT + 3, bytes([1 << power.SENSE_PIN]))       # EPIER
    m.poke(0xFC048000 + 0x40 + power.SENSE_PIN, bytes([level]))   # ICR0 source 2


def test_pins_read_high_and_keep_other_bits():
    m = Machine()
    power.PowerSupply(m)
    m.poke(power.PPDSDR_C, b'\x08')          # the SD gate's bit, someone else's
    m.poke(power.PPDSDR_G, b'\x02')          # an output the OS drives high
    run_reads(m, power.PPDSDR_C, power.PPDSDR_G)
    assert m.peek(power.PPDSDR_C, 1)[0] == 0x08 | power.SENSE_BIT
    assert m.peek(power.PPDSDR_G, 1)[0] == 0x02 | power.CHARGED_BIT


def test_cut_drops_sense_flags_edge_and_delivers_level_7():
    m = Machine()
    esd = Esdhc()
    s = power.PowerSupply(m, esd)
    arm(m)
    assert s.armed
    esd.log.append((25, 0x100))               # before the cut: not counted
    m.uc.reg_write(UC_M68K_REG_SR, 0x2700)    # even with the mask at 7
    s.cut()
    assert m.peek(power.EPFR, 1)[0] & (1 << power.SENSE_PIN)
    assert s.step(0) is not None
    assert s.service(0)
    assert m.raised == [(power.VECTOR, 7)]
    assert not s.service(1)                   # once
    run_reads(m, power.PPDSDR_C)
    assert not m.peek(power.PPDSDR_C, 1)[0] & power.SENSE_BIT
    assert not s.done
    esd.log += [(23, 0x80000040), (25, 0x7A000), (23, 0x80000040), (25, 0x7A040),
                (0, power.PRE_IDLE)]
    assert s.done
    assert s.blocks_written() == 128


def test_cut_unarmed_is_never_delivered():
    m = Machine()
    s = power.PowerSupply(m)
    assert not s.armed
    s.cut()
    assert not s.service(0)
    assert m.raised == []
    assert s.step(0, 123) == 123


def test_plain_go_idle_is_not_the_end_of_the_save():
    m = Machine()
    esd = Esdhc()
    s = power.PowerSupply(m, esd)
    arm(m)
    s.cut()
    esd.log.append((0, 0))
    assert not s.done
