"""
test_host_write.py — Host-to-chip integration test

Test sequence:
  1. Boot all 9 tiles from flash (same firmware.bin as sim-flash test).
  2. Wait for boot_controller to assert cpu_rst_n (boot done).
  3. Verify SRAM[0:3] = first firmware bytes (sanity check).
  4. Host sends CMD 0x03 0xFF  →  assert reset, put tiles in write mode.
  5. For each tile(r,c): CMD 0x04 selects it, then CMD 0x00 writes its unique
     10×10 sub-image (image rows r*10..r*10+9, cols c*10..c*10+9) to SRAM[0..99].
  6. Verify each tile holds its correct 10×10 patch.

SPI timing: SPI half-cycle = SPI_HALF_NS = 500 ns  (sys_clk period = 10 ns).
The double-register synchroniser in host_spi_slave needs >= 2 sys_clk cycles
between SPI edges; 500 ns gives 50 cycles — plenty of margin.
"""

import os
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge, Timer

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(__file__)

FIRMWARE_BIN = os.path.join(_HERE, "firmware.bin")
PBM_FILE     = os.path.join(_HERE,
    "../src/host-to-chip - Paul&Andrea/input.pbm")

SPI_HALF_NS = 500   # ns per SPI half-cycle


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def load_firmware():
    if os.path.exists(FIRMWARE_BIN):
        with open(FIRMWARE_BIN, "rb") as f:
            return list(f.read())
    # Fallback: minimal 4-byte NOP sled
    return [0x13, 0x01, 0x00, 0x40] + [0xAA] * 1020


def load_pbm_bytes(path):
    """Parse a P1 (ASCII) PBM and return a flat list of pixel bytes (0 or 1)."""
    with open(path) as f:
        raw = f.read()

    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            lines.append(line)

    assert lines[0] == 'P1', f"Expected P1 PBM, got: {lines[0]!r}"
    w, h = map(int, lines[1].split())

    tokens = ' '.join(lines[2:]).split()
    pixels = [int(t) for t in tokens]

    assert len(pixels) == w * h, \
        f"Expected {w*h} pixels for {w}x{h}, got {len(pixels)}"
    return w, h, pixels


FIRMWARE = load_firmware()


# ---------------------------------------------------------------------------
# Flash SPI model — responds to the boot_controller's read commands
# ---------------------------------------------------------------------------

async def spi_flash_stream(dut):
    """Coroutine: serve firmware bytes over the flash SPI interface."""
    while True:
        await FallingEdge(dut.flash_csb)
        # Skip 32 bits (8-bit command + 24-bit address)
        for _ in range(32):
            await RisingEdge(dut.flash_clk)
        byte_idx = 0
        while True:
            for bit in range(7, -1, -1):
                await FallingEdge(dut.flash_clk)
                b = FIRMWARE[byte_idx] if byte_idx < len(FIRMWARE) else 0xFF
                dut.flash_miso.value = (b >> bit) & 1
                await RisingEdge(dut.flash_clk)
            byte_idx += 1
            try:
                if int(dut.flash_csb.value) == 1:
                    break
            except Exception:
                break
        dut.flash_miso.value = 0


# ---------------------------------------------------------------------------
# Host SPI helpers
# ---------------------------------------------------------------------------

async def _spi_byte(dut, byte_val):
    """Shift one byte MSB-first; return the byte received on MISO."""
    recv = 0
    for bit in range(7, -1, -1):
        dut.host_mosi.value = (byte_val >> bit) & 1
        await Timer(SPI_HALF_NS, unit='ns')
        dut.host_sclk.value = 1
        recv = (recv << 1) | int(dut.host_miso.value)
        await Timer(SPI_HALF_NS, unit='ns')
        dut.host_sclk.value = 0
    return recv & 0xFF


async def spi_transaction(dut, byte_list):
    """Assert CS, shift byte_list, deassert CS. Returns list of received bytes."""
    dut.host_csb.value = 0
    await Timer(SPI_HALF_NS, unit='ns')
    result = [await _spi_byte(dut, b) for b in byte_list]
    await Timer(SPI_HALF_NS, unit='ns')
    dut.host_csb.value = 1
    await Timer(SPI_HALF_NS * 4, unit='ns')
    return result


async def host_set_reset(dut, assert_rst):
    """CMD 0x03: assert (0xFF) or release (0x00) CPU reset for all tiles."""
    await spi_transaction(dut, [0x03, 0xFF if assert_rst else 0x00])


async def host_set_tile(dut, row, col):
    """CMD 0x04: select tile(row,col) for subsequent writes. row=0xFF = broadcast."""
    await spi_transaction(dut, [0x04, row & 0xFF, col & 0xFF])


async def host_write_sram(dut, addr, data):
    """CMD 0x00: broadcast-write one byte to all 9 tile SRAMs at addr."""
    await spi_transaction(dut, [
        0x00,
        (addr >> 8) & 0x07,   # addr[10:8]
        addr & 0xFF,           # addr[7:0]
        data & 0xFF,
    ])


# ---------------------------------------------------------------------------
# SRAM readback helper (direct memory access via cocotb hierarchy)
# ---------------------------------------------------------------------------

def sram_read(dut, row, col, addr):
    """Read one byte from tile(row,col) SRAM at addr via cocotb hierarchy."""
    mem = dut.mesh_inst.rows[row].cols[col].tile_inst.sram_inst.mem
    val = mem[addr].value
    if not val.is_resolvable:
        return None
    return int(val) & 0xFF


def sram_image(dut, patch, log):
    """Reconstruct and print the full image assembled from all 9 tiles' SRAM patches."""
    log.info("=== Reconstructed image (# = 1, . = 0) ===")
    full_w = 3 * patch
    for img_row in range(3 * patch):
        tr = img_row // patch   # tile row
        pr = img_row % patch    # pixel row within tile
        line = ""
        for tc in range(3):     # tile col
            for pc in range(patch):
                addr = pr * patch + pc
                v = sram_read(dut, tr, tc, addr)
                line += "#" if (v is not None and v != 0) else "."
        log.info(f"  {line}")
    log.info(f"  {'=' * full_w}")


def sram_dump(dut, row, col, length, log, bytes_per_line=16):
    """Print a hex dump of the first `length` bytes of tile(row,col) SRAM."""
    log.info(f"--- SRAM dump tile({row},{col}) [{length} bytes] ---")
    for base in range(0, length, bytes_per_line):
        chunk = []
        for a in range(base, min(base + bytes_per_line, length)):
            v = sram_read(dut, row, col, a)
            chunk.append(f"{v:02x}" if v is not None else "XX")
        log.info(f"  {base:04x}: {' '.join(chunk)}")


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@cocotb.test()
async def host_write_input_pbm(dut):
    """Boot from flash, then overwrite all tile SRAMs with input.pbm via host SPI."""

    # ----- Initialise SPI pins -----
    dut.flash_miso.value = 0
    dut.host_csb.value   = 1
    dut.host_sclk.value  = 0
    dut.host_mosi.value  = 0

    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    cocotb.start_soon(spi_flash_stream(dut))

    # ----- Reset -----
    dut.rst.value = 1
    for _ in range(8):
        await RisingEdge(dut.clk)
    dut.rst.value = 0

    # ----- Wait for flash boot to complete (cpu_rst_n goes high) -----
    dut._log.info("Waiting for flash boot to complete ...")
    boot_cycle = None
    for cycle in range(700_000):
        await RisingEdge(dut.clk)
        try:
            v = dut.mesh_inst.cpu_rst_n.value
            if v.is_resolvable and int(v) == 1:
                boot_cycle = cycle
                break
        except Exception:
            pass
        if cycle % 100_000 == 0 and cycle > 0:
            dut._log.info(f"  heartbeat: cycle {cycle}")

    assert boot_cycle is not None, "Timeout: cpu_rst_n never asserted"
    dut._log.info(f"Boot complete at cycle {boot_cycle}")

    # ----- Settle and sanity-check firmware in SRAM -----
    for _ in range(20):
        await RisingEdge(dut.clk)

    fw_exp = FIRMWARE[:4]
    dut._log.info(f"Firmware first 4 bytes expected: {[hex(b) for b in fw_exp]}")
    fw_ok = True
    for addr, exp in enumerate(fw_exp):
        got = sram_read(dut, 0, 0, addr)
        if got is None or got != exp:
            dut._log.error(
                f"Pre-write SRAM[{addr}]: got {hex(got) if got is not None else 'X'}, "
                f"expected {hex(exp)}"
            )
            fw_ok = False
    if fw_ok:
        dut._log.info("Pre-write firmware sanity check: PASS")

    # ----- Load input.pbm -----
    w, h, pixels = load_pbm_bytes(PBM_FILE)
    dut._log.info(f"Loaded input.pbm: {w}×{h} = {len(pixels)} bytes")

    # ----- Phase 1: assert host reset (puts all tiles in write mode) -----
    dut._log.info("Asserting host reset (tiles enter boot-write mode) ...")
    await host_set_reset(dut, assert_rst=True)
    for _ in range(10):
        await RisingEdge(dut.clk)

    # ----- Phase 2: per-tile write — each tile gets its 10×10 patch -----
    # tile(r,c) receives the 10×10 sub-image at image rows [r*10..r*10+9],
    # image cols [c*10..c*10+9], stored linearly at SRAM addr 0..99.
    PATCH = 10
    dut._log.info("Writing per-tile 10×10 patches from input.pbm ...")
    for r in range(3):
        for c in range(3):
            await host_set_tile(dut, r, c)
            written = 0
            for i in range(PATCH):
                for j in range(PATCH):
                    src = (r * PATCH + i) * w + (c * PATCH + j)
                    await host_write_sram(dut, i * PATCH + j, pixels[src])
                    written += 1
            dut._log.info(f"  tile({r},{c}): wrote {written} bytes")

    dut._log.info("Write complete.")

    # settle
    for _ in range(20):
        await RisingEdge(dut.clk)

    # ----- Phase 3: verify each tile has its correct 10×10 patch -----
    dut._log.info("Verifying per-tile SRAM contents ...")
    total_errors = 0

    for r in range(3):
        for c in range(3):
            tile_errors = 0
            for i in range(PATCH):
                for j in range(PATCH):
                    sram_addr = i * PATCH + j
                    src = (r * PATCH + i) * w + (c * PATCH + j)
                    exp = pixels[src]
                    got = sram_read(dut, r, c, sram_addr)
                    if got is None:
                        dut._log.error(
                            f"tile({r},{c}) SRAM[{sram_addr}]: unresolvable (X/Z)"
                        )
                        tile_errors += 1
                    elif got != exp:
                        dut._log.error(
                            f"tile({r},{c}) SRAM[{sram_addr:3d}]: "
                            f"got 0x{got:02x}, expected 0x{exp:02x}"
                        )
                        tile_errors += 1
            if tile_errors == 0:
                dut._log.info(f"tile({r},{c}): PASS (10×10 patch correct)")
            total_errors += tile_errors
        await RisingEdge(dut.clk)

    # ----- Phase 4: SRAM dump for all 9 tiles (full 10×10 patch = 100 bytes) -----
    dut._log.info("=== SRAM contents (100-byte 10×10 patch per tile) ===")
    for r in range(3):
        for c in range(3):
            sram_dump(dut, r, c, PATCH * PATCH, dut._log)

    sram_image(dut, PATCH, dut._log)

    if total_errors == 0:
        dut._log.info("ALL 9 TILES PASS — host-to-chip write verified.")
    else:
        raise AssertionError(
            f"host-to-chip write FAILED: {total_errors} byte mismatches across tiles"
        )
