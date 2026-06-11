# SPDX-FileCopyrightText: © 2025 Project Template Contributors
# SPDX-License-Identifier: Apache-2.0

import os
import random
import logging
import re
import tempfile
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import Timer, Edge, RisingEdge, FallingEdge, ClockCycles
from cocotb_tools.runner import get_runner

sim = os.getenv("SIM", "icarus")
pdk_root = os.getenv("PDK_ROOT", Path("~/.ciel").expanduser())
pdk = os.getenv("PDK", "gf180mcuD")
scl = os.getenv("SCL", "gf180mcu_as_sc_mcu7t3v3")
gl = os.getenv("GL", False)
gl_tile = os.getenv("GL_TILE", "")   # path to mesh_tile.nl.v for mixed RTL/GL sim
slot = os.getenv("SLOT", "1x1")

hdl_toplevel = "chip_top"

async def set_defaults(dut):
    # CS_N must be inactive (high) from the start — if it starts low, the
    # 0→1 edge after reset deasserts triggers a spurious spi_debug write commit.
    dut.input_PAD.value = _CS_N

async def enable_power(dut):
    dut.VDD.value = 1
    dut.VSS.value = 0

async def start_clock(clock, freq=50):
    """Start the clock @ freq MHz"""
    c = Clock(clock, 1 / freq * 1000, "ns")
    cocotb.start_soon(c.start())


async def reset(reset, active_low=True, time_ns=1000):
    """Reset dut"""
    cocotb.log.info("Reset asserted...")

    reset.value = not active_low
    await Timer(time_ns, "ns")
    reset.value = active_low

    cocotb.log.info("Reset deasserted.")


async def start_up(dut):
    """Startup sequence"""
    await set_defaults(dut)
    if gl:
        await enable_power(dut)
    await start_clock(dut.clk_PAD)
    await reset(dut.rst_n_PAD)


# ── DFT SPI Debug Interface ────────────────────────────────────────────────
# SPI half-period. Must satisfy: HALF_PERIOD >> 4 * sys_clk_period.
# At 25 MHz sys_clk (40 ns period), 500 ns gives ~12 sys_clk cycles per
# half-period, safely above the 4-cycle synchroniser requirement.
SPI_HALF_NS = 500

_DFT_MODE = 1 << 3   # input_PAD[3]
_CS_N     = 1 << 2   # input_PAD[2]  (active low)
_MOSI     = 1 << 1   # input_PAD[1]
_SCLK     = 1 << 0   # input_PAD[0]

def _miso(dut):
    # Read bidir_PAD[0] (spi_miso). Other bits may be X/Z; use string LSB.
    s = str(dut.bidir_PAD.value)  # e.g. "XXXX...X1" or "XXXX...X0"
    return 1 if s[-1] == '1' else 0

async def dft_spi_transact(dut, write, tile_id, addr, wdata=0x00):
    """
    Drive one 32-bit spi_debug frame and return the 8-bit read data.
    Frame: [31]=R/W  [30:27]=tile_id  [26:16]=addr  [15:8]=dc  [7:0]=wdata
    For reads (write=False), MISO is sampled during bits 22-29 (0-indexed).
    """
    frame = ((1 if write else 0) << 31) | ((tile_id & 0xF) << 27) \
        | ((addr & 0x7FF) << 16) | (wdata & 0xFF)

    miso_bits = []

    # Assert CS (cs_n low), keep dft_mode high
    dut.input_PAD.value = _DFT_MODE
    await Timer(SPI_HALF_NS, "ns")

    for i in range(32):
        mosi = (frame >> (31 - i)) & 1

        # Rising edge: drive MOSI
        dut.input_PAD.value = _DFT_MODE | (mosi * _MOSI) | _SCLK
        await Timer(SPI_HALF_NS, "ns")

        # Falling edge: slave drives MISO
        # spi_debug drives MISO when bit_cnt is 23..30
        dut.input_PAD.value = _DFT_MODE | (mosi * _MOSI)
        await Timer(SPI_HALF_NS // 2, "ns")  # wait for MISO to settle

        if not write and 22 <= i <= 29:
            miso_bits.append(_miso(dut))

        await Timer(SPI_HALF_NS // 2, "ns")

    # Deassert CS - triggers write commit inside spi_debug
    dut.input_PAD.value = _DFT_MODE | _CS_N
    await Timer(SPI_HALF_NS, "ns")

    if miso_bits:
        result = 0
        for b in miso_bits:
            result = (result << 1) | b
        return result
    return 0

async def dft_write(dut, tile_id, addr, data):
    await dft_spi_transact(dut, write=True, tile_id=tile_id, addr=addr, wdata=data)

async def dft_read(dut, tile_id, addr):
    return await dft_spi_transact(dut, write=False, tile_id=tile_id, addr=addr)


# ============================================================================
# Tile-ID helpers
# ============================================================================

def tile6(row, col):
    """6-bit TILE_ID used by mesh_router: {row[2:0], col[2:0]}"""
    return ((row & 0x7) << 3) | (col & 0x7)

def tile4(row, col):
    """4-bit DFT tile_id used by mesh_rxc / spi_debug: {row[1:0], col[1:0]}"""
    return ((row & 0x3) << 2) | (col & 0x3)


# ============================================================================
# Hand-assembled RISC-V instruction helpers
# ============================================================================

def _le4(word):
    """32-bit word → 4 little-endian bytes"""
    return [(word >> s) & 0xFF for s in (0, 8, 16, 24)]

def _lui(rd, imm20):
    """lui rd, imm20  →  4 little-endian bytes"""
    assert 0 <= rd <= 31
    word = ((imm20 & 0xFFFFF) << 12) | ((rd & 0x1F) << 7) | 0x37
    return _le4(word)

def _addi(rd, rs1, imm12):
    """addi rd, rs1, imm12  (imm12 is signed)"""
    assert 0 <= rd <= 31 and 0 <= rs1 <= 31
    word = ((imm12 & 0xFFF) << 20) | ((rs1 & 0x1F) << 15) | (0b000 << 12) | ((rd & 0x1F) << 7) | 0x13
    return _le4(word)

def _sw(rs2, rs1, imm12):
    """sw rs2, imm12(rs1)"""
    imm = imm12 & 0xFFF
    word = (((imm >> 5) & 0x7F) << 25) | ((rs2 & 0x1F) << 20) | ((rs1 & 0x1F) << 15) | (0b010 << 12) | ((imm & 0x1F) << 7) | 0x23
    return _le4(word)

def _lw(rd, rs1, imm12):
    """lw rd, imm12(rs1)"""
    word = ((imm12 & 0xFFF) << 20) | ((rs1 & 0x1F) << 15) | (0b010 << 12) | ((rd & 0x1F) << 7) | 0x03
    return _le4(word)

def _sb(rs2, rs1, imm12):
    """sb rs2, imm12(rs1)"""
    imm = imm12 & 0xFFF
    word = (((imm >> 5) & 0x7F) << 25) | ((rs2 & 0x1F) << 20) | ((rs1 & 0x1F) << 15) | (0b000 << 12) | ((imm & 0x1F) << 7) | 0x23
    return _le4(word)

def _jal_spin():
    """jal zero, 0  — infinite spin"""
    return _le4(0x0000006F)

def _nop():
    """addi x0, x0, 0"""
    return _le4(0x00000013)

# Register aliases
_A0 = 10   # x10
_A1 = 11   # x11
_A2 = 12   # x12
_A3 = 13   # x13


# ============================================================================
# RISC-V program builders for mesh routing tests
# ============================================================================

def build_sender_program(dest_row, dest_col, payload_byte):
    """
    Build a RISC-V program that injects one mesh flit to (dest_row, dest_col)
    carrying payload_byte, then spins.

    Flit encoding (36-bit):
      [35]    = valid  (set by router on injection write)
      [34:29] = dest TILE_ID = {dest_row[2:0], dest_col[2:0]}
      [28:0]  = payload (zero-extended from dat_o[28:0])

    CPU injection write (wb_adr = 0x80000000):
      dat_o[31:26] = dest TILE_ID (router extracts bits [31:26] → flit[34:29])
      dat_o[28:0]  = payload

    Instructions:
      lui  a0, 0x80000    # a0 = 0x80000000 (inject addr)
      lui  a1, upper20    # a1 = upper20 << 12
      addi a1, a1, lower12
      sw   a1, 0(a0)      # inject flit
      jal  zero, 0        # spin
    """
    dest_id6 = tile6(dest_row, dest_col)
    dat_o    = (dest_id6 << 26) | (payload_byte & 0xFF)

    # Split into LUI upper (imm[31:12]) and ADDI lower (sign-extended imm[11:0])
    lower12 = dat_o & 0xFFF
    upper20 = dat_o >> 12
    if lower12 & 0x800:          # ADDI sign-extends; compensate in LUI
        upper20 = (upper20 + 1) & 0xFFFFF

    # Convert lower12 to a signed 12-bit value for _addi
    signed_lower = lower12 if not (lower12 & 0x800) else lower12 - 0x1000

    prog = []
    prog += _lui(_A0, 0x80000)          # a0 = 0x80000000
    prog += _lui(_A1, upper20)          # a1 = upper20 << 12
    prog += _addi(_A1, _A1, signed_lower)
    prog += _sw(_A1, _A0, 0)            # sw a1, 0(a0)
    prog += _jal_spin()
    return prog


def build_receiver_program(nop_count=32):
    """
    Build a RISC-V program that:
      1. Sets up a2 = 0x80000004 (router FIFO ejection read address)
      2. Sets up a3 = 0x300      (SRAM result byte address)
      3. Stalls for nop_count NOPs (give sender a head-start)
      4. Reads the payload word from the FIFO into a1
      5. Stores the low byte of a1 to SRAM[0x300]
      6. Spins

    Instructions:
      lui  a0, 0x80000      # a0 = 0x80000000
      addi a2, a0, 4        # a2 = 0x80000004
      lui  a3, 0            # a3 = 0
      addi a3, a3, 0x300    # a3 = 0x300
      nop × nop_count
      lw   a1, 0(a2)        # payload from FIFO
      sb   a1, 0(a3)        # SRAM[0x300] = low byte
      jal  zero, 0          # spin
    """
    prog = []
    prog += _lui(_A0, 0x80000)          # a0 = 0x80000000
    prog += _addi(_A2, _A0, 4)          # a2 = 0x80000004
    prog += _lui(_A3, 0)                # a3 = 0
    prog += _addi(_A3, _A3, 0x300)      # a3 = 0x300
    for _ in range(nop_count):
        prog += _nop()
    prog += _lw(_A1, _A2, 0)            # lw a1, 0(a2)
    prog += _sb(_A1, _A3, 0)            # sb a1, 0(a3)
    prog += _jal_spin()
    return prog


# ============================================================================
# DFT tile initialisation helper
# ============================================================================

async def _init_tile(dut, row, col, program_bytes):
    """
    Fully initialise one tile via DFT while dft_mode is already asserted:

      1. Write program into SRAM[0x000 .. len-1].
      2. Verify every written byte (catches DFT path bugs early).
      3. Zero the SERV RF area (SRAM[0x770..0x7FF]) so that bank1 cen_fell
         is armed and all 32 RISC-V registers start at zero.
      4. Touch SRAM[0x000] with a DFT read to reset bank_sel_q to 0 so that
         SERV's first instruction fetch (PC=0, bank0) returns the correct byte
         rather than the stale bank1 output left by the RF zeroing.

    The caller must have already asserted dft_mode before calling this.
    """
    tid4 = tile4(row, col)
    label = f"tile({row},{col})"

    # ── 1. Write program ─────────────────────────────────────────────────────
    dut._log.info(f"[INIT] {label}: writing {len(program_bytes)}-byte program...")
    for i, byte in enumerate(program_bytes):
        await dft_write(dut, tid4, 0x000 + i, byte)

    # ── 2. Verify program bytes ──────────────────────────────────────────────
    dut._log.info(f"[INIT] {label}: verifying program...")
    for i, expected in enumerate(program_bytes):
        got = await dft_read(dut, tid4, 0x000 + i)
        assert got == expected, (
            f"{label} program verify failed at SRAM[0x{i:03X}]: "
            f"wrote 0x{expected:02X}, read 0x{got:02X}"
        )

    # ── 3. Zero RF area (SRAM[0x770..0x7FF]) ────────────────────────────────
    # This arms bank1's cen_fell flag (previously untouched) and zeroes every
    # SERV register so no garbage values corrupt instruction decode.
    RF_START = 0x770
    RF_END   = 0x7FF
    dut._log.info(f"[INIT] {label}: zeroing RF area 0x{RF_START:03X}..0x{RF_END:03X}...")
    for addr in range(RF_START, RF_END + 1):
        await dft_write(dut, tid4, addr, 0x00)

    # ── 4. Reset bank_sel_q to 0 (bank0 touch) ──────────────────────────────
    # The last RF zero write was to bank1 (A[10]=1), leaving bank_sel_q=1.
    # One bank0 read flips bank_sel_q=0 so SERV's first PC=0 fetch is correct.
    await dft_read(dut, tid4, 0x000)
    dut._log.info(f"[INIT] {label}: ready (bank_sel_q reset to 0).")


# ============================================================================
# All nine tiles as (row, col)
# ============================================================================

_ALL_TILES = [
    (0, 0), (0, 1), (0, 2),
    (1, 0), (1, 1), (1, 2),
    (2, 0), (2, 1), (2, 2),
]


# ============================================================================
# TEST 1 — DFT single write/read on tile (0,0)
# ============================================================================

@cocotb.test(skip=True)
async def test_counter(dut):
    """Run the counter test"""

    logger = logging.getLogger("my_testbench")
    logger.info("Startup sequence...")
    await start_up(dut)
    logger.info("Running the test...")
    await ClockCycles(dut.clk_PAD, 10)
    dut.input_PAD.value = -1
    await ClockCycles(dut.clk_PAD, 100)
    assert dut.bidir_PAD.value == 100 - 1
    logger.info("Done!")


@cocotb.test()
async def test_dft_write_read(dut):
    """
    Write a byte to tile (0,0) SRAM via SPI debug interface, read it back,
    verify it matches. Cores are held in reset by dft_mode so they cannot
    interfere with the SRAM.
    """
    await start_up(dut)

    dut.input_PAD.value = _DFT_MODE | _CS_N
    await ClockCycles(dut.clk_PAD, 4)

    tile_id = 0b0000    # tile (row 0, col 0)
    addr = 0x050
    wdata = 0xA5

    await dft_write(dut, tile_id, addr, wdata)
    rdata = await dft_read(dut, tile_id, addr)

    dut._log.info(f"DFT write =0x{wdata:02x} read = 0x{rdata:02x}")
    assert rdata == wdata, f"DFT read back failed: wrote 0x{wdata:02x}, got 0x{rdata:02x}"

    dut.input_PAD.value = _CS_N
    dut._log.info("test_dft_write_read PASSED")


# ============================================================================
# TEST 2 — DFT sweep: all 9 tiles, 3 patterns, 3 addresses
# ============================================================================

@cocotb.test(skip=True)
async def test_dft_sweep(dut):
    """
    Thorough DFT SRAM sweep across all 9 tiles and multiple addresses/patterns.

    For every tile in the 3×3 mesh, writes three different byte patterns to
    three different SRAM addresses:
      - 0xA5 @ 0x000 (alternating bits, low address)
      - 0x5A @ 0x1FF (inverse alternating, mid address)
      - 0xFF @ 0x3FE (all ones, high address)
    Then reads every location back and asserts it matches.

    Tile IDs are {row[1:0], col[1:0]}:
      (0,0)=0x0  (0,1)=0x1  (0,2)=0x2
      (1,0)=0x4  (1,1)=0x5  (1,2)=0x6
      (2,0)=0x8  (2,1)=0x9  (2,2)=0xA
    """
    await start_up(dut)

    ALL_TILES = [
        (0, 0, 0b0000), (0, 1, 0b0001), (0, 2, 0b0010),
        (1, 0, 0b0100), (1, 1, 0b0101), (1, 2, 0b0110),
        (2, 0, 0b1000), (2, 1, 0b1001), (2, 2, 0b1010),
    ]
    TEST_VECTORS = [
        (0x000, 0xA5),
        (0x1FF, 0x5A),
        (0x3FE, 0xFF),
    ]

    dut.input_PAD.value = _DFT_MODE | _CS_N
    await ClockCycles(dut.clk_PAD, 4)

    for row, col, tid in ALL_TILES:
        for addr, data in TEST_VECTORS:
            await dft_write(dut, tid, addr, data)
        dut._log.info(f"  tile ({row},{col}): wrote {len(TEST_VECTORS)} locations")

    failures = []
    for row, col, tid in ALL_TILES:
        for addr, expected in TEST_VECTORS:
            got = await dft_read(dut, tid, addr)
            if got != expected:
                failures.append(
                    f"tile({row},{col}) addr=0x{addr:03x}: wrote 0x{expected:02x} got 0x{got:02x}"
                )
            else:
                dut._log.info(f"  tile({row},{col}) addr=0x{addr:03x} ok: 0x{got:02x}")

    dut.input_PAD.value = _CS_N

    assert not failures, "DFT sweep failures:\n" + "\n".join(failures)
    dut._log.info(
        f"test_dft_sweep PASSED — {len(ALL_TILES) * len(TEST_VECTORS)} locations verified"
    )


# ============================================================================
# TEST 3 — DFT boot hello: SERV CPU writes 'H' to SRAM on tile (0,0)
# ============================================================================

@cocotb.test()
async def test_dft_boot_hello(dut):
    """
    Boot a RISC-V 'hello world' on tile (0,0) using DFT as the bootloader.

    Sequence:
      1. Assert dft_mode — SERV cores freeze, boot_controller runs but its
         SRAM writes are blocked by the dft_mode mux priority.
      2. Write a 24-byte hand-assembled RISC-V program into tile(0,0)
         SRAM[0x000..0x017] via DFT.
             lui  a0, 0             # a0 = 0
             addi a0, a0, 0x200    # a0 = 0x200
             lui  a1, 0             # a1 = 0
             addi a1, a1, 0x48     # a1 = 0x48 ('H')
             sb   a1, 0(a0)        # SRAM[0x200] = 'H'
             jal  zero, 0          # spin
      3. Wait ~80k cycles for boot_controller to finish.
      4. Zero SERV RF area (SRAM[0x770..0x7FF]) to arm bank1 cen_fell and
         put all registers in a known-zero state.
      5. Touch SRAM[0x000] to reset bank_sel_q → 0.
      6. Release dft_mode — SERV executes from PC=0.
      7. Wait 10k cycles, re-assert dft_mode, read SRAM[0x200].
      8. Assert it equals 0x48 ('H').
    """
    await start_up(dut)

    dut.input_PAD.value = _DFT_MODE | _CS_N
    await ClockCycles(dut.clk_PAD, 4)

    PROG_BASE  = 0x000
    HELLO_PROG = [
        0x37, 0x05, 0x00, 0x00,  # lui  a0, 0
        0x13, 0x05, 0x05, 0x20,  # addi a0, a0, 0x200
        0xB7, 0x05, 0x00, 0x00,  # lui  a1, 0
        0x93, 0x85, 0x85, 0x04,  # addi a1, a1, 0x48  ('H')
        0x23, 0x00, 0xB5, 0x00,  # sb   a1, 0(a0)
        0x6F, 0x00, 0x00, 0x00,  # jal  zero, 0
    ]
    RESULT_ADDR = 0x200
    EXPECTED    = 0x48   # 'H'

    dut._log.info("Writing RISC-V hello-world program to tile(0,0) via DFT...")
    for i, byte in enumerate(HELLO_PROG):
        await dft_write(dut, tile_id=0b0000, addr=PROG_BASE + i, data=byte)

    dut._log.info("Verifying program bytes via DFT read-back...")
    for i, expected_byte in enumerate(HELLO_PROG):
        phys = PROG_BASE + i
        got = await dft_read(dut, tile_id=0b0000, addr=phys)
        assert got == expected_byte, (
            f"Program verify failed at SRAM[0x{phys:03X}]: "
            f"wrote 0x{expected_byte:02X}, read back 0x{got:02X}"
        )
    dut._log.info("Program verified — all 24 bytes match.")

    dut._log.info("Waiting ~80k cycles for boot_controller to release cpu_reset_n...")
    await ClockCycles(dut.clk_PAD, 80000)

    # Re-check program bytes after boot wait
    dut._log.info("Diagnostic: re-reading program bytes after boot wait...")
    prog_ok = True
    for i, expected_byte in enumerate(HELLO_PROG):
        got = await dft_read(dut, tile_id=0b0000, addr=PROG_BASE + i)
        if got != expected_byte:
            dut._log.warning(
                f"SRAM[0x{PROG_BASE+i:03X}] CORRUPTED: "
                f"expected 0x{expected_byte:02X}, got 0x{got:02X}"
            )
            prog_ok = False
    if prog_ok:
        dut._log.info("Program bytes intact after boot wait.")
    else:
        dut._log.warning("Re-writing program bytes...")
        for i, byte in enumerate(HELLO_PROG):
            await dft_write(dut, tile_id=0b0000, addr=PROG_BASE + i, data=byte)

    # Zero RF area to arm bank1 cen_fell and clear all registers
    RF_AREA_START = 0x770
    RF_AREA_END   = 0x7FF
    dut._log.info(
        f"Initializing RF area SRAM[0x{RF_AREA_START:03X}..0x{RF_AREA_END:03X}] to zero..."
    )
    for addr in range(RF_AREA_START, RF_AREA_END + 1):
        await dft_write(dut, tile_id=0b0000, addr=addr, data=0x00)
    dut._log.info("RF area initialized — bank1 operational, all registers = 0.")

    # Reset bank_sel_q to 0
    await dft_read(dut, tile_id=0b0000, addr=0x000)
    dut._log.info("bank_sel_q reset to 0 (bank0 touch done).")

    dut.input_PAD.value = _CS_N
    dut._log.info("Released dft_mode — SERV core now executing...")

    await ClockCycles(dut.clk_PAD, 10000)

    dut.input_PAD.value = _DFT_MODE | _CS_N
    await ClockCycles(dut.clk_PAD, 4)

    # Diagnostics: read RF slots for a0/a1
    a0_b = [await dft_read(dut, 0b0000, a) for a in (0x7D7, 0x7D6, 0x7D5, 0x7D4)]
    a0_val = a0_b[0] | (a0_b[1] << 8) | (a0_b[2] << 16) | (a0_b[3] << 24)
    dut._log.info(f"RF a0 = 0x{a0_val:08x}")
    a1_b = [await dft_read(dut, 0b0000, a) for a in (0x7D3, 0x7D2, 0x7D1, 0x7D0)]
    a1_val = a1_b[0] | (a1_b[1] << 8) | (a1_b[2] << 16) | (a1_b[3] << 24)
    dut._log.info(f"RF a1 = 0x{a1_val:08x}")

    result = await dft_read(dut, tile_id=0b0000, addr=RESULT_ADDR)
    dut._log.info(
        f"SRAM[0x{RESULT_ADDR:03X}] = 0x{result:02X}  "
        f"(expected 0x{EXPECTED:02X} = '{chr(EXPECTED)}')"
    )

    dut.input_PAD.value = _CS_N
    assert result == EXPECTED, \
        f"DFT boot failed: expected 0x{EXPECTED:02X} ('{chr(EXPECTED)}'), got 0x{result:02X}"
    dut._log.info("test_dft_boot_hello PASSED — SERV booted via DFT and wrote 'H' to SRAM!")


# ============================================================================
# TEST 4 — Mesh route smoke: (0,0) CPU sends one flit to (0,1) via router
# ============================================================================

@cocotb.test()
async def test_mesh_route_smoke(dut):
    """
    Smoke test: tile (0,0) CPU sends a single flit to tile (0,1).
    Tile (0,1) CPU reads the payload from its router ejection FIFO and
    stores the byte into its SRAM[0x300].
    We verify via DFT that SRAM[0x300] of tile (0,1) equals the sent byte.

    This exercises every layer end-to-end:
      * DFT write/read on two tiles
      * SERV CPU executing real RISC-V (injection write, FIFO read, store byte)
      * mesh_router XY routing: (0,0) -> east -> (0,1)
      * Router ejection FIFO in tile (0,1)
    """
    PAYLOAD     = 0xAB
    SRC_ROW, SRC_COL = 0, 0
    DST_ROW, DST_COL = 0, 1
    RESULT_ADDR = 0x300

    await start_up(dut)

    # Assert DFT mode — all CPUs frozen, boot_controller SRAM writes blocked
    dut.input_PAD.value = _DFT_MODE | _CS_N
    await ClockCycles(dut.clk_PAD, 4)

    sender_prog   = build_sender_program(DST_ROW, DST_COL, PAYLOAD)
    receiver_prog = build_receiver_program(nop_count=32)

    dut._log.info(f"Sender   program: {len(sender_prog)} bytes")
    dut._log.info(f"Receiver program: {len(receiver_prog)} bytes")

    await _init_tile(dut, SRC_ROW, SRC_COL, sender_prog)
    await _init_tile(dut, DST_ROW, DST_COL, receiver_prog)

    dut._log.info("Waiting for boot_controller to release cpu_reset_n...")
    await ClockCycles(dut.clk_PAD, 80_000)

    dut.input_PAD.value = _CS_N
    dut._log.info("DFT released — CPUs executing.")

    await ClockCycles(dut.clk_PAD, 20_000)

    dut.input_PAD.value = _DFT_MODE | _CS_N
    await ClockCycles(dut.clk_PAD, 4)

    result = await dft_read(dut, tile4(DST_ROW, DST_COL), RESULT_ADDR)
    dut._log.info(
        f"tile({DST_ROW},{DST_COL}) SRAM[0x{RESULT_ADDR:03X}] = 0x{result:02X} "
        f"(expected 0x{PAYLOAD:02X})"
    )

    dut.input_PAD.value = _CS_N
    assert result == PAYLOAD, (
        f"Mesh route smoke test FAILED: "
        f"expected 0x{PAYLOAD:02X}, got 0x{result:02X}"
    )
    dut._log.info("test_mesh_route_smoke PASSED — flit travelled (0,0)->(0,1) correctly.")


# ============================================================================
# TEST 5 — Pure DFT mesh: unique sentinel byte per tile  (fast, no CPU)
#
# Confirms that the DFT SPI path can reach every tile's SRAM independently
# and correctly. No CPUs are ever released; no boot_controller wait needed.
#
# Tile IDs (DFT 4-bit encoding = {row[1:0], col[1:0]}):
#   (0,0)=0x0  (0,1)=0x1  (0,2)=0x2
#   (1,0)=0x4  (1,1)=0x5  (1,2)=0x6
#   (2,0)=0x8  (2,1)=0x9  (2,2)=0xA
# ============================================================================

@cocotb.test(skip=True)
async def test_mesh_dft_unique_byte(dut):
    """
    Write a unique sentinel byte to SRAM[0x300] of every tile via DFT,
    then read every tile back and assert it matches.

    The sentinel for tile (row, col) is:
        (tile4(row, col) << 4) | 0xC   e.g. tile(1,2)=0x6 → 0x6C

    This makes the expected value obvious in waveforms and log output,
    and ensures no two tiles share the same expected byte so aliasing
    bugs are caught immediately.

    No CPUs are released.  Total SPI transactions: 9 writes + 9 reads = 18.
    Expected runtime: a few seconds.
    """
    RESULT_ADDR = 0x300

    await start_up(dut)

    dut.input_PAD.value = _DFT_MODE | _CS_N
    await ClockCycles(dut.clk_PAD, 4)

    # ── Write phase ──────────────────────────────────────────────────────────
    dut._log.info("test_mesh_dft_unique_byte: write phase...")
    sentinels = {}
    for (row, col) in _ALL_TILES:
        tid  = tile4(row, col)
        byte = ((tid & 0xF) << 4) | 0xC   # e.g. tile(0,0)=0x0C, tile(1,2)=0x6C
        sentinels[(row, col)] = byte
        await dft_write(dut, tid, RESULT_ADDR, byte)
        dut._log.info(f"  wrote tile({row},{col}) tid=0x{tid:X} → SRAM[0x{RESULT_ADDR:03X}] = 0x{byte:02X}")

    # ── Read-back phase ──────────────────────────────────────────────────────
    dut._log.info("test_mesh_dft_unique_byte: read-back phase...")
    failures = []
    for (row, col) in _ALL_TILES:
        tid      = tile4(row, col)
        expected = sentinels[(row, col)]
        got      = await dft_read(dut, tid, RESULT_ADDR)
        status   = "OK" if got == expected else "FAIL"
        dut._log.info(
            f"  tile({row},{col}) SRAM[0x{RESULT_ADDR:03X}] = 0x{got:02X} "
            f"(expected 0x{expected:02X}) {status}"
        )
        if got != expected:
            failures.append(
                f"tile({row},{col}) tid=0x{tid:X}: "
                f"wrote 0x{expected:02X}, read 0x{got:02X}"
            )

    dut.input_PAD.value = _CS_N

    assert not failures, (
        f"test_mesh_dft_unique_byte FAILED — {len(failures)}/9 tiles:\n"
        + "\n".join(failures)
    )
    dut._log.info("test_mesh_dft_unique_byte PASSED — all 9 tiles reachable and distinct.")


# ============================================================================
# TEST 6 — Pure DFT mesh: 3 fault-detection patterns × 3 addresses × 9 tiles
# ============================================================================

@cocotb.test(skip=True)
async def test_mesh_dft_all_tiles_patterns(dut):
    """
    Write three fault-detection patterns to three different SRAM addresses
    in every tile, then read them all back and assert correctness.

    Patterns and addresses:
        0xA5  @ 0x100   — alternating bits  (1010_0101), low-mid address
        0x5A  @ 0x200   — inverse pattern   (0101_1010), mid address
        0xFF  @ 0x300   — all ones          (1111_1111), upper address

    Each pattern is XORed with the tile_id so every tile holds unique values
    at every address — aliasing bugs become immediately visible.

    Catches:
      * Data-line stuck-at-0 or stuck-at-1 faults (0xA5 vs 0x5A cover all bits)
      * Address-line faults (three different addresses exercise A[8:6])
      * Tile-select faults (each tile gets different values at the same addresses)

    All 9 tiles × 3 vectors = 27 writes + 27 reads = 54 SPI transactions.
    Expected runtime: under a minute.
    """
    # (address, pattern) pairs — chosen to stress both data and address lines
    VECTORS = [
        (0x100, 0xA5),   # alternating bits
        (0x200, 0x5A),   # inverse alternating
        (0x300, 0xFF),   # all ones
    ]

    await start_up(dut)

    dut.input_PAD.value = _DFT_MODE | _CS_N
    await ClockCycles(dut.clk_PAD, 4)

    # ── Write phase: fill every tile ─────────────────────────────────────────
    dut._log.info("test_mesh_dft_all_tiles_patterns: write phase...")
    for (row, col) in _ALL_TILES:
        tid = tile4(row, col)
        for (addr, pattern) in VECTORS:
            # XOR the pattern with the tile_id so every tile has unique values
            # at every address — makes aliasing bugs immediately visible.
            byte = pattern ^ tid
            await dft_write(dut, tid, addr, byte)
        dut._log.info(
            f"  tile({row},{col}) tid=0x{tid:X}: "
            f"wrote {len(VECTORS)} locations"
        )

    # ── Read-back phase: verify every tile ───────────────────────────────────
    dut._log.info("test_mesh_dft_all_tiles_patterns: read-back phase...")
    failures = []
    for (row, col) in _ALL_TILES:
        tid = tile4(row, col)
        for (addr, pattern) in VECTORS:
            expected = pattern ^ tid
            got      = await dft_read(dut, tid, addr)
            status   = "OK" if got == expected else "FAIL"
            dut._log.info(
                f"  tile({row},{col}) addr=0x{addr:03X}: "
                f"got 0x{got:02X} (expected 0x{expected:02X}) {status}"
            )
            if got != expected:
                failures.append(
                    f"tile({row},{col}) addr=0x{addr:03X}: "
                    f"expected 0x{expected:02X}, got 0x{got:02X}"
                )

    dut.input_PAD.value = _CS_N

    assert not failures, (
        f"test_mesh_dft_all_tiles_patterns FAILED — {len(failures)}/27 checks:\n"
        + "\n".join(failures)
    )
    dut._log.info(
        f"test_mesh_dft_all_tiles_patterns PASSED — "
        f"all 9 tiles × {len(VECTORS)} patterns verified."
    )

# # ============================================================================
# # TEST 7 — Checkerboard readback: boot main.c, read grid from all tiles
# #
# # Unlike tests 1-6 which either load hand-assembled programs via DFT or do
# # pure DFT SRAM sweeps, this test lets the boot_controller load main.c from
# # SPI flash normally, waits for the CPUs to run at least one iteration, then
# # reads back the 10×10 checkerboard grid from every tile via DFT SPI and
# # prints a side-by-side expected vs actual comparison.
# # ============================================================================

# # Grid / mesh constants — must match main.c
# _CB_SIZE   = 10     # grid is SIZE × SIZE bytes
# _CB_MESH_R = 2
# _CB_MESH_C = 2

# # Physical SRAM addresses (CPU byte addr = DFT addr, direct mapping)
# _SRAM_GRID_BASE   = 0x0500          # 10×10 grid, row-major
# _DEBUG_ITER_COUNT = 0x0700 + 28     # 0x071C, 4-byte little-endian word

# # Boot + poll budget
# _CB_BOOT_CYCLES    = 80_000
# _CB_POLL_INTERVAL  = 100            # clock cycles between iter polls
# _CB_POLL_MAX_TRIES = 5_000          # give up after this many polls


# def _cb_hw_tile_id(r, c):
#     """Hardware fill value written by main.c: ((row<<3)|col)+1"""
#     return ((r << 3) | c) + 1


# def _cb_expected_grid(r, c, iteration):
#     """
#     Expected 10×10 grid for tile (r,c) at the given iteration.
#     cell(row,col) = fill_val if (row+col+iteration)%2==0 else 0
#     """
#     fill_val = _cb_hw_tile_id(r, c)
#     return [
#         [fill_val if (row + col + iteration) % 2 == 0 else 0
#          for col in range(_CB_SIZE)]
#         for row in range(_CB_SIZE)
#     ]


# # Display char map: fill_val → single character for readable grid printout
# _CB_CHARS = "123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
# _CB_FILL_TO_CHAR = {}
# _cb_idx = 0
# for _cb_r in range(8):
#     for _cb_c in range(8):
#         _cb_fv = (((_cb_r << 3) | _cb_c) + 1) & 0xFF
#         if _cb_fv not in _CB_FILL_TO_CHAR:
#             _CB_FILL_TO_CHAR[_cb_fv] = _CB_CHARS[_cb_idx % len(_CB_CHARS)]
#             _cb_idx += 1


# def _cb_fmt(v):
#     return '.' if v == 0 else _CB_FILL_TO_CHAR.get(v, '?')


# async def _cb_read_word(dut, tid, addr):
#     """Read 4-byte little-endian word from tile tid at addr via DFT."""
#     b0 = await dft_read(dut, tid, addr)
#     b1 = await dft_read(dut, tid, addr + 1)
#     b2 = await dft_read(dut, tid, addr + 2)
#     b3 = await dft_read(dut, tid, addr + 3)
#     return b0 | (b1 << 8) | (b2 << 16) | (b3 << 24)


# async def _cb_read_grid(dut, row, col):
#     """Read the 10×10 grid from tile (row,col) SRAM via DFT SPI."""
#     tid = tile4(row, col)
#     return [
#         [await dft_read(dut, tid, _SRAM_GRID_BASE + y * _CB_SIZE + x)
#          for x in range(_CB_SIZE)]
#         for y in range(_CB_SIZE)
#     ]


# def _cb_print_comparison(row, col, iteration, actual):
#     """Print expected vs actual side-by-side. Returns mismatch count."""
#     exp      = _cb_expected_grid(row, col, iteration)
#     fill_val = _cb_hw_tile_id(row, col)

#     print(f"\n{'='*56}")
#     print(f"TILE ({row},{col})  hw_id=0x{fill_val:02x}  iteration={iteration}")
#     print(f"EXPECTED (checkerboard)       ACTUAL (DFT read)")
#     print(f"{'-'*56}")

#     mismatches = 0
#     for y in range(_CB_SIZE):
#         exp_row = "".join(_cb_fmt(exp[y][x]) for x in range(_CB_SIZE))
#         act_row = "".join(_cb_fmt(actual[y][x]) for x in range(_CB_SIZE))
#         marker  = "  <- MISMATCH" if exp_row != act_row else ""
#         if marker:
#             mismatches += sum(1 for x in range(_CB_SIZE) if exp[y][x] != actual[y][x])
#         print(f"  {exp_row}          {act_row}{marker}")

#     print(f"{'='*56}")
#     print(f"  {'PASS' if mismatches == 0 else f'FAIL — {mismatches} cells wrong'}")
#     return mismatches


# ============================================================================
# TEST 7 — Checkerboard readback: 2x2, DFT-loaded firmware
# ============================================================================

_CB_SIZE = 10
_CB_TILES = [(0, 0), (0, 1), (1, 0), (1, 1)]

_SRAM_GRID_BASE = 0x0500
_DEBUG_ITER_COUNT = 0x0700 + 28
_DEBUG_MY_ID = 0x0700 + 44

_DEBUG_MAGIC0 = 0x0730
_DEBUG_MAGIC1 = 0x0734
_DEBUG_PRE_CHECKERBOARD = 0x0740
_DEBUG_POST_CHECKERBOARD = 0x0744
_DEBUG_PHASE_MARKER = 0x0748

# Firmware should fit below SERV RF area.
_FW_SAFE_LIMIT = 0x770

_CB_BOOT_CYCLES = 80_000
_CB_RUN_CYCLES = 1_000_000

_FIRMWARE_BIN = Path(
    os.getenv(
        "FIRMWARE_BIN",
        Path(__file__).resolve().parent / "../src/firmware/firmware.bin"
    )
)


def _cb_hw_tile_id(row, col):
    return ((row << 3) | col) + 1


def _cb_expected_grid(row, col, iteration):
    fill_val = _cb_hw_tile_id(row, col)
    phase = iteration & 1

    return [
        [
            fill_val if ((y + x + phase) & 1) == 0 else 0
            for x in range(_CB_SIZE)
        ]
        for y in range(_CB_SIZE)
    ]


def _cb_fmt(value):
    if value == 0:
        return "."
    if 1 <= value <= 9:
        return str(value)
    return chr(ord("A") + ((value - 10) % 26))


async def _cb_read_word(dut, tile_id, addr):
    value = 0
    for i in range(4):
        byte = await dft_read(dut, tile_id, addr + i)
        value |= byte << (8 * i)
    return value


async def _cb_read_grid(dut, row, col):
    tile_id = tile4(row, col)
    grid = []

    for y in range(_CB_SIZE):
        row_values = []
        for x in range(_CB_SIZE):
            addr = _SRAM_GRID_BASE + y * _CB_SIZE + x
            row_values.append(await dft_read(dut, tile_id, addr))
        grid.append(row_values)

    return grid


async def _cb_load_firmware_to_tile(dut, row, col, firmware_bytes):
    """
    Load firmware directly into tile SRAM through DFT.

    This bypasses the SPI flash boot path, so we can test:
      firmware -> SERV CPU -> SRAM checkerboard writes
    without depending on the boot_controller/flash model.
    """
    tid = tile4(row, col)
    label = f"tile({row},{col})"

    dut._log.info(f"[FW_LOAD] {label}: writing {len(firmware_bytes)} bytes...")

    for addr, byte in enumerate(firmware_bytes):
        await dft_write(dut, tid, addr, byte)

    dut._log.info(f"[FW_LOAD] {label}: verifying firmware bytes...")

    for addr, expected in enumerate(firmware_bytes):
        got = await dft_read(dut, tid, addr)
        assert got == expected, (
            f"{label} firmware verify failed at 0x{addr:03X}: "
            f"expected 0x{expected:02X}, got 0x{got:02X}"
        )

    # Zero SERV RF/register-file area.
    # This is the same idea as _init_tile().
    for addr in range(0x770, 0x800):
        await dft_write(dut, tid, addr, 0x00)

    # Touch bank0 so first instruction fetch at PC=0 sees the correct bank.
    await dft_read(dut, tid, 0x000)

    dut._log.info(f"[FW_LOAD] {label}: ready.")


def _cb_print_comparison(row, col, expected_iteration, actual):
    expected = _cb_expected_grid(row, col, expected_iteration)

    print("")
    print("=" * 64)
    print(
        f"TILE ({row},{col}) "
        f"tid4=0x{tile4(row, col):X} "
        f"fill=0x{_cb_hw_tile_id(row, col):02X} "
        f"expected_phase={expected_iteration & 1}"
    )
    print("EXPECTED       ACTUAL")
    print("-" * 64)

    mismatches = 0

    for y in range(_CB_SIZE):
        expected_row = "".join(_cb_fmt(expected[y][x]) for x in range(_CB_SIZE))
        actual_row = "".join(_cb_fmt(actual[y][x]) for x in range(_CB_SIZE))

        row_mismatch = False
        for x in range(_CB_SIZE):
            if expected[y][x] != actual[y][x]:
                mismatches += 1
                row_mismatch = True

        marker = "  <- MISMATCH" if row_mismatch else ""
        print(f"{expected_row}       {actual_row}{marker}")

    print(f"RESULT: {'PASS' if mismatches == 0 else f'FAIL — {mismatches} wrong cells'}")
    print("=" * 64)

    return mismatches


@cocotb.test()
async def test_checkerboard_2x2_via_dft(dut):
    await start_up(dut)

    # Freeze CPUs immediately.
    dut.input_PAD.value = _DFT_MODE | _CS_N
    await ClockCycles(dut.clk_PAD, 8)

    # Let boot_controller finish whatever it is doing first, so it cannot
    # overwrite the firmware we are about to DFT-load.
    dut._log.info(f"Waiting {_CB_BOOT_CYCLES} cycles for boot_controller to finish...")
    await ClockCycles(dut.clk_PAD, _CB_BOOT_CYCLES)

    firmware_path = Path(_FIRMWARE_BIN)
    assert firmware_path.exists(), f"Firmware binary not found: {firmware_path}"

    raw_firmware = firmware_path.read_bytes()

    assert len(raw_firmware) > 0, "Firmware binary is empty"

    # firmware.bin is padded to 2048 bytes, but 0x770..0x7FF is SERV RF/register space.
    # Load only 0x000..0x76F into SRAM, then explicitly zero RF below.
    firmware_bytes = list(raw_firmware[:_FW_SAFE_LIMIT])

    # Safety check: if there is actual nonzero code/data past 0x76F, the firmware is too large.
    overflow = raw_firmware[_FW_SAFE_LIMIT:]
    if any(byte != 0 for byte in overflow):
        raise AssertionError(
            f"Firmware has nonzero bytes past 0x{_FW_SAFE_LIMIT - 1:03X}. "
            f"That overlaps SERV RF/register space. Rebuild smaller firmware or adjust linker."
        )

    dut._log.info(
        f"Firmware raw size = {len(raw_firmware)} bytes; "
        f"DFT-loading first {len(firmware_bytes)} bytes"
    )

    dut._log.info(f"Loading firmware from {firmware_path}")
    dut._log.info(f"Firmware size = {len(firmware_bytes)} bytes")

    for row, col in _CB_TILES:
        await _cb_load_firmware_to_tile(dut, row, col, firmware_bytes)

    # Release DFT mode. CPUs start executing from PC=0.
    dut.input_PAD.value = _CS_N
    await ClockCycles(dut.clk_PAD, _CB_RUN_CYCLES)

    # Freeze CPUs and inspect SRAM.
    dut.input_PAD.value = _DFT_MODE | _CS_N
    await ClockCycles(dut.clk_PAD, 8)

    print("")
    print("#" * 64)
    print("CHECKERBOARD 2x2 READBACK — DFT-LOADED FIRMWARE")
    print("#" * 64)

    total_mismatches = 0
    tile_results = {}

    # First: read debug markers. This tells us whether main() ran.
    for row, col in _CB_TILES:
        tid = tile4(row, col)

        magic0 = await _cb_read_word(dut, tid, _DEBUG_MAGIC0)
        magic1 = await _cb_read_word(dut, tid, _DEBUG_MAGIC1)
        my_id = await _cb_read_word(dut, tid, _DEBUG_MY_ID)
        phase = await _cb_read_word(dut, tid, _DEBUG_PHASE_MARKER)
        pre = await _cb_read_word(dut, tid, _DEBUG_PRE_CHECKERBOARD)
        post = await _cb_read_word(dut, tid, _DEBUG_POST_CHECKERBOARD)
        iter_count = await _cb_read_word(dut, tid, _DEBUG_ITER_COUNT)

        print(
            f"DEBUG tile({row},{col}) tid=0x{tid:X}: "
            f"magic0=0x{magic0:08X} magic1=0x{magic1:08X} "
            f"my_id=0x{my_id:08X} phase=0x{phase:08X} "
            f"pre=0x{pre:08X} post=0x{post:08X} iter={iter_count}"
        )

        if magic0 != 0xDEADBEEF or magic1 != 0xCAFEBABE:
            raise AssertionError(
                f"Firmware did not reach main() on tile({row},{col}). "
                f"magic0=0x{magic0:08X}, magic1=0x{magic1:08X}"
            )

    # Use tile(0,0)'s iter_count as expected phase.
    iter00 = await _cb_read_word(dut, tile4(0, 0), _DEBUG_ITER_COUNT)
    expected_iteration = iter00 & 1

    print(f"tile(0,0) raw iter_count = {iter00}")
    print(f"using expected checkerboard phase = {expected_iteration}")

    for row, col in _CB_TILES:
        tid = tile4(row, col)
        tile_iter = await _cb_read_word(dut, tid, _DEBUG_ITER_COUNT)
        actual = await _cb_read_grid(dut, row, col)

        mismatches = _cb_print_comparison(row, col, expected_iteration, actual)

        tile_results[(row, col)] = {
            "tile_id": tid,
            "iter": tile_iter,
            "mismatches": mismatches,
        }

        total_mismatches += mismatches

    print("")
    print("#" * 64)
    print("SUMMARY")
    print("#" * 64)

    for row, col in _CB_TILES:
        result = tile_results[(row, col)]
        status = "PASS" if result["mismatches"] == 0 else f"FAIL ({result['mismatches']} cells)"
        print(
            f"tile({row},{col}) "
            f"tid=0x{result['tile_id']:X} "
            f"iter={result['iter']} "
            f"{status}"
        )

    print("#" * 64)
    print("")

    dut.input_PAD.value = _CS_N

    assert total_mismatches == 0, (
        f"test_checkerboard_2x2_via_dft FAILED: "
        f"{total_mismatches} mismatched cells across 2x2 tiles"
    )

    dut._log.info("test_checkerboard_2x2_via_dft PASSED")

# ============================================================================
# Runner
# ============================================================================

def prepare_gl_tile_sources(nl_path: Path, build_dir: Path):
    """
    Synthesized mesh_tile.nl.v has no parameters, but mesh_3x3.v passes
    #(.TILE_ID(...)) on every instantiation.  Fix this without touching the
    synthesis output:
      1. Copy the netlist and rename  module mesh_tile → mesh_tile_gl
      2. Write a thin wrapper  mesh_tile  that accepts TILE_ID (ignored) and
         wires all ports straight through to mesh_tile_gl.
    Returns [renamed_netlist_path, wrapper_path].
    """
    build_dir.mkdir(parents=True, exist_ok=True)

    nl_text = nl_path.read_text()
    nl_renamed = re.sub(r'\bmodule\s+mesh_tile\b', 'module mesh_tile_gl', nl_text)
    nl_renamed = nl_renamed.replace(
        '.B(\\sram2048.bank_sel_cap ),\n    .A(\\sram2048.bank_sel_q ),\n    .Y(_0211_))',
        '.B(\\sram2048.A[10] ),\n    .A(\\sram2048.bank_sel_q ),\n    .Y(_0211_))'
    )
    renamed_path = build_dir / "mesh_tile_gl.nl.v"
    renamed_path.write_text(nl_renamed)
    if r'\sram2048.bank_sel_cap ),\n    .A(\sram2048.bank_sel_q ),\n    .Y(_0211_))' in nl_renamed:
        raise RuntimeError(
            "bank_sel_q netlist patch did not apply — "
            "netlist layout changed, update the patch in prepare_gl_tile_sources()"
        )

    wrapper = """\
// Auto-generated wrapper: adds TILE_ID parameter dropped by synthesis.
// TILE_ID is accepted but unused — post-synthesis the value is baked in.
module mesh_tile #(parameter [3:0] TILE_ID = 0) (
    input  wire        boot_mode,
    input  wire        boot_wen,
    input  wire        clk,
    input  wire        dft_ce,
    input  wire        dft_mode,
    input  wire        dft_we,
    input  wire        rst,
    input  wire [10:0] boot_addr,
    input  wire  [7:0] boot_data,
    input  wire [10:0] dft_addr,
    output wire  [7:0] dft_rdata,
    input  wire  [7:0] dft_wdata,
    input  wire [33:0] east_in,
    output wire [33:0] east_out,
    input  wire [33:0] ne_in,
    output wire [33:0] ne_out,
    input  wire [33:0] north_in,
    output wire [33:0] north_out,
    input  wire [33:0] nw_in,
    output wire [33:0] nw_out,
    input  wire [33:0] se_in,
    output wire [33:0] se_out,
    input  wire [33:0] south_in,
    output wire [33:0] south_out,
    input  wire [33:0] sw_in,
    output wire [33:0] sw_out,
    input  wire [33:0] west_in,
    output wire [33:0] west_out
);
    mesh_tile_gl u_gl (
        .boot_mode(boot_mode), .boot_wen(boot_wen),
        .clk(clk), .dft_ce(dft_ce), .dft_mode(dft_mode), .dft_we(dft_we),
        .rst(rst), .boot_addr(boot_addr), .boot_data(boot_data),
        .dft_addr(dft_addr), .dft_rdata(dft_rdata), .dft_wdata(dft_wdata),
        .east_in(east_in),   .east_out(east_out),
        .ne_in(ne_in),       .ne_out(ne_out),
        .north_in(north_in), .north_out(north_out),
        .nw_in(nw_in),       .nw_out(nw_out),
        .se_in(se_in),       .se_out(se_out),
        .south_in(south_in), .south_out(south_out),
        .sw_in(sw_in),       .sw_out(sw_out),
        .west_in(west_in),   .west_out(west_out)
    );
endmodule
"""
    wrapper_path = build_dir / "mesh_tile_gl_wrapper.v"
    wrapper_path.write_text(wrapper)

    return [renamed_path, wrapper_path]


def chip_top_runner():

    proj_path = Path(__file__).resolve().parent

    sources = []
    defines = {f"SLOT_{slot.upper()}": True}
    includes = [proj_path / "../src/"]

    if gl:
        sources.append(Path(pdk_root) / pdk / "libs.ref" / scl / "verilog" / f"{scl}.v")
        primitives = Path(pdk_root) / pdk / "libs.ref" / scl / "verilog" / "primitives.v"
        if primitives.exists():
            sources.append(primitives)
        sources.append(proj_path / f"../final/pnl/{hdl_toplevel}.pnl.v")
        defines = {"FUNCTIONAL": True, "USE_POWER_PINS": True}
    elif gl_tile:
        sources.append(Path(pdk_root) / pdk / "libs.ref" / scl / "verilog" / f"{scl}.v")
        primitives = Path(pdk_root) / pdk / "libs.ref" / scl / "verilog" / "primitives.v"
        if primitives.exists():
            sources.append(primitives)
        sources.append(proj_path / "gf180mcu_as_sc_mcu7t3v3_stubs.v")
        sources.append(proj_path / "../src/chip_top.sv")
        sources.append(proj_path / "../src/chip_core.sv")
        defines["FUNCTIONAL"] = True
    else:
        sources.append(proj_path / "../src/chip_top.sv")
        sources.append(proj_path / "../src/chip_core.sv")

    sources += [
        Path(pdk_root) / pdk / "libs.ref/gf180mcu_fd_io/verilog/gf180mcu_fd_io.v",
        Path(pdk_root) / pdk / "libs.ref/gf180mcu_fd_io/verilog/gf180mcu_ws_io.v",
        proj_path / "../libs/gf180mcu_ocd_ip_sram/cells/gf180mcu_ocd_ip_sram__sram1024x8m8wm1/gf180mcu_ocd_ip_sram__sram1024x8m8wm1.v",
        proj_path / "../ip/gf180mcu_ws_ip__id/vh/gf180mcu_ws_ip__id.v",
        proj_path / "../ip/gf180mcu_ws_ip__logo/vh/gf180mcu_ws_ip__logo.v",
        proj_path / "../src/dft_ethan/spi_debug.v",
        # proj_path / "../src/dft_ethan/gf180mcu_fd_ip_sram__sram2048x8m8wm1.v",
        proj_path / "../src/mesh_psi_aan/mesh_rxc.v",
        *(
            prepare_gl_tile_sources(Path(gl_tile), proj_path / "sim_build")
            if gl_tile else
            [proj_path / "../src/mesh_psi_aan/mesh_tile.v"]
        ),
        proj_path / "../src/mesh_psi_aan/mesh_router.v",
        proj_path / "../src/mesh_psi_aan/boot_controller.v",
        *([] if gl_tile else [
            *sorted(f for f in (proj_path / "../src/subservient/rtl").glob("*.v")
                if "sram1024x8" not in f.name),
            *sorted((proj_path / "../src/serv/rtl").glob("*.v")),
            *sorted((proj_path / "../src/serv/servile").glob("*.v")),
        ]),
    ]

    build_args = []
    if sim == "verilator":
        build_args = ["--timing", "--trace", "--trace-fst", "--trace-structs"]

    runner = get_runner(sim)
    runner.build(
        sources=sources,
        hdl_toplevel=hdl_toplevel,
        defines=defines,
        always=True,
        includes=includes,
        build_args=build_args,
        waves=True,
    )

    runner.test(
        hdl_toplevel=hdl_toplevel,
        test_module="chip_top_tb",
        plusargs=["+notimingchecks"],
        waves=True,
    )


if __name__ == "__main__":
    chip_top_runner()