#include <stdint.h>

// ============================================================================
// Multi-pattern snake connectivity test
//
// Compile with -DPATTERN=0 .. -DPATTERN=3 to select the traversal.
//
// Pattern 0 — Horizontal boustrophedon (original H-snake)
//   Even rows go →, odd rows go ←.  Uses E/W/S links.
//
// Pattern 1 — SE diagonal snake  (antidiagonal stripes, NW corner first)
//   Walks tiles in antidiagonal order: (0,0), (1,0),(0,1), (2,0),(1,1),(0,2) …
//   Within each antidiagonal the order is column-ascending.
//   Each hop is either one step SOUTH or one step EAST depending on whether
//   the sender and receiver are on the same row.
//   The crucial sub-case: when two consecutive antidiagonal tiles are
//   (r,c) → (r-1,c+1), the sender writes to its SE neighbour, exercising
//   se_out → nw_in.
//   NOTE: the XY router still forwards cardinally, but for single-step
//   diagonal hops the direct tile is both south-east of sender.  To force
//   the diagonal wire to carry the flit the sender must write dest = (r+1,c+1)
//   and the flit will transit se_out of sender → nw_in of receiver in one cycle
//   because those wires connect immediately-adjacent tiles.  The router *inside*
//   the receiver sees the flit on nw_in and ejects it (dest == my_id).
//   In the router code nw_in is checked as a transit input, so this works
//   correctly with your existing mesh_router.v.
//
// Pattern 2 — SW diagonal snake  (antidiagonal stripes, NE corner first)
//   Mirror of pattern 1.  Starts at (0, MESH_C-1), walks antidiagonals with
//   column-descending order within each stripe.
//   Each hop is either one step SOUTH or one step WEST.
//   Diagonal sub-case: (r,c) → (r+1,c-1) exercises sw_out → ne_in.
//
// Pattern 3 — Vertical boustrophedon (column-first V-snake)
//   Even columns go ↓, odd columns go ↑.  Uses N/S/E links.
//   Provides a second pass over all N/S wires in a different sequence so
//   ordering hazards are exposed.
//
// For patterns 1 and 2 a tile MUST know which wire its incoming token
// arrived on.  Because the router ejects from all 9 inputs into a single
// FIFO the firmware cannot distinguish NW from N from E at the FIFO level.
// Instead the *logical* direction is inferred from the snake ordering:
//   pattern 1: predecessor is either (r-1, c) [came from N] or (r, c-1) [came from W]
//              when the hop is purely diagonal the predecessor is (r-1, c+1) →
//              that means it came via NW (sender wrote to SE = our NW input).
//   pattern 2: symmetric.
//
// ============================================================================

#ifndef PATTERN
#define PATTERN 0
#endif

// ============================================================================
// Mesh configuration
// ============================================================================
#define MESH_R  5
#define MESH_C  5

// ============================================================================
// NoC memory-mapped registers
// ============================================================================
#define NOC_INJECT_BASE  0x80000000u
#define NOC_RECV_BASE    0x80000004u
#define NOC_ID_BASE      0x80000008u

// TILE_ID encoding: row in [5:3], col in [2:0]
#define TILE_ID(r, c)   ((((uint32_t)(r) & 0x7u) << 3) | ((uint32_t)(c) & 0x7u))

#define FLIT_DEST_SHIFT  26u
#define TOKEN_VALID_BIT  (1u << 10)

// ============================================================================
// Debug memory map
// ============================================================================
#define DEBUG_BASE          0x0700u
#define DEBUG_MY_ID         (DEBUG_BASE +  0)
#define DEBUG_TOKEN_RECV    (DEBUG_BASE +  4)
#define DEBUG_DIR_RECV      (DEBUG_BASE +  8)
#define DEBUG_TOKEN_SENT    (DEBUG_BASE + 12)
#define DEBUG_DEST_SENT     (DEBUG_BASE + 16)
#define DEBUG_DONE          (DEBUG_BASE + 20)
// Stores which PATTERN this tile ran (testbench can cross-check)
#define DEBUG_PATTERN       (DEBUG_BASE + 24)

// ---- Direction codes -------------------------------------------------------
// Cardinal
#define DIR_SELF   0u   // tile (0,0) or pattern start — no receive
#define DIR_WEST   1u
#define DIR_EAST   2u
#define DIR_NORTH  3u
#define DIR_SOUTH  4u
// Diagonal
#define DIR_NW     5u   // token arrived on the NW input (sent by SE neighbour)
#define DIR_NE     6u   // token arrived on the NE input (sent by SW neighbour)
#define DIR_SE     7u   // token arrived on the SE input  (unused — reserved)
#define DIR_SW     8u   // token arrived on the SW input  (unused — reserved)

// ============================================================================
// Typed SRAM pointers
// ============================================================================
#define dbg_my_id       ((volatile uint32_t *)DEBUG_MY_ID)
#define dbg_token_recv  ((volatile uint32_t *)DEBUG_TOKEN_RECV)
#define dbg_dir_recv    ((volatile uint32_t *)DEBUG_DIR_RECV)
#define dbg_token_sent  ((volatile uint32_t *)DEBUG_TOKEN_SENT)
#define dbg_dest_sent   ((volatile uint32_t *)DEBUG_DEST_SENT)
#define dbg_done        ((volatile uint32_t *)DEBUG_DONE)
#define dbg_pattern     ((volatile uint32_t *)DEBUG_PATTERN)

// ============================================================================
// NoC helpers
// ============================================================================
static inline void noc_send(uint32_t dest_id, uint32_t payload)
{
    *(volatile uint32_t *)NOC_INJECT_BASE =
        (dest_id << FLIT_DEST_SHIFT) | TOKEN_VALID_BIT | (payload & 0x03FFFFFFu);
}

static inline uint32_t noc_recv_token(void)
{
    uint32_t p;
    do { p = *(volatile uint32_t *)NOC_RECV_BASE; } while (!(p & TOKEN_VALID_BIT));
    return p;
}

static inline uint32_t noc_read_my_id(void)
{
    return *(volatile uint32_t *)NOC_ID_BASE & 0x3Fu;
}

// ============================================================================
// Pattern 0: Horizontal boustrophedon
//   Even rows left→right, odd rows right→left, step south at row end.
// ============================================================================
#if PATTERN == 0

static int snake_next(int r, int c, int *nr, int *nc)
{
    int even = ((r & 1) == 0);
    if (even  && c < MESH_C - 1) { *nr = r;     *nc = c + 1; return 1; }
    if (!even && c > 0)           { *nr = r;     *nc = c - 1; return 1; }
    if (r < MESH_R - 1)           { *nr = r + 1; *nc = c;     return 1; }
    return 0;
}

static int is_head(int r, int c) { return (r == 0 && c == 0); }

// Direction from which the token arrived at (r,c).
// Row-start tiles receive from the row above (north_in), all others from E or W.
static uint32_t arrival_dir(int r, int c)
{
    int even = ((r & 1) == 0);
    if (even && c > 0)            return DIR_WEST;
    if (!even && c < MESH_C - 1) return DIR_EAST;
    return DIR_NORTH;
}

#endif // PATTERN == 0

// ============================================================================
// Pattern 1: SE diagonal snake
//   Antidiagonal d = r + c.  Within each antidiagonal, tiles are visited in
//   order of increasing row (= decreasing col).
//   Successor of tile (r,c):
//     - if r > 0 on the same antidiagonal → (r-1, c+1)  [one step NE — but
//       we are the SENDER, so we write to (r-1,c+1).  (r-1,c+1) is our NW
//       neighbour only when r-1 = r-1 and c+1 = c+1… wait, let's be precise:
//       (r-1,c+1) is NORTH-EAST of us.  From sender's perspective we output
//       on ne_out; from receiver's perspective it arrives on sw_in.
//       Actually no: let's re-check the direction convention in mesh_rxc.v:
//         ne_i of (r,c) = grid_sw[r-1][c+1]   ← from tile above-right
//       So (r,c)'s ne_in carries the sw_out of (r-1,c+1).
//       Conversely, (r,c)'s ne_out feeds the sw_in of (r-1,c+1).
//       If we want to send from (r,c) to (r-1,c+1) via diagonal wire:
//         sender (r,c) ne_out → receiver (r-1,c+1) sw_in.
//       That uses the NE output of sender = SW input of receiver.
//     - At row-start of next antidiagonal → step SOUTH to (r+1, c)
//       or EAST to (r, c+1) depending on boundary.
//
//   To keep things clean, within each antidiag we step DOWN the column
//   (increasing r, decreasing c).  That means:
//     - intra-antidiag hop: (r,c) → (r+1,c-1)  [one step south-west]
//       sender uses sw_out, receiver sees it on ne_in.
//     - antidiag transition: last tile of antidiag d to first tile of d+1.
//       Last tile: largest r on antidiag d.  First tile: smallest r on d+1.
//       This hop is either SOUTH (same col, row+1) or EAST (same row, col+1).
//
//   So pattern 1 actually exercises sw_out → ne_in for intra-antidiag hops.
//   Rename to "SW-diagonal within antidiag" to avoid confusion.
//   The pure diagonal link tested:  sw_out / ne_in.
//
// ============================================================================
#if PATTERN == 1

// Compute the position of tile with visit-order index idx in the
// antidiagonal snake.  We scan antidiagonals 0..2*(N-1), and within each
// antidiagonal we visit in order of increasing row.
// Returns (r,c) for the given index.
static void idx_to_rc(int idx, int *pr, int *pc)
{
    int count = 0;
    int d;
    for (d = 0; d < MESH_R + MESH_C - 1; d++) {
        int r_start = (d < MESH_C) ? 0 : d - (MESH_C - 1);
        int r_end   = (d < MESH_R) ? d : MESH_R - 1;
        for (int r = r_start; r <= r_end; r++) {
            if (count == idx) { *pr = r; *pc = d - r; return; }
            count++;
        }
    }
    *pr = -1; *pc = -1;
}

// Return the visit-order index of tile (r,c) in pattern 1.
static int rc_to_idx(int r, int c)
{
    int d = r + c;
    int r_start = (d < MESH_C) ? 0 : d - (MESH_C - 1);
    int base = 0;
    // Sum tiles in antidiagonals 0..d-1
    for (int di = 0; di < d; di++) {
        int rs = (di < MESH_C) ? 0 : di - (MESH_C - 1);
        int re = (di < MESH_R) ? di : MESH_R - 1;
        base += re - rs + 1;
    }
    return base + (r - r_start);
}

static int snake_next(int r, int c, int *nr, int *nc)
{
    int idx = rc_to_idx(r, c);
    int total = MESH_R * MESH_C;
    if (idx + 1 >= total) return 0;
    idx_to_rc(idx + 1, nr, nc);
    return 1;
}

static int is_head(int r, int c) { return (r == 0 && c == 0); }

// Direction the token arrived at (r,c) from its predecessor.
// Predecessor: tile with index rc_to_idx(r,c) - 1.
static uint32_t arrival_dir(int r, int c)
{
    int idx = rc_to_idx(r, c);
    if (idx == 0) return DIR_SELF;
    int pr, pc;
    idx_to_rc(idx - 1, &pr, &pc);
    // Predecessor is at (pr,pc); we are at (r,c).
    if (pr == r - 1 && pc == c + 1) return DIR_NE; // came via NE diagonal (sw_out of NE nbr)
    if (pr == r - 1 && pc == c)     return DIR_NORTH;
    if (pr == r && pc == c - 1)     return DIR_WEST;
    return DIR_NORTH; // fallback
}

#endif // PATTERN == 1

// ============================================================================
// Pattern 2: SW diagonal snake  (NE corner first)
//   Mirror of pattern 1.  Start at (0, MESH_C-1).
//   Within each antidiagonal visit in order of decreasing row (increasing col).
//   Intra-antidiag hop: (r,c) → (r-1,c+1)  = NE direction.
//   sender: ne_out → receiver sw_in.
//   Exercises ne_out / sw_in.
// ============================================================================
#if PATTERN == 2

static void idx_to_rc(int idx, int *pr, int *pc)
{
    int count = 0;
    for (int d = 0; d < MESH_R + MESH_C - 1; d++) {
        int r_start = (d < MESH_C) ? 0 : d - (MESH_C - 1);
        int r_end   = (d < MESH_R) ? d : MESH_R - 1;
        // Visit in DECREASING row order (= increasing col)
        for (int r = r_end; r >= r_start; r--) {
            if (count == idx) { *pr = r; *pc = d - r; return; }
            count++;
        }
    }
    *pr = -1; *pc = -1;
}

static int rc_to_idx(int r, int c)
{
    int d = r + c;
    int r_start = (d < MESH_C) ? 0 : d - (MESH_C - 1);
    int r_end   = (d < MESH_R) ? d : MESH_R - 1;
    int base = 0;
    for (int di = 0; di < d; di++) {
        int rs = (di < MESH_C) ? 0 : di - (MESH_C - 1);
        int re = (di < MESH_R) ? di : MESH_R - 1;
        base += re - rs + 1;
    }
    // Within antidiag d, our position is (r_end - r) in decreasing-r order
    return base + (r_end - r);
}

static int snake_next(int r, int c, int *nr, int *nc)
{
    int idx = rc_to_idx(r, c);
    if (idx + 1 >= MESH_R * MESH_C) return 0;
    idx_to_rc(idx + 1, nr, nc);
    return 1;
}

// Head is tile with visit-order 0
static int is_head(int r, int c)
{
    int pr, pc;
    idx_to_rc(0, &pr, &pc);
    return (r == pr && c == pc);
}

static uint32_t arrival_dir(int r, int c)
{
    int idx = rc_to_idx(r, c);
    if (idx == 0) return DIR_SELF;
    int pr, pc;
    idx_to_rc(idx - 1, &pr, &pc);
    if (pr == r + 1 && pc == c - 1) return DIR_SW; // arrived via SW diagonal
    if (pr == r + 1 && pc == c)     return DIR_SOUTH;
    if (pr == r && pc == c + 1)     return DIR_EAST;
    return DIR_SOUTH; // fallback
}

#endif // PATTERN == 2

// ============================================================================
// Pattern 3: Vertical boustrophedon
//   Even cols top→bottom, odd cols bottom→top, step east at col end.
// ============================================================================
#if PATTERN == 3

static int snake_next(int r, int c, int *nr, int *nc)
{
    int even = ((c & 1) == 0);
    if (even  && r < MESH_R - 1) { *nr = r + 1; *nc = c;     return 1; }
    if (!even && r > 0)           { *nr = r - 1; *nc = c;     return 1; }
    if (c < MESH_C - 1)           { *nr = r;     *nc = c + 1; return 1; }
    return 0;
}

static int is_head(int r, int c) { return (r == 0 && c == 0); }

static uint32_t arrival_dir(int r, int c)
{
    int even = ((c & 1) == 0);
    if (even && r > 0)            return DIR_NORTH;  // stepped down, came from above
    if (!even && r < MESH_R - 1) return DIR_SOUTH;   // stepped up, came from below
    return DIR_WEST;  // arrived from the column to our left
}

#endif // PATTERN == 3

// ============================================================================
// _start
// ============================================================================
__attribute__((section(".text.init"), naked))
void _start(void)
{
    __asm__ volatile (
        "li   sp, 0x7fc\n"
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
    int      my_row = (int)((my_id >> 3) & 0x7u);
    int      my_col = (int)(my_id & 0x7u);

    *dbg_my_id   = my_id;
    *dbg_pattern = (uint32_t)PATTERN;

    int head = is_head(my_row, my_col);

    uint32_t token_payload;
    if (head) {
        token_payload   = TOKEN_VALID_BIT | (my_id & 0xFFu);
        *dbg_dir_recv   = DIR_SELF;
        *dbg_token_recv = token_payload;
    } else {
        token_payload   = noc_recv_token();
        *dbg_token_recv = token_payload;
        *dbg_dir_recv   = arrival_dir(my_row, my_col);
    }

    int next_r, next_c;
    if (snake_next(my_row, my_col, &next_r, &next_c)) {
        uint32_t dest    = TILE_ID(next_r, next_c);
        uint32_t out_pay = TOKEN_VALID_BIT | (my_id & 0xFFu);
        *dbg_token_sent = out_pay;
        *dbg_dest_sent  = dest;
        noc_send(dest, out_pay);
    } else {
        *dbg_token_sent = 0;
        *dbg_dest_sent  = 0xFFu;
    }

    *dbg_done = 0xDEADu;

    while (1) {}
    return 0;
}
