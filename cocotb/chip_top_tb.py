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

# what is this counter test? 

@cocotb.test(skip=True)
async def test_counter(dut):
    """Run the counter test"""

    # Create a logger for this testbench
    logger = logging.getLogger("my_testbench")

    logger.info("Startup sequence...")

    # Start up
    await start_up(dut)

    logger.info("Running the test...")

    # Wait for some time...
    await ClockCycles(dut.clk_PAD, 10)

    # Start the counter by setting all inputs to 1
    dut.input_PAD.value = -1

    # Wait for a number of clock cycles
    await ClockCycles(dut.clk_PAD, 100)

    # Check the end result of the counter
    assert dut.bidir_PAD.value == 100 - 1

    logger.info("Done!")


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

    # 1. Rename the module in a temp copy + patch bank_sel_q timing bug.
    #
    # The synthesized netlist has a 2-cycle delay on bank_sel_q because the
    # RTL simulation branch uses a blocking assignment for bank_sel_cap (making
    # it 0-cycle), but the synthesis branch uses a D-FF (1-cycle), so
    # bank_sel_q ends up 2 cycles behind A[10] instead of 1.  This causes the
    # output mux to select the wrong bank on every bank0<->bank1 transition,
    # corrupting SERV instruction/data reads.
    #
    # Fix: in the netlist the mux feeding bank_sel_q's D input is:
    #   mux2(S=CEN_gated, B=bank_sel_cap, A=bank_sel_q) -> bank_sel_q FF D
    # Change B from bank_sel_cap to A[10] so bank_sel_q gets 1-cycle delay
    # directly from A[10], matching the RTL simulation behaviour.
    nl_text = nl_path.read_text()
    nl_renamed = re.sub(r'\bmodule\s+mesh_tile\b', 'module mesh_tile_gl', nl_text)
    # Patch the mux: replace the one instance where B drives bank_sel_q's D
    # (B=bank_sel_cap) with B=A[10].  The replacement is exact-string safe
    # because the mux also has A=bank_sel_q on the next line.
    nl_renamed = nl_renamed.replace(
        '.B(\\sram2048.bank_sel_cap ),\n    .A(\\sram2048.bank_sel_q ),\n    .Y(_0211_))',
        '.B(\\sram2048.A[10] ),\n    .A(\\sram2048.bank_sel_q ),\n    .Y(_0211_))'
    )
    renamed_path = build_dir / "mesh_tile_gl.nl.v"
    renamed_path.write_text(nl_renamed)
    # Verify the patch applied (detect netlist layout changes between runs)
    if r'\sram2048.bank_sel_cap ),\n    .A(\sram2048.bank_sel_q ),\n    .Y(_0211_))' in nl_renamed:
        raise RuntimeError(
            "bank_sel_q netlist patch did not apply — "
            "netlist layout changed, update the patch in prepare_gl_tile_sources()"
        )

    # 2. Build wrapper with TILE_ID param + full port passthrough
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
        # Full chip GLS: powered netlist replaces everything
        sources.append(Path(pdk_root) / pdk / "libs.ref" / scl / "verilog" / f"{scl}.v")
        primitives = Path(pdk_root) / pdk / "libs.ref" / scl / "verilog" / "primitives.v"
        if primitives.exists():
            sources.append(primitives)

        # We use the powered netlist
        sources.append(proj_path / f"../final/pnl/{hdl_toplevel}.pnl.v")

        defines = {"FUNCTIONAL": True, "USE_POWER_PINS": True}
    elif gl_tile:
        # Mixed RTL/GL: chip_top + chip_core stay RTL; mesh_tile is gate-level.
        # SCL standard-cell behavioral models (needed by the synthesized tile netlist)
        sources.append(Path(pdk_root) / pdk / "libs.ref" / scl / "verilog" / f"{scl}.v")
        primitives = Path(pdk_root) / pdk / "libs.ref" / scl / "verilog" / "primitives.v"
        if primitives.exists():
            sources.append(primitives)
        # Stubs for cells missing from the functional library (ao211, aoi211, oai211)
        sources.append(proj_path / "gf180mcu_as_sc_mcu7t3v3_stubs.v")
        # Rest of chip stays RTL
        sources.append(proj_path / "../src/chip_top.sv")
        sources.append(proj_path / "../src/chip_core.sv")
        # FUNCTIONAL suppresses timing checks inside std-cell models
        defines["FUNCTIONAL"] = True
    else:
        sources.append(proj_path / "../src/chip_top.sv")
        sources.append(proj_path / "../src/chip_core.sv")

    sources += [
        # IO pad models
        Path(pdk_root) / pdk / "libs.ref/gf180mcu_fd_io/verilog/gf180mcu_fd_io.v",
        Path(pdk_root) / pdk / "libs.ref/gf180mcu_fd_io/verilog/gf180mcu_ws_io.v",

        # SRAM macros (3.3V) — 1024x8 is what sram2048x8_gf180 wraps
        proj_path / "../libs/gf180mcu_ocd_ip_sram/cells/gf180mcu_ocd_ip_sram__sram1024x8m8wm1/gf180mcu_ocd_ip_sram__sram1024x8m8wm1.v",

        # Custom IP
        proj_path / "../ip/gf180mcu_ws_ip__id/vh/gf180mcu_ws_ip__id.v",
        proj_path / "../ip/gf180mcu_ws_ip__logo/vh/gf180mcu_ws_ip__logo.v",

        # Ethan: Added these files:
        proj_path / "../src/dft_ethan/spi_debug.v",
        proj_path / "../src/dft_ethan/gf180mcu_fd_ip_sram__sram2048x8m8wm1.v",
        proj_path / "../src/mesh_psi_aan/mesh_3x3.v",
        # mesh_tile: use synthesized netlist in GL_TILE mode, RTL otherwise
        *(
            prepare_gl_tile_sources(Path(gl_tile), proj_path / "sim_build")
            if gl_tile else
            [proj_path / "../src/mesh_psi_aan/mesh_tile.v"]
        ),
        proj_path / "../src/mesh_psi_aan/mesh_router.v",
        proj_path / "../src/mesh_psi_aan/boot_controller.v",
        # Add all .v files from subservient and serv submodules:
        # Exclude SRAM model already included from libs/ above
        # In GL_TILE mode the synthesized netlist replaces all subservient/serv RTL
        *([] if gl_tile else [
            *sorted(f for f in (proj_path / "../src/subservient/rtl").glob("*.v")
                    if "sram1024x8" not in f.name),
            *sorted((proj_path / "../src/serv/rtl").glob("*.v")),
            *sorted((proj_path / "../src/serv/servile").glob("*.v")),
        ]),
    ]

    build_args = []

    if sim == "icarus":
        # For debugging
        # build_args = ["-Winfloop", "-pfileline=1"]
        pass

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

    plusargs = ["+notimingchecks"]

    runner.test(
        hdl_toplevel=hdl_toplevel,
        test_module="chip_top_tb,",
        plusargs=plusargs,
        waves=True,
    )

# Start of host to chip testing, using the DFT SPI debug interface to read/write tile SRAMs and boot a CPU.
# Ethan: helper SPI functions
# input_PAD[0] = spi_clk, [1] = spi_mosi, [2] = spi_cs_n, [3] = dft_mode
# bidir_PAD[0] = spi_miso (output from chip)

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

@cocotb.test()
async def test_dft_write_read(dut):
    """
    Write a byte to tile (0,0) SRAM via SPI debug interface, read it back,
    verify it matches. Cores are held in reset by dft_mode so they cannot
    interfere with the SRAM.
    """
    await start_up(dut)

    # Assert dft_mode before transactions (cores fo into reset inside chip)
    dut.input_PAD.value = _DFT_MODE | _CS_N
    await ClockCycles(dut.clk_PAD, 4)

    tile_id = 0b0000    # tile (row 0, col 0)
    addr = 0x050        # random address
    wdata = 0xA5        # random data

    await dft_write(dut, tile_id, addr, wdata)
    rdata = await dft_read(dut, tile_id, addr)

    dut._log.info(f"DFT write =0x{wdata:02x} read = 0x{rdata:02x}")
    assert rdata == wdata, f"DFT read back failed: wrote 0x{wdata:02x}, got 0x{rdata:02x}"

    # release dft_mode
    dut.input_PAD.value = _CS_N
    dut._log.info("test_dft_write_read PASSED")


@cocotb.test()
async def test_dft_sweep(dut):
    """
    Thorough DFT SRAM sweep across all 9 tiles and multiple addresses/patterns.

    What this test does:
      1. Enters DFT mode (holds all 9 SERV cores in reset so they can't touch SRAM).
      2. For every tile in the 3x3 mesh it writes three different byte patterns
         to three different SRAM addresses:
           - 0xA5 (1010_0101) — alternating bits, catches stuck-at faults
           - 0x5A (0101_1010) — inverse of above, catches complementary faults
           - 0xFF (1111_1111) — all ones, catches stuck-at-0
         Address 0x000 (bottom of SRAM), 0x1FF (middle), 0x3FE (near top of
         the 1024-entry bank) are used so we hit low, mid, and high address lines.
      3. After writing all bytes it reads every location back and asserts the
         value matches what was written.
      4. Exits DFT mode.

    Tile IDs are {row[1:0], col[1:0]}, so the 3x3 grid is:
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
        (0x000, 0xA5),  # low address,  alternating bits
        (0x1FF, 0x5A),  # mid address,  inverse alternating
        (0x3FE, 0xFF),  # high address, all ones
    ]

    dut.input_PAD.value = _DFT_MODE | _CS_N
    await ClockCycles(dut.clk_PAD, 4)

    # Write phase — fill every tile
    for row, col, tile_id in ALL_TILES:
        for addr, data in TEST_VECTORS:
            await dft_write(dut, tile_id, addr, data)
        dut._log.info(f"  tile ({row},{col}): wrote {len(TEST_VECTORS)} locations")

    # Read-back phase — verify every tile
    failures = []
    for row, col, tile_id in ALL_TILES:
        for addr, expected in TEST_VECTORS:
            got = await dft_read(dut, tile_id, addr)
            if got != expected:
                failures.append(
                    f"tile({row},{col}) addr=0x{addr:03x}: wrote 0x{expected:02x} got 0x{got:02x}"
                )
            else:
                dut._log.info(f"  tile({row},{col}) addr=0x{addr:03x} ok: 0x{got:02x}")

    dut.input_PAD.value = _CS_N

    assert not failures, "DFT sweep failures:\n" + "\n".join(failures)
    dut._log.info(f"test_dft_sweep PASSED — {len(ALL_TILES) * len(TEST_VECTORS)} locations verified")


@cocotb.test()
async def test_dft_boot_hello(dut):
    """
    Boot a RISC-V 'hello world' on tile (0,0) using DFT as the bootloader.

    Background — why this works:
      The mesh has a boot_controller that normally reads a program from SPI
      flash and loads it into every tile's SRAM, then releases cpu_reset_n.
      Two key facts from mesh_tile.v make DFT boot possible:

        1.  dft_mode=1 gives DFT PRIORITY over the SRAM mux — the
            boot_controller's SRAM writes are BLOCKED while dft_mode is high,
            so our DFT-written program cannot be overwritten.

        2.  .i_rst(rst | boot_mode | dft_mode) — the SERV core is held in
            reset by EITHER signal, so dft_mode=1 keeps the CPU frozen even
            after boot_controller releases cpu_reset_n.

    SRAM layout (servile_rf_mem_if with aw=11, rf_regs=36):
      Physical 0x000-0x76F = program + data  (CPU byte addr = physical addr)
      Physical 0x770-0x7FF = SERV register file (stored at ~rf_addr, i.e.
                              bitwise-NOT of logical RF address — top of SRAM)
      CPU PC=0 fetches from physical 0x000; CPU data addr 0x200 → physical 0x200.

    Sequence:
      1. Assert dft_mode immediately — SERV cores freeze, boot_controller
         runs but its SRAM writes are blocked.
      2. Write a 24-byte RISC-V program into tile(0,0) SRAM[0x000..0x017]
         via DFT. Physical 0x000 = CPU PC=0. The program avoids reading x0
         (zero register) because SERV's regzero override never fires at
         runtime — x0 reads come from SRAM and may be garbage. LUI (U-type,
         no source register) initialises a0/a1 unconditionally:
             lui  a0, 0             # a0 = 0
             addi a0, a0, 0x200    # a0 = 0x200
             lui  a1, 0             # a1 = 0
             addi a1, a1, 0x48     # a1 = 0x48 = 'H' (ASCII Hello)
             sb   a1, 0(a0)        # store byte: phys SRAM[0x200] = 'H'
             jal  zero, 0          # spin forever
      3. Wait ~80,000 cycles for the boot_controller to complete its 2048-byte
         flash-read state machine and release cpu_reset_n. Our program is safe
         in SRAM the whole time (dft_mode blocks all boot_controller writes).
      4. Release dft_mode=0.
         Now: rst=0, boot_mode=0, dft_mode=0 → SERV starts executing; CPU
         PC=0 fetches from physical SRAM[0x000].
      5. Wait 5000 cycles — SERV is bit-serial (RF + mem share one SRAM),
         each instruction takes ~100-300 cycles with arbitration overhead.
      6. Re-assert dft_mode=1 to stop the core and reclaim SRAM.
      7. Read back SRAM[0x200] (phys) via DFT. Assert it equals 0x48 ('H').
    """
    await start_up(dut)

    # Step 1: assert dft_mode immediately — freeze cores, block boot_controller
    dut.input_PAD.value = _DFT_MODE | _CS_N
    await ClockCycles(dut.clk_PAD, 4)

    # Step 2: write the hand-assembled RISC-V program byte-by-byte via DFT.
    # Each instruction is 4 bytes, little-endian (LSB at lower address).
    #
    # Deliberately avoid reading x0 (zero register). In SERV, regzero
    # (the hardware x0=0 override) requires i_raddr >= 0xFC, but the max
    # RF address with rf_regs=36 is 0x8F — so regzero never fires and x0
    # reads from SRAM. The RF area may hold garbage before it is zeroed,
    # so any instruction that reads x0 can compute a wrong result.
    # Using LUI (U-type, no source register) sidesteps this entirely.
    #
    #   PC 0x000: 0x00000537 = lui  a0, 0          # a0 = 0 (no rs1)
    #   PC 0x004: 0x20050513 = addi a0, a0, 0x200  # a0 = 0x200
    #   PC 0x008: 0x000005B7 = lui  a1, 0          # a1 = 0 (no rs1)
    #   PC 0x00C: 0x04858593 = addi a1, a1, 0x48   # a1 = 0x48 ('H')
    #   PC 0x010: 0x00B50023 = sb   a1, 0(a0)      # SRAM[0x200] = 'H'
    #   PC 0x014: 0x0000006F = jal  zero, 0        # spin
    #
    # SRAM layout (servile_rf_mem_if, aw=11):
    #   Physical 0x000-0x76F = program + data (CPU byte addr = physical addr)
    #   Physical 0x770-0x7FF = SERV register file (bitwise-NOT mapping, top of SRAM)
    # CPU PC=0 → physical SRAM[0x000]. CPU data addr 0x200 → physical SRAM[0x200].
    PROG_BASE   = 0x000  # physical SRAM address where CPU PC=0 fetches
    HELLO_PROG = [
        0x37, 0x05, 0x00, 0x00,  # lui  a0, 0
        0x13, 0x05, 0x05, 0x20,  # addi a0, a0, 0x200
        0xB7, 0x05, 0x00, 0x00,  # lui  a1, 0
        0x93, 0x85, 0x85, 0x04,  # addi a1, a1, 0x48  ('H')
        0x23, 0x00, 0xB5, 0x00,  # sb   a1, 0(a0)
        0x6F, 0x00, 0x00, 0x00,  # jal  zero, 0
    ]
    RESULT_ADDR = 0x200  # physical SRAM addr = CPU data addr (direct mapping)
    EXPECTED    = 0x48   # 'H'

    dut._log.info("Writing RISC-V hello-world program to tile(0,0) via DFT...")
    for i, byte in enumerate(HELLO_PROG):
        await dft_write(dut, tile_id=0b0000, addr=PROG_BASE + i, data=byte)
    end_addr = PROG_BASE + len(HELLO_PROG) - 1
    dut._log.info(f"Program loaded: {len(HELLO_PROG)} bytes at SRAM[0x{PROG_BASE:03X}..0x{end_addr:03X}] (CPU PC=0x000)")

    # Verify the bytes landed correctly before releasing the CPU.
    # If this fails, the issue is in the DFT write path, not the CPU.
    dut._log.info("Verifying program bytes via DFT read-back...")
    for i, expected_byte in enumerate(HELLO_PROG):
        phys = PROG_BASE + i
        got = await dft_read(dut, tile_id=0b0000, addr=phys)
        assert got == expected_byte, (
            f"Program verify failed at SRAM[0x{phys:03X}]: "
            f"wrote 0x{expected_byte:02X}, read back 0x{got:02X}"
        )
    dut._log.info("Program verified — all 24 bytes match.")

    # Step 3: wait for boot_controller to finish its flash-read sequence.
    # Timing: IDLE(1) + CMD(24) + ADDR(72) + 2048×(READ32+WRITE2) = 69729 cycles.
    # dft_mode=1 blocks all boot_controller SRAM writes during this wait.
    dut._log.info("Waiting ~80k cycles for boot_controller to release cpu_reset_n...")
    await ClockCycles(dut.clk_PAD, 80000)
    dut._log.info("boot_controller done — cpu_reset_n should now be 1, boot_mode=0.")

    # Diagnostic: re-check program bytes after the wait.
    # If something overwrote them during boot, this catches it.
    dut._log.info("Diagnostic: re-reading program bytes after boot wait...")
    prog_ok = True
    for i, expected_byte in enumerate(HELLO_PROG):
        got = await dft_read(dut, tile_id=0b0000, addr=PROG_BASE + i)
        if got != expected_byte:
            dut._log.warning(
                f"SRAM[0x{PROG_BASE+i:03X}] CORRUPTED during boot wait: "
                f"expected 0x{expected_byte:02X}, got 0x{got:02X}"
            )
            prog_ok = False
    if prog_ok:
        dut._log.info("Program bytes intact after boot wait — boot_controller writes were blocked.")
    else:
        # Re-write the program since boot_controller corrupted it
        dut._log.warning("Re-writing program bytes (boot_controller overwrote them)...")
        for i, byte in enumerate(HELLO_PROG):
            await dft_write(dut, tile_id=0b0000, addr=PROG_BASE + i, data=byte)
        dut._log.info("Program re-written.")

    # Critical: initialize the SERV RF area (SRAM[0x770:0x7FF]) to zero.
    #
    # The GF180 SRAM wrapper has two banks:
    #   bank0: SRAM[0x000-0x3FF] (A[10]=0)
    #   bank1: SRAM[0x400-0x7FF] (A[10]=1)
    # Each bank tracks a 'cen_fell' flag that must latch a CEN HIGH→LOW
    # transition before any read or write is accepted by the model.
    #
    # Our DFT writes (0x000-0x00F) only touched bank0.  The boot_controller
    # was blocked by dft_mode=1 the entire time and never wrote to bank1.
    # Result: bank1 cen_fell=0, so every bank1 read returns X (garbage).
    #
    # SERV's register file is mapped by servile_rf_mem_if to SRAM[0x770:0x7FF]
    # (36 registers × 4 bytes = 144 bytes, all in bank1).  With cen_fell=0
    # those reads return X, corrupting x0 and every other register.
    # The addi/sb sequence then computes a garbage address and fails.
    #
    # Writing 0x00 to every RF address arms bank1's cen_fell and zeroes all
    # registers so SERV starts execution from a clean state.
    RF_AREA_START = 0x770  # first byte of SERV RF in physical SRAM
    RF_AREA_END   = 0x7FF  # last  byte of SERV RF in physical SRAM
    dut._log.info(
        f"Initializing RF area SRAM[0x{RF_AREA_START:03X}..0x{RF_AREA_END:03X}] "
        f"to zero (arms bank1 cen_fell, zeroes all SERV registers)..."
    )
    for addr in range(RF_AREA_START, RF_AREA_END + 1):
        await dft_write(dut, tile_id=0b0000, addr=addr, data=0x00)
    dut._log.info("RF area initialized — bank1 operational, all registers = 0.")

    # Critical: reset bank_sel_q to 0 before releasing SERV.
    #
    # sram2048x8_gf180 has a registered output-mux control:
    #   reg bank_sel_q;
    #   always @(posedge CLK) if (!CEN) bank_sel_q <= bank_sel;
    #   assign Q = bank_sel_q ? q_bank1 : q_bank0;
    #
    # The last DFT write was to 0x7FF (bank1), leaving bank_sel_q=1.
    # SERV's first fetch is from PC=0x000 (bank0). With bank_sel_q=1 the mux
    # returns q_bank1 (garbage) instead of the correct instruction byte —
    # corrupting the LUI instruction and producing a wrong register value.
    # One bank0 read flips bank_sel_q=0 before the CPU starts.
    await dft_read(dut, tile_id=0b0000, addr=0x000)
    dut._log.info("bank_sel_q reset to 0 (bank0 touch done).")

    # Step 4: release dft_mode — SERV sees rst=0, boot_mode=0, dft_mode=0 → runs!
    dut.input_PAD.value = _CS_N
    dut._log.info("Released dft_mode — SERV core now executing (PC=0 → phys SRAM[0x000])...")

    # Step 5: give SERV time to execute the 5-instruction program (+ spin).
    # SERV is bit-serial; RF and instruction memory share one SRAM via
    # servile_rf_mem_if, so each instruction takes ~100-300 cycles with
    # arbitration. 10000 cycles gives >5× margin for 5 instructions.
    await ClockCycles(dut.clk_PAD, 10000)

    # Step 6: re-assert dft_mode to stop the core and take back SRAM
    dut.input_PAD.value = _DFT_MODE | _CS_N
    await ClockCycles(dut.clk_PAD, 4)

    # Diagnostic: read a0 (x10) and a1 (x11) RF locations to see what SERV computed.
    # RF address mapping (servile_rf_mem_if, aw=11, rf_depth=8, rf_regs=36):
    #   rf_addr = ~{3'b0, i_raddr[7:0]}  where i_raddr = reg_num*4 + byte_sel
    # a0 (x10=10): i_raddr 0x28-0x2B → SRAM 0x7D7(byte0), 0x7D6, 0x7D5, 0x7D4(byte3)
    # a1 (x11=11): i_raddr 0x2C-0x2F → SRAM 0x7D3(byte0), 0x7D2, 0x7D1, 0x7D0(byte3)
    a0_b = [await dft_read(dut, 0b0000, a) for a in (0x7D7, 0x7D6, 0x7D5, 0x7D4)]
    a0_val = a0_b[0] | (a0_b[1] << 8) | (a0_b[2] << 16) | (a0_b[3] << 24)
    dut._log.info(f"RF a0 = 0x{a0_val:08x}  bytes(lsb→msb)={[f'0x{b:02x}' for b in a0_b]}")
    a1_b = [await dft_read(dut, 0b0000, a) for a in (0x7D3, 0x7D2, 0x7D1, 0x7D0)]
    a1_val = a1_b[0] | (a1_b[1] << 8) | (a1_b[2] << 16) | (a1_b[3] << 24)
    dut._log.info(f"RF a1 = 0x{a1_val:08x}  bytes(lsb→msb)={[f'0x{b:02x}' for b in a1_b]}")
    prog0 = await dft_read(dut, 0b0000, 0x000)
    dut._log.info(f"SRAM[0x000] = 0x{prog0:02x} (expect 0x37 = first byte of lui a0,0)")

    # Step 7: read back the result
    result = await dft_read(dut, tile_id=0b0000, addr=RESULT_ADDR)
    dut._log.info(
        f"SRAM[0x{RESULT_ADDR:03X}] = 0x{result:02X}  "
        f"(expected 0x{EXPECTED:02X} = '{chr(EXPECTED)}')"
    )

    dut.input_PAD.value = _CS_N
    assert result == EXPECTED, \
        f"DFT boot failed: expected 0x{EXPECTED:02X} ('{chr(EXPECTED)}'), got 0x{result:02X}"
    dut._log.info("test_dft_boot_hello PASSED — SERV booted via DFT and wrote 'H' to SRAM!")





    


if __name__ == "__main__":
    chip_top_runner()
