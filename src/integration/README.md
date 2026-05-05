# Integration Testbench — Psi & Andrea (Mesh + Host-to-Chip)

## Setup

```bash
source .venv/bin/activate
```

---

## Running the Tests

### Full Pipeline Test
```bash
make sim-top
```
Runs `cocotb/test_top.py`. Tests the complete flow end-to-end:
1. **Boot** — SPI flash model loads firmware into all 9 tile SRAMs via `boot_controller`
2. **Seed** — Host SPI bit-bangs a blinker pattern into the mesh tiles
3. **Run** — Releases reset, waits for `0xCCCCCCCC` on `monitor_22_se`
4. **Readback** — Reads all 9 tile SRAMs, assembles a 30x30 Game of Life frame, checks pixel correctness, and writes `gol_gen1.pbm`

### Flash + Mesh Boot Test
```bash
make sim-flash
```
Runs `cocotb/test_flash_mesh.py`. Tests that `boot_controller` correctly streams firmware from SPI flash into all tile SRAMs. Verifies the SRAM contents byte-by-byte.

---

## Verilog Files Required

| File | Description |
|------|-------------|
| `top.v` | Top-level integration module |
| `mesh_3x3.v` | 3x3 mesh network instantiation |
| `mesh_tile.v` | Individual mesh tile (Game of Life cell + SRAM + router) |
| `mesh_router.v` | NoC router for inter-tile communication |
| `host_to_chip.v` | Host SPI to chip interface |
| `rd_crossbar.v` | Read crossbar for mesh SRAM access |
| `boot_loader.v` | Boot controller — loads firmware from flash into SRAMs |
| `flash/spi_arbiter.v` | SPI bus arbiter |
| `flash/housekeeping.v` | Housekeeping SPI interface |
| `flash/flash_clk.v` | Flash clock generator |
| `flash/shiftregister.v` | SPI shift register |
| `flash/hk_boot_adapter.v` | Adapter between housekeeping and boot controller |
| `subservient/ethan/*.v` | RISC-V subservient core + GF180 SRAM models |
| `serv/rtl/*.v` | SERV CPU pipeline |
| `serv/servile/*.v` | SERV support modules |

Also required: `firmware.bin` and `firmware/firmware.elf` (compiled RISC-V firmware).

---

## Cocotb Test Files

| File | Runs with |
|------|-----------|
| `cocotb/test_top.py` | `make sim-top` |
| `cocotb/test_flash_mesh.py` | `make sim-flash` |
| `cocotb/test_mesh.py` | standalone mesh unit tests |
| `cocotb/chip_top_tb.py` | chip-level template testbench |
