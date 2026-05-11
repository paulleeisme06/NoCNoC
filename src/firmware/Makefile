########################################
# --- TOOLCHAIN ---
########################################
CC      = riscv64-unknown-elf-gcc
OBJCOPY = riscv64-unknown-elf-objcopy

########################################
# --- PATHS ---
########################################
FW_DIR      := $(CURDIR)
SRC_DIR     := $(abspath $(FW_DIR)/..)
ROOT_DIR    := $(abspath $(FW_DIR)/../..)

RTL_DIR     := $(SRC_DIR)/mesh_psi_aan
DFT_DIR     := $(SRC_DIR)/dft_ethan
COCOTB_DIR  := $(ROOT_DIR)/cocotb

########################################
# --- FIRMWARE FILES ---
########################################
TARGET_ELF := $(FW_DIR)/firmware.elf
TARGET_BIN := $(SRC_DIR)/firmware.bin
TARGET_HEX := $(FW_DIR)/firmware.hex

FW_SRC := $(FW_DIR)/main.c
LINKER := $(FW_DIR)/link.ld

########################################
# --- FIRMWARE FLAGS ---
########################################
CFLAGS = -march=rv32i -mabi=ilp32 -Os -ffreestanding -nostdlib \
         -fno-builtin -fno-shrink-wrap -fno-tree-bit-ccp -mstrict-align

LDFLAGS = -T $(LINKER) -nostdlib

########################################
# --- COCOTB / VERILATOR SETTINGS ---
########################################
TOPLEVEL_LANG = verilog
TOPLEVEL      = mesh_rxc
MODULE        = test_mesh
SIM           = verilator

export TOPLEVEL_LANG
export TOPLEVEL
export MODULE
export SIM

# Let cocotb find test_mesh.py in ~/NoCNoC/cocotb
export PYTHONPATH := $(COCOTB_DIR):$(PYTHONPATH)

VERILOG_EXTRA_DIRS = \
	$(SRC_DIR)/subservient/rtl \
	$(SRC_DIR)/serv/rtl \
	$(SRC_DIR)/serv/servile

VERILOG_EXTRA = $(wildcard $(addsuffix /*.v,$(VERILOG_EXTRA_DIRS)))

VERILOG_SOURCES = \
	$(RTL_DIR)/mesh_rxc.v \
	$(RTL_DIR)/mesh_tile.v \
	$(RTL_DIR)/mesh_router.v \
	$(RTL_DIR)/boot_controller.v \
	$(DFT_DIR)/gf180mcu_fd_ip_sram__sram2048x8m8wm1.v \
	$(VERILOG_EXTRA)

export VERILOG_SOURCES

COMPILE_ARGS = --timing \
	-Wno-PINMISSING \
	-Wno-SPECIFYIGN \
	-Wno-MODDUP \
	-Wno-MINTYPMAXDLY \
	--trace \
	--trace-depth 6

SIM_ARGS = --trace --trace-file sim_build/dump.fst

export COMPILE_ARGS
export SIM_ARGS

########################################
# --- DEFAULT TARGET ---
########################################
all: sim

########################################
# --- FIRMWARE BUILD ---
########################################
firmware: $(TARGET_BIN) size_check

$(TARGET_ELF): $(FW_SRC) $(LINKER)
	$(CC) $(CFLAGS) -o $@ $(FW_SRC) $(LDFLAGS)

$(TARGET_BIN): $(TARGET_ELF)
	$(OBJCOPY) -O binary $< $@
	truncate -s 2048 $@

# Optional hex rule; not used by sim unless you call make hex
hex: $(TARGET_HEX)

$(TARGET_HEX): $(TARGET_BIN)
	@if [ -f "$(FW_DIR)/bin2hex.py" ]; then \
		cd $(FW_DIR) && python3 bin2hex.py $(TARGET_BIN) firmware.hex; \
	else \
		echo "Skipping firmware.hex: bin2hex.py not found in $(FW_DIR)"; \
	fi

size_check: $(TARGET_ELF)
	@echo "--- FIRMWARE SIZE REPORT ---"
	@riscv64-unknown-elf-size $(TARGET_ELF)

########################################
# --- SIMULATION ---
########################################
sim: $(TARGET_BIN) size_check
	cd $(SRC_DIR) && $(MAKE) -f $(shell cocotb-config --makefiles)/Makefile.sim

########################################
# --- CLEAN ---
########################################
clean:
	rm -f $(TARGET_ELF)
	rm -f $(TARGET_HEX)
	rm -f $(SRC_DIR)/firmware.bin
	rm -f $(FW_DIR)/firmware.bin
	rm -rf $(SRC_DIR)/sim_build
	rm -f $(SRC_DIR)/results.xml
	rm -f $(SRC_DIR)/dump.fst
	rm -f $(FW_DIR)/results.xml
	rm -f $(FW_DIR)/dump.fst