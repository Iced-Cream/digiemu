# pyright: reportMissingImports=false
"""The supply, as the Digitone (mk1) board shows it to the OS: a power-off.

Pulling the plug on the device does not stop the OS at once. The board has
two inputs for it, both GPIO pins the OS reads:

- the power sense, port C bit 2 (PPDSDR_C, 0xEC09401A): high while the
  supply is up. The OS takes both of its edges on EPORT0 pin 2 (INTC0
  source 2, vector 66) at level 7;
- the hold-up capacitor's "charged" line, port G bit 3 (PPDSDR_G,
  0xEC09401E). The OS arms its power-off save only once it has seen it high.

On the falling edge the OS quiets the board and runs its power-off save from
two software-forced interrupts on INTC2 (sources 29 and 30, vectors 221 and
222). The save writes the working state to the eMMC and ends with CMD0 with
argument 0xF0F0F0F0 (GO_PRE_IDLE_STATE); the OS then waits for the power to
go. The next boot loads that state back. Nothing here knows a guest address:
the OS's own handler does the work, so any OS that keeps that handler
behaves as on the device.

`PowerSupply` models the two pins (both high: supply up, capacitor charged
at once, since its charge time is not known) and, once `cut()` is called,
the edge: the sense reads low, EPORT's flag 2 is set, and vector 66 is
delivered on the shared step clock. `INTC2 sources 29 and 30` are delivered
by emu/intfrc.py's ForcedInterrupts. `done` is set when the eSDHC has seen
the pre-idle CMD0 after the cut.

Not modelled: the capacitor's charge time and running out mid-save, the
power coming back mid-save (the OS's "power good" path), brown-outs.
"""
from unicorn import UC_HOOK_MEM_READ
from unicorn.m68k_const import UC_M68K_REG_SR

from emu.pit import PENDING_STEP, interrupt_level

GPIO = 0xEC094000
PPDSDR_C = GPIO + 0x18 + 2      # port C pins
PPDSDR_G = GPIO + 0x18 + 6      # port G pins
SENSE_BIT = 0x04                # PC2: supply up
CHARGED_BIT = 0x08              # PG3: hold-up capacitor charged
EPORT = 0xFC090000
EPFR = EPORT + 6                # edge port flags (write 1 to clear)
SENSE_PIN = 2
VECTOR = 64 + SENSE_PIN         # INTC0 source 2
INTC2 = 0xFC050000
INTC2_FIRST_VECTOR = 192
SAVE_SOURCES = (29, 30)         # the OS's forced "power back" / "power down"
PRE_IDLE = 0xF0F0F0F0           # CMD0's argument for GO_PRE_IDLE_STATE


class PowerSupply:
    """The supply and hold-up capacitor pins; `cut()` switches the supply off."""

    def __init__(self, m, esdhc=None):
        self.m = m
        self.esdhc = esdhc
        self.up = True
        self.pending = False        # the falling edge, not yet delivered
        self.delivered = False
        self.log_at = None          # esdhc.log length at the cut
        self.cut_at = None          # instruction count at the cut
        self.now = 0
        uc = m.uc
        uc.hook_add(UC_HOOK_MEM_READ, self._on_sense, begin=PPDSDR_C, end=PPDSDR_C)
        uc.hook_add(UC_HOOK_MEM_READ, self._on_charged, begin=PPDSDR_G, end=PPDSDR_G)
        # Its other bits are whatever else models them (emu/gpio.py's SdGate on
        # port C bit 3) or plain memory: change only this pin, as SdGate does.

    def _pin(self, uc, addr, bit, high):
        cur = bytes(uc.mem_read(addr, 1))[0] & ~bit
        uc.mem_write(addr, bytes([cur | (bit if high else 0)]))

    def _on_sense(self, uc, typ, addr, size, val, data):
        self._pin(uc, PPDSDR_C, SENSE_BIT, self.up)

    def _on_charged(self, uc, typ, addr, size, val, data):
        self._pin(uc, PPDSDR_G, CHARGED_BIT, True)

    def cut(self):
        """Switch the supply off: the sense falls and EPORT flags the edge."""
        if not self.up:
            return
        self.up = False
        self.pending = True
        self.cut_at = self.now
        if self.esdhc is not None:
            self.log_at = len(self.esdhc.log)
        flags = self.m.peek(EPFR, 1)[0]
        self.m.poke(EPFR, bytes([flags | (1 << SENSE_PIN)]))

    def commands(self):
        """-> the eSDHC commands (index, argument) issued since the cut."""
        if self.esdhc is None or self.log_at is None:
            return []
        return list(self.esdhc.log[self.log_at:])

    @property
    def armed(self):
        """Whether the OS takes the edge at all (EPORT0 pin 2's interrupt enabled)."""
        return bool(self.m.peek(EPORT + 3, 1)[0] & (1 << SENSE_PIN))

    @property
    def done(self):
        """The OS has put the eMMC in pre-idle: its power-off save is over."""
        return any(i == 0 and a == PRE_IDLE for i, a in self.commands())

    def blocks_written(self):
        """-> 512-byte blocks the OS wrote (CMD23 counts before CMD25) since the cut."""
        n, count = 0, None
        for idx, arg in self.commands():
            if idx == 23:
                count = arg & 0xFFFF
            elif idx == 25:
                n += count or 0
                count = None
        return n

    # -- the event source protocol (emu.longrun.spin) ---------------------
    def step(self, done, remaining=None):
        self.now = done
        if not self.pending:
            return remaining
        return min(PENDING_STEP, remaining) if remaining is not None else PENDING_STEP

    def service(self, done):
        self.now = done
        if not self.pending:
            return False
        level = interrupt_level(self.m, VECTOR, respect_mask=False)
        if level is None:
            self.pending = False    # not armed: the OS never sees it
            return False
        sr = self.m.uc.reg_read(UC_M68K_REG_SR)
        if level < 7 and ((sr >> 8) & 7) >= level:
            return False            # level 7 is not maskable
        if self.m.raise_vector(VECTOR, level=level):
            self.pending = False
            self.delivered = True
            return True
        return False


def install(m, ev):
    """-> (PowerSupply, ForcedInterrupts for INTC2 29/30): both event sources
    for emu.longrun.spin, on the machine `build()` returned."""
    from emu import intfrc
    supply = PowerSupply(m, ev.get('esdhc'))
    forced = intfrc.ForcedInterrupts(m, sources=SAVE_SOURCES, base=INTC2,
                                     first_vector=INTC2_FIRST_VECTOR)
    ev['power'] = supply
    return supply, forced
