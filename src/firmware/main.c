#include <stdint.h>

// ============================================================================
// Snake-token connectivity test
//
// Purpose: prove that every inter-tile link carries data correctly.
//
// Pattern (3×3 mesh, numbers = visit order):
//
//   row 0  →  (0,0) 00 → (0,1) 01 → (0,2) 02
//                                      ↓ south
//   row 1  ←  (1,0) 05 ← (1,1) 04 ← (1,2) 03
//              ↓ south
//   row 2  →  (2,0) 06 → (2,1) 07 → (2,2) 08
//
// Each tile:
//   1. Waits to receive the token (except tile(0,0) which starts the chain).
//   2. Writes the received token payload into DEBUG_TOKEN_RECV.
//   3. Writes the direction it received the token from into DEBUG_DIR_RECV.
//   4. Forwards the token to the next tile in the snake order.
//   5. Writes DEBUG_DONE = 0xDEAD when finished.
//
// Tile(0,0) generates the initial token (payload = TOKEN_VALID_BIT | its own TILE_ID).
// Every forwarded token has payload = TOKEN_VALID_BIT | sender_TILE_ID so the
// testbench can verify that the right neighbour sent the right token.
// ============================================================================

// ============================================================================
// Mesh configuration — must match mesh_3x3.v
// ============================================================================
#define MESH_R  3
#define MESH_C  3

// ============================================================================
// NoC memory-mapped registers
// ============================================================================
#define NOC_INJECT_BASE  0x80000000u
#define NOC_RECV_BASE    0x80000004u
#define NOC_ID_BASE      0x80000008u

// TILE_ID encoding: row in [3:2], col in [1:0]  — matches mesh_router.v
//   parameter [3:0] MY_ID,  wire [1:0] my_row = MY_ID[3:2],  my_col = MY_ID[1:0]
#define TILE_ID(r, c)   ((((uint32_t)(r)) << 2) | ((uint32_t)(c)))

// Flit layout (matches mesh_router.v inject path):
//   CPU writes a 32-bit word to 0x80000000.
//   The router builds the 34-bit flit as:
//     [33]    = 1'b1 (valid)
//     [32:29] = word[31:28]  → dest TILE_ID (4 bits)
//     [28:0]  = word[28:0]   → payload
//
//   Inject word layout:
//     [31:28]  dest TILE_ID (4 bits) — consumed by router, NOT in payload
//     [28:0]   payload delivered to receiver FIFO ({3'b0, flit[28:0]})
//
// TOKEN_VALID_BIT must live inside payload[28:0] and must NOT overlap
// bits [31:28] (the dest field).  Bit 16 is safe.
#define FLIT_DEST_SHIFT  28u
#define TOKEN_VALID_BIT  (1u << 16)   // sentinel: bit 16, safely inside payload

// ============================================================================
// Debug memory map  (byte addresses inside the 2 KB SRAM)
// ============================================================================
#define DEBUG_BASE          0x0700u
#define DEBUG_MY_ID         (DEBUG_BASE +  0)   // uint32: my TILE_ID
#define DEBUG_TOKEN_RECV    (DEBUG_BASE +  4)   // uint32: raw payload received
#define DEBUG_DIR_RECV      (DEBUG_BASE +  8)   // uint32: direction token came from
#define DEBUG_TOKEN_SENT    (DEBUG_BASE + 12)   // uint32: payload we forwarded
#define DEBUG_DEST_SENT     (DEBUG_BASE + 16)   // uint32: dest TILE_ID we sent to
#define DEBUG_DONE          (DEBUG_BASE + 20)   // uint32: 0xDEAD when finished

// Direction codes stored in DEBUG_DIR_RECV
#define DIR_SELF   0u   // tile(0,0) — no receive, self-start
#define DIR_WEST   1u   // token arrived from the west  (E link of left neighbour)
#define DIR_EAST   2u   // token arrived from the east  (W link of right neighbour)
#define DIR_NORTH  3u   // token arrived from the north (S link of upper neighbour)

// ============================================================================
// Typed SRAM pointers
// ============================================================================
#define dbg_my_id       ((volatile uint32_t *)DEBUG_MY_ID)
#define dbg_token_recv  ((volatile uint32_t *)DEBUG_TOKEN_RECV)
#define dbg_dir_recv    ((volatile uint32_t *)DEBUG_DIR_RECV)
#define dbg_token_sent  ((volatile uint32_t *)DEBUG_TOKEN_SENT)
#define dbg_dest_sent   ((volatile uint32_t *)DEBUG_DEST_SENT)
#define dbg_done        ((volatile uint32_t *)DEBUG_DONE)

// ============================================================================
// NoC helpers
// ============================================================================
static inline void noc_send(uint32_t dest_id, uint32_t payload)
{
    // dest_id is 4 bits, shifted to [31:28].
    // payload must fit in [27:0] — TOKEN_VALID_BIT (bit 16) is safely below bit 28.
    *(volatile uint32_t *)NOC_INJECT_BASE =
        (dest_id << FLIT_DEST_SHIFT) | TOKEN_VALID_BIT | (payload & 0x0FFFFFFFu);
}

// Block-poll until we receive a flit with TOKEN_VALID_BIT set.
// Returns the full 29-bit payload word (bits [28:0]).
static inline uint32_t noc_recv_token(void)
{
    uint32_t p;
    do {
        p = *(volatile uint32_t *)NOC_RECV_BASE;
    } while (!(p & TOKEN_VALID_BIT));
    return p;
}

static inline uint32_t noc_read_my_id(void)
{
    return *(volatile uint32_t *)NOC_ID_BASE & 0xFu;  // 4-bit TILE_ID
}

// ============================================================================
// Snake ordering helpers
//
// The snake visits tiles in boustrophedon row order:
//   even rows (0,2,...): left → right   (increasing col)
//   odd  rows (1,3,...): right → left   (decreasing col)
//
// snake_next(r, c, &nr, &nc) fills (nr, nc) with the next tile in the snake.
// Returns 0 if (r,c) is the last tile (no next tile).
// ============================================================================
static int snake_next(int r, int c, int *nr, int *nc)
{
    int even_row = ((r & 1) == 0);

    if (even_row) {
        /* moving right */
        if (c < MESH_C - 1) {
            *nr = r;
            *nc = c + 1;
            return 1;
        }
    } else {
        /* moving left */
        if (c > 0) {
            *nr = r;
            *nc = c - 1;
            return 1;
        }
    }

    /* At the row end — step south if there is a next row */
    if (r < MESH_R - 1) {
        *nr = r + 1;
        *nc = c;
        return 1;
    }

    /* Last tile in the mesh — no next */
    return 0;
}

// ============================================================================
// _start — minimal RISC-V reset entry
// ============================================================================
__attribute__((section(".text.init"), naked))
void _start(void)
{
    __asm__ volatile (
        "li   sp, 0x7fc\n"
        /* zero the entire debug region 0x0700..0x077f */
        "li   t0, 0x0700\n"
        "li   t1, 0x0780\n"
        "1: bge  t0, t1, 2f\n"
        "   sb   zero, 0(t0)\n"
        "   addi t0, t0, 1\n"
        "   j    1b\n"
        "2: call main\n"
        "3: j    3b\n"
    );
}

// ============================================================================
// main
// ============================================================================
int main(void)
{
    uint32_t my_id  = noc_read_my_id();
    // 4-bit ID: row = MY_ID[3:2], col = MY_ID[1:0]
    int      my_row = (int)((my_id >> 2) & 0x3u);
    int      my_col = (int)(my_id & 0x3u);

    *dbg_my_id = my_id;

    // -------------------------------------------------------------------------
    // Determine what position in the snake this tile occupies.
    //
    // A tile is the *snake head* (first in chain) if and only if it is
    // tile(0,0).  Every other tile must first receive the token from its
    // snake predecessor before forwarding it.
    // -------------------------------------------------------------------------
    int is_head = (my_row == 0 && my_col == 0);

    uint32_t token_payload;

    if (is_head) {
        // Tile(0,0): generate the initial token.
        token_payload = TOKEN_VALID_BIT | (my_id & 0xFFu);
        *dbg_dir_recv   = DIR_SELF;
        *dbg_token_recv = token_payload;
    } else {
        // Every other tile: block-wait for the token.
        token_payload = noc_recv_token();
        *dbg_token_recv = token_payload;

        // Determine which direction the token came from based on snake order:
        //   even row, c>0       → predecessor is (r, c-1) → token came from WEST
        //   odd  row, c<MC-1    → predecessor is (r, c+1) → token came from EAST
        //   row-start column    → predecessor is (r-1, c) → token came from NORTH
        int even_row = ((my_row & 1) == 0);
        uint32_t dir;
        if (even_row && my_col > 0)
            dir = DIR_WEST;
        else if (!even_row && my_col < MESH_C - 1)
            dir = DIR_EAST;
        else
            dir = DIR_NORTH;
        *dbg_dir_recv = dir;
    }

    // -------------------------------------------------------------------------
    // Forward the token to the next tile in the snake, if there is one.
    // We embed our own TILE_ID in the payload so the receiver can verify
    // that the right tile sent it.
    // -------------------------------------------------------------------------
    int next_r, next_c;
    if (snake_next(my_row, my_col, &next_r, &next_c)) {
        uint32_t dest    = TILE_ID(next_r, next_c);
        uint32_t out_pay = TOKEN_VALID_BIT | (my_id & 0xFFu);
        *dbg_token_sent = out_pay;
        *dbg_dest_sent  = dest;
        noc_send(dest, out_pay);
    } else {
        /* Last tile — nothing to forward */
        *dbg_token_sent = 0;
        *dbg_dest_sent  = 0xFFu;
    }

    *dbg_done = 0xDEADu;

    /* Spin forever */
    while (1) {}
    return 0;
}