import numpy as np
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

# ============================================================
# Two-tape reverse-buffer loop animation
#
# Tape A and Tape B move in opposite directions.
# At each end there is a read/write bridge.
#
# Normal mode:
#   read the outgoing sample from one tape
#   write it directly onto the incoming side of the other tape
#
# Buffer mode (last L before reversal):
#   do NOT write to the other tape
#   store the outgoing samples into buffers
#
# Replay mode (first L after reversal):
#   write the REVERSED stored buffer from the other tape onto itself
#
# After replay:
#   continue with direct writing from the other tape
#
# IMPORTANT:
# If PASS_LEN = 2*L exactly, then:
#   - last L = buffering
#   - first L after reversal = replay
# which leaves no room for "direct" mode.
#
# For a visible direct phase, set PASS_LEN > 2*L, e.g. 3*L.
# ============================================================

# -------------------------
# Parameters
# -------------------------
L = 80                    # buffer window length
PASS_LEN = 3 * L          # set to 2*L for the strict interpretation
N = 2 * L                 # visible tape length (samples shown on each tape)
INTERVAL_MS = 35
SHIFT_DECAY = 0.998       # tiny decay to keep amplitudes under control
WRITE_GAIN = 1.0

# Initial tape contents
A = np.zeros(N)
B = np.zeros(N)

# Put some initial signals on the tapes
x1 = np.linspace(-1, 1, 40)
pkt1 = np.sin(2 * np.pi * 3 * np.linspace(0, 1, 40)) * np.exp(-4 * x1**2)
A[8:48] = pkt1

x2 = np.linspace(-1, 1, 50)
pkt2 = 0.8 * np.cos(2 * np.pi * 2 * np.linspace(0, 1, 50)) * np.exp(-3.5 * x2**2)
B[100:150] = pkt2[:max(0, min(50, N - 100))]

# State
direction = 1
# direction = +1:
#   Tape A moves left -> right
#   Tape B moves right -> left
#
# direction = -1:
#   Tape A moves right -> left
#   Tape B moves left -> right

step_in_pass = 0
cycle = 0

# Buffers collected during the last L before reversal
store_from_A = []
store_from_B = []

# Replay buffers used after reversal
replay_to_A = []
replay_to_B = []
replay_idx = 0

# For faint persistent trail visualization
A_trail = np.zeros(N)
B_trail = np.zeros(N)

# Optional external recording injection
def external_input(frame):
    # Small occasional injection so the loop keeps evolving
    if 60 < (frame % 240) < 95:
        return 0.35 * np.sin(2 * np.pi * (frame % 36) / 18.0)
    return 0.0

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def shift_tapes():
    """Shift tapes one sample according to current direction.
    Returns outgoing samples and functions for writing incoming samples.
    """
    global A, B, direction

    if direction == 1:
        # A moves left -> right
        A_out = A[-1]
        A[1:] = SHIFT_DECAY * A[:-1]

        # B moves right -> left
        B_out = B[0]
        B[:-1] = SHIFT_DECAY * B[1:]

        def write_A(v):
            A[0] = v

        def write_B(v):
            B[-1] = v

    else:
        # A moves right -> left
        A_out = A[0]
        A[:-1] = SHIFT_DECAY * A[1:]

        # B moves left -> right
        B_out = B[-1]
        B[1:] = SHIFT_DECAY * B[:-1]

        def write_A(v):
            A[-1] = v

        def write_B(v):
            B[0] = v

    return A_out, B_out, write_A, write_B

def start_reversal():
    """Reverse directions and prepare reversed replay buffers."""
    global direction, step_in_pass, cycle
    global store_from_A, store_from_B, replay_to_A, replay_to_B, replay_idx

    direction *= -1
    cycle += 1
    step_in_pass = 0

    # After reversal:
    #   Tape A writes reversed material that was stored from Tape B
    #   Tape B writes reversed material that was stored from Tape A
    replay_to_A = list(reversed(store_from_B))
    replay_to_B = list(reversed(store_from_A))
    replay_idx = 0

    store_from_A = []
    store_from_B = []

def one_step(frame):
    """Advance simulation by one sample/frame."""
    global step_in_pass, replay_idx
    global store_from_A, store_from_B
    global A_trail, B_trail

    A_out, B_out, write_A, write_B = shift_tapes()

    # Decide mode
    buffering_phase = step_in_pass >= (PASS_LEN - L)
    replay_phase = replay_idx < min(len(replay_to_A), len(replay_to_B))

    inj = external_input(frame)

    if replay_phase:
        mode = "replay reversed buffered recording"
        write_A(WRITE_GAIN * replay_to_A[replay_idx] + inj)
        write_B(WRITE_GAIN * replay_to_B[replay_idx])
        replay_idx += 1

    elif buffering_phase:
        mode = "buffer only (no cross-writing)"
        store_from_A.append(A_out)
        store_from_B.append(B_out)
        write_A(inj)   # blank / new input only
        write_B(0.0)

    else:
        mode = "direct write from the other tape"
        # read outgoing sample from one tape and write to the other
        write_A(WRITE_GAIN * B_out + inj)
        write_B(WRITE_GAIN * A_out)

    # Update faint persistent trails
    A_trail[:] = np.maximum(0.986 * A_trail, np.abs(A))
    B_trail[:] = np.maximum(0.986 * B_trail, np.abs(B))

    step_in_pass += 1

    # Reversal condition
    if step_in_pass >= PASS_LEN:
        start_reversal()

    return mode

# ------------------------------------------------------------
# Plotting
# ------------------------------------------------------------
fig, (ax, mem_ax) = plt.subplots(
    2, 1,
    figsize=(12, 7),
    gridspec_kw={"height_ratios": [4.5, 1.5]}
)

tape_x = np.linspace(0, 1, N)

def draw_arrow(ax, x0, y0, x1, y1, label):
    ax.annotate(
        "",
        xy=(x1, y1),
        xytext=(x0, y0),
        arrowprops=dict(arrowstyle="->", lw=2)
    )
    ax.text((x0 + x1) / 2, (y0 + y1) / 2 + 0.06, label, ha="center", fontsize=9)

def update(frame):
    mode = one_step(frame)

    ax.clear()
    mem_ax.clear()

    yA = 1.0
    yB = 0.0
    amp = 0.28

    ax.set_xlim(-0.08, 1.08)
    ax.set_ylim(-0.75, 1.70)
    ax.axis("off")

    # Base tape lines
    ax.plot([0, 1], [yA, yA], lw=12, alpha=0.18, solid_capstyle="round")
    ax.plot([0, 1], [yB, yB], lw=12, alpha=0.18, solid_capstyle="round")

    # Trails
    ax.fill_between(tape_x, yA, yA + amp * A_trail, alpha=0.10)
    ax.fill_between(tape_x, yB, yB + amp * B_trail, alpha=0.10)

    # Actual signal on tapes
    ax.plot(tape_x, yA + amp * A, lw=2.2)
    ax.plot(tape_x, yB + amp * B, lw=2.2)

    # End heads
    for xh in [0, 1]:
        ax.scatter([xh, xh], [yA, yB], s=120, marker="s", zorder=5)
        ax.plot([xh, xh], [yB, yA], lw=1.0, alpha=0.35)

    # Motion arrows
    if direction == 1:
        ax.annotate("", xy=(0.74, yA + 0.20), xytext=(0.55, yA + 0.20),
                    arrowprops=dict(arrowstyle="->", lw=2))
        ax.text(0.645, yA + 0.28, "Tape A moves →", ha="center", fontsize=10)

        ax.annotate("", xy=(0.26, yB + 0.20), xytext=(0.45, yB + 0.20),
                    arrowprops=dict(arrowstyle="->", lw=2))
        ax.text(0.355, yB + 0.28, "Tape B moves ←", ha="center", fontsize=10)

        draw_arrow(ax, 0.0, yB + 0.04, 0.0, yA - 0.04, "B → A")
        draw_arrow(ax, 1.0, yA - 0.04, 1.0, yB + 0.04, "A → B")
    else:
        ax.annotate("", xy=(0.26, yA + 0.20), xytext=(0.45, yA + 0.20),
                    arrowprops=dict(arrowstyle="->", lw=2))
        ax.text(0.355, yA + 0.28, "Tape A moves ←", ha="center", fontsize=10)

        ax.annotate("", xy=(0.74, yB + 0.20), xytext=(0.55, yB + 0.20),
                    arrowprops=dict(arrowstyle="->", lw=2))
        ax.text(0.645, yB + 0.28, "Tape B moves →", ha="center", fontsize=10)

        draw_arrow(ax, 0.0, yA - 0.04, 0.0, yB + 0.04, "A → B")
        draw_arrow(ax, 1.0, yB + 0.04, 1.0, yA - 0.04, "B → A")

    # Labels
    ax.text(0.5, 1.56, "Two-tape reverse-buffer loop", ha="center", fontsize=14)
    ax.text(
        0.5, 1.43,
        f"cycle={cycle} | pass position={step_in_pass}/{PASS_LEN} | mode: {mode}",
        ha="center", fontsize=10
    )
    ax.text(
        0.5, 1.31,
        f"L={L}, PASS_LEN={PASS_LEN}  "
        f"(strict description: PASS_LEN=2*L; visible direct phase: PASS_LEN>2*L)",
        ha="center", fontsize=9
    )

    ax.text(-0.055, yA, "Tape A", ha="right", va="center", fontsize=10)
    ax.text(-0.055, yB, "Tape B", ha="right", va="center", fontsize=10)

    ax.text(0.0, yB - 0.55, "left read/write end", ha="center", fontsize=9)
    ax.text(1.0, yB - 0.55, "right read/write end", ha="center", fontsize=9)

    # Memory axis
    mem_ax.set_xlim(0, L if L > 1 else 2)
    mem_ax.set_ylim(-1.2, 1.2)
    mem_ax.axhline(0, lw=0.8, alpha=0.35)
    mem_ax.set_title("Stored end buffers (used after reversal, written reversed)", fontsize=10)
    mem_ax.set_xlabel("buffer sample index")
    mem_ax.set_yticks([])

    if len(store_from_A) > 1:
        mem_ax.plot(np.arange(len(store_from_A)), store_from_A, label="stored from Tape A")
    if len(store_from_B) > 1:
        mem_ax.plot(np.arange(len(store_from_B)), store_from_B, label="stored from Tape B")

    # show replay cursor if replaying
    if replay_idx < min(len(replay_to_A), len(replay_to_B)) and len(replay_to_A) > 0:
        mem_ax.axvline(replay_idx, lw=1.2, alpha=0.6)

    mem_ax.legend(loc="upper right")

    return []

ani = FuncAnimation(fig, update, frames=3000, interval=INTERVAL_MS, blit=False)
plt.tight_layout()
plt.show()
