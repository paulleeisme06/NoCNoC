MEMORY {
    sram (rwx) : ORIGIN = 0x00000000, LENGTH = 2048
}
SECTIONS {
    .text : {
        *(.text.init)
        *(.text)
        *(.rodata*)
    } > sram
    .data : {
        *(.data*)
        *(.sdata*)
    } > sram
    .bss : {
        _bss_start = .;
        *(.bss*)
        *(.sbss*)
        _bss_end = .;
    } > sram
    _end = .;
    ASSERT(_end < 0x0500, "Error: code too large, overlaps stack region!")
}