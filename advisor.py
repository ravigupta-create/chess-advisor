#!/usr/bin/env python3
"""
Chess Advisor — Stockfish 18 at MAXIMUM strength.
Auto-detects opponent moves from Apple Chess.app via screen capture.
100% free. No typing opponent moves — just play!
"""

import chess
import chess.engine
import chess.pgn
import sys
import os
import re
import time
import io
import subprocess
import tempfile
import multiprocessing
from collections import OrderedDict

# ── Screen capture imports ─────────────────────────────────────────────
try:
    import Quartz
    from PIL import Image
    VISION_AVAILABLE = True
except ImportError:
    VISION_AVAILABLE = False

# ── Auto-detect system resources ───────────────────────────────────────
CPU_COUNT = multiprocessing.cpu_count()
ENGINE_THREADS = max(1, CPU_COUNT - 1)
STOCKFISH_PATH = "/opt/homebrew/bin/stockfish"

# ── Engine configuration — MAXED OUT ───────────────────────────────────
ENGINE_CONFIG = {
    "Threads": ENGINE_THREADS,
    "Hash": 2048,
    "Skill Level": 20,
    "UCI_LimitStrength": False,
    "UCI_ShowWDL": True,
}

# ── Analysis settings ──────────────────────────────────────────────────
DEFAULT_TIME = 3.0
CRITICAL_TIME = 8.0
QUICK_TIME = 1.5
NORMAL_MULTIPV = 3
CRITICAL_MULTIPV = 5
PV_DEPTH_DISPLAY = 8
CACHE_MAX_SIZE = 200

# ── Screen capture settings ────────────────────────────────────────────
POLL_INTERVAL = 0.4          # Seconds between screen checks
RENDER_SETTLE_DELAY = 0.3    # Wait for Chess.app animation to finish
DIFF_THRESHOLD = 20          # Min avg pixel difference to count as changed

# ── ANSI colors ────────────────────────────────────────────────────────
CLEAR = "\033[2J\033[H"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
WHITE_FG = "\033[97m"
BLACK_FG = "\033[30m"
WHITE_BG = "\033[47m"
DARK_BG = "\033[100m"

PIECE_NAMES = {
    chess.PAWN: "Pawn", chess.KNIGHT: "Knight", chess.BISHOP: "Bishop",
    chess.ROOK: "Rook", chess.QUEEN: "Queen", chess.KING: "King",
}
UNICODE_PIECES = {
    (chess.PAWN, chess.WHITE): "♙", (chess.KNIGHT, chess.WHITE): "♘",
    (chess.BISHOP, chess.WHITE): "♗", (chess.ROOK, chess.WHITE): "♖",
    (chess.QUEEN, chess.WHITE): "♕", (chess.KING, chess.WHITE): "♔",
    (chess.PAWN, chess.BLACK): "♟", (chess.KNIGHT, chess.BLACK): "♞",
    (chess.BISHOP, chess.BLACK): "♝", (chess.ROOK, chess.BLACK): "♜",
    (chess.QUEEN, chess.BLACK): "♛", (chess.KING, chess.BLACK): "♚",
}


# ═══════════════════════════════════════════════════════════════════════
#  BOARD WATCHER — Auto-detects moves from Chess.app screen
# ═══════════════════════════════════════════════════════════════════════

class BoardWatcher:
    """Watches Apple Chess.app and auto-detects opponent moves via screen capture."""

    def __init__(self):
        self.window_id = None
        self.col_edges = None       # X positions of 9 column boundaries
        self.row_top = None         # Y of top row
        self.row_bottom = None      # Y of bottom row
        self.scale = 1.0            # Retina scale factor
        self.available = False
        self.reference_image = None  # Screenshot before opponent moves

    def initialize(self):
        """Find Chess.app window and calibrate board grid."""
        if not VISION_AVAILABLE:
            print(f"  {YELLOW}Auto-detection unavailable (needs Pillow + Quartz){RESET}")
            return False

        # Bring Chess.app to front so we can capture it
        try:
            subprocess.run(['osascript', '-e', 'tell application "Chess" to activate'],
                           capture_output=True, timeout=3)
            time.sleep(0.5)
        except Exception:
            pass

        # Find Chess.app window
        winfo = self._find_chess_window()
        if not winfo:
            print(f"  {YELLOW}Chess.app window not found — using manual mode{RESET}")
            return False

        self.window_id = winfo['id']
        print(f"  {DIM}Found Chess.app (window {self.window_id}){RESET}")

        # Capture and calibrate
        img = self._capture_window()
        if img is None:
            print(f"  {YELLOW}Could not capture Chess.app — check Screen Recording permission{RESET}")
            print(f"  {DIM}System Settings → Privacy & Security → Screen Recording → enable Terminal{RESET}")
            return False

        if self._calibrate_grid(img):
            self.available = True
            print(f"  {GREEN}Board detection calibrated — auto-detect ON{RESET}")
            return True
        else:
            print(f"  {YELLOW}Could not detect board grid — using manual mode{RESET}")
            return False

    def _find_chess_window(self):
        """Find the main Chess.app game window via Quartz. Prefers on-screen windows."""
        try:
            windows = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID
            )
            candidates = []
            for w in windows:
                if w.get('kCGWindowOwnerName') != 'Chess':
                    continue
                name = w.get('kCGWindowName', '')
                bounds = w.get('kCGWindowBounds', {})
                height = bounds.get('Height', 0)
                onscreen = w.get('kCGWindowIsOnscreen', False)
                if 'Game' in name and height > 200:
                    candidates.append({
                        'id': w['kCGWindowNumber'],
                        'name': name,
                        'bounds': dict(bounds),
                        'onscreen': bool(onscreen),
                    })
            if not candidates:
                return None
            # Prefer on-screen windows, then largest
            candidates.sort(key=lambda c: (c['onscreen'], c['bounds'].get('Height', 0)), reverse=True)
            return candidates[0]
        except Exception:
            return None

    def _capture_window(self):
        """Capture the Chess.app window using screencapture. Cleans up temp file."""
        if self.window_id is None:
            return None
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix='.png', prefix='chess_adv_')
            os.close(fd)
            result = subprocess.run(
                ['screencapture', '-l', str(self.window_id), '-x', '-o', '-t', 'png', tmp],
                capture_output=True, timeout=5
            )
            if result.returncode != 0:
                os.unlink(tmp)
                return None
            img = Image.open(tmp)
            img.load()
            os.unlink(tmp)
            # Detect blank/black images (macOS returns black for background windows)
            pixels = img.load()
            w, h = img.size
            step_y = max(1, h // 8)
            step_x = max(1, w // 8)
            non_black = 0
            for sy in range(h // 4, h * 3 // 4, step_y):
                for sx in range(w // 4, w * 3 // 4, step_x):
                    r, g, b = pixels[sx, sy][:3]
                    if r > 10 or g > 10 or b > 10:
                        non_black += 1
            if non_black == 0:
                return None  # All-black image = window not rendered
            return img
        except Exception:
            try:
                if tmp:
                    os.unlink(tmp)
            except Exception:
                pass
            return None

    def _is_board_pixel(self, r, g, b):
        """Check if pixel is a board square. Supports Wood, Marble, Metal themes."""
        # Black/very dark = window border or background
        if r < 30 and g < 30 and b < 30:
            return False
        # Pure gray window chrome (neutral, low brightness)
        if abs(r - g) < 8 and abs(g - b) < 8 and r < 60:
            return False
        # Liquid Glass translucent toolbar (semi-transparent gray)
        if abs(r - g) < 12 and abs(g - b) < 12 and 60 <= r <= 180:
            return False
        # Wood theme: warm tones
        if r > 70 and (r - b) > 10:
            return True
        # Marble theme: bright/white-ish
        if (r + g + b) > 280:
            return True
        # Metal theme: blue/silver tones
        if r > 60 and g > 60 and b > 60 and (r + g + b) > 200:
            return True
        return False

    def _calibrate_grid(self, img):
        """Detect the 8x8 board grid within the captured window image."""
        w, h = img.size
        pixels = img.load()  # Fast pixel access object

        # Find board horizontal extent by scanning along vertical center
        cy = h // 2
        left = right = 0
        for x in range(w):
            r, g, b = pixels[x, cy][:3]
            if self._is_board_pixel(r, g, b):
                left = x
                break
        for x in range(w - 1, -1, -1):
            r, g, b = pixels[x, cy][:3]
            if self._is_board_pixel(r, g, b):
                right = x
                break

        if right - left < 100:
            return False

        # Find column boundaries by detecting brightness transitions
        brightness = []
        for x in range(left, right + 1):
            r, g, b = pixels[x, cy][:3]
            brightness.append((r + g + b) / 3)

        win = 5
        transitions = []
        for i in range(win, len(brightness) - win):
            avg_before = sum(brightness[i - win:i]) / win
            avg_after = sum(brightness[i:i + win]) / win
            diff = abs(avg_after - avg_before)
            if diff > 12:
                transitions.append((left + i, diff))

        # Filter close transitions (keep strongest)
        filtered = []
        for x, diff in transitions:
            if not filtered or x - filtered[-1][0] > 30:
                filtered.append((x, diff))
            elif diff > filtered[-1][1]:
                filtered[-1] = (x, diff)

        # Use detected transitions if we got exactly 9, otherwise estimate from board extent
        if len(filtered) == 9:
            self.col_edges = [x for x, _ in filtered]
        else:
            board_width = right - left
            sq_w = board_width / 8
            self.col_edges = [int(left + i * sq_w) for i in range(9)]

        # Find vertical board extent
        cx = w // 2
        top = bottom = 0
        for y in range(50, h):
            r, g, b = pixels[cx, y][:3]
            if self._is_board_pixel(r, g, b):
                top = y
                break
        for y in range(h - 1, 0, -1):
            r, g, b = pixels[cx, y][:3]
            if self._is_board_pixel(r, g, b):
                bottom = y
                break

        if bottom - top < 100:
            return False

        self.row_top = top
        self.row_bottom = bottom
        return True

    def _get_square_center(self, file, rank):
        """Get pixel coordinates for the center of a board square."""
        # File 0=a, 7=h. Rank 0=1(bottom), 7=8(top)
        # In the image: file goes left-to-right, rank 7 is at top
        x = (self.col_edges[file] + self.col_edges[file + 1]) // 2
        row_height = (self.row_bottom - self.row_top) / 8
        # Rank 7 (row 8) is at the top of the image
        y = int(self.row_top + (7 - rank + 0.5) * row_height)
        return x, y

    def _get_square_diff(self, px1, px2, w, h, file, rank):
        """Compare a square between two screenshots using fast pixel access."""
        cx, cy = self._get_square_center(file, rank)
        sq_w = (self.col_edges[1] - self.col_edges[0]) if len(self.col_edges) > 1 else 100
        half = max(5, int(sq_w * 0.25))

        total_diff = 0
        count = 0
        for dy in range(-half, half + 1, 3):
            for dx in range(-half, half + 1, 3):
                px, py = cx + dx, cy + dy
                if 0 <= px < w and 0 <= py < h:
                    r1, g1, b1 = px1[px, py][:3]
                    r2, g2, b2 = px2[px, py][:3]
                    total_diff += abs(r1 - r2) + abs(g1 - g2) + abs(b1 - b2)
                    count += 1

        return total_diff / max(count, 1) / 3

    def _detect_changed_squares(self, before, after):
        """Find squares that changed between two screenshots."""
        # Use fast pixel access objects (loaded once, used for all 64 squares)
        px1 = before.load()
        px2 = after.load()
        w = min(before.width, after.width)
        h = min(before.height, after.height)
        changed = []
        for rank in range(8):
            for file in range(8):
                diff = self._get_square_diff(px1, px2, w, h, file, rank)
                if diff > DIFF_THRESHOLD:
                    changed.append((file, rank, diff))
        changed.sort(key=lambda x: -x[2])
        return changed

    def _deduce_move(self, changed_squares, board):
        """From changed squares and known board state, find the matching legal move."""
        if len(changed_squares) < 2:
            return None

        changed_set = {chess.square(f, r) for f, r, _ in changed_squares}

        best_match = None
        best_score = -1

        for move in board.legal_moves:
            affected = {move.from_square, move.to_square}

            if board.is_castling(move):
                rank = chess.square_rank(move.from_square)
                if board.is_kingside_castling(move):
                    affected.update({chess.square(7, rank), chess.square(5, rank)})
                else:
                    affected.update({chess.square(0, rank), chess.square(3, rank)})

            if board.is_en_passant(move):
                cap_sq = chess.square(chess.square_file(move.to_square),
                                     chess.square_rank(move.from_square))
                affected.add(cap_sq)

            # Score: how many affected squares are in the changed set
            overlap = len(affected & changed_set)
            # Penalize if many changed squares are NOT in affected (noise)
            noise = len(changed_set - affected)
            score = overlap * 10 - noise

            if overlap >= len(affected) and score > best_score:
                best_score = score
                best_match = move

        return best_match

    def get_window_title(self):
        """Get the current window title using cached window ID (fast path)."""
        if self.window_id is not None:
            try:
                # Query only the specific window by ID — much faster than enumerating all
                info_list = Quartz.CGWindowListCopyWindowInfo(
                    Quartz.kCGWindowListOptionIncludingWindow, self.window_id
                )
                if info_list and len(info_list) > 0:
                    name = info_list[0].get('kCGWindowName', '')
                    if name:
                        return name
            except Exception:
                pass
        # Fallback: full enumeration if cached ID failed
        try:
            winfo = self._find_chess_window()
            if winfo:
                self.window_id = winfo['id']
                return winfo['name']
        except Exception:
            pass
        return None

    def is_turn(self, color):
        """Check if it's a specific color's turn based on window title."""
        title = self.get_window_title()
        if title is None:
            return None
        if color == chess.WHITE:
            return 'White to Move' in title
        else:
            return 'Black to Move' in title

    def take_reference(self):
        """Take a reference screenshot (before opponent moves)."""
        self.reference_image = self._capture_window()

    def wait_for_opponent_move(self, board, playing_as):
        """
        Poll Chess.app until the opponent moves, then detect what move was made.
        Returns the detected chess.Move, or None if detection fails.
        """
        if not self.available:
            return None

        # Take reference screenshot if we don't have one
        if self.reference_image is None:
            self.reference_image = self._capture_window()

        my_turn_str = 'White to Move' if playing_as == chess.WHITE else 'Black to Move'
        dots = 0
        max_wait = 600  # 10 minute timeout
        waited = 0

        while waited < max_wait:
            time.sleep(POLL_INTERVAL)

            title = self.get_window_title()
            if title is None:
                # Chess.app might have closed
                return None

            # Check if it's our turn now (opponent finished)
            if my_turn_str in title:
                # Wait for animation to settle
                time.sleep(RENDER_SETTLE_DELAY)

                # Capture the new board state
                after = self._capture_window()
                if after is None:
                    return None

                before = self.reference_image

                # Detect changed squares
                changed = self._detect_changed_squares(before, after)
                if changed:
                    move = self._deduce_move(changed, board)
                    if move:
                        self.reference_image = None  # Reset for next cycle
                        return move

                # If detection failed, try once more with a longer settle
                time.sleep(0.5)
                after = self._capture_window()
                if after:
                    changed = self._detect_changed_squares(before, after)
                    if changed:
                        move = self._deduce_move(changed, board)
                        if move:
                            self.reference_image = None
                            return move

                # Detection failed
                self.reference_image = None
                return None

            # Check for game over in title
            if 'Checkmate' in title or 'Draw' in title or 'Stalemate' in title:
                return None

            # Still opponent's turn — show waiting dots
            waited += POLL_INTERVAL
            dots = (dots + 1) % 4
            sys.stdout.write(f"\r  {DIM}Watching Chess.app{'.' * dots}{'   '}{RESET}")
            sys.stdout.flush()

        # Timeout
        self.reference_image = None
        return None


# ═══════════════════════════════════════════════════════════════════════
#  LRU CACHE
# ═══════════════════════════════════════════════════════════════════════

class LRUCache:
    """Simple LRU cache with bounded size."""

    def __init__(self, max_size=CACHE_MAX_SIZE):
        self._cache = OrderedDict()
        self._max_size = max_size

    def get(self, key):
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def put(self, key, value):
        if key in self._cache:
            self._cache.move_to_end(key)
        else:
            if len(self._cache) >= self._max_size:
                self._cache.popitem(last=False)
        self._cache[key] = value


# ═══════════════════════════════════════════════════════════════════════
#  CHESS ADVISOR — Main engine
# ═══════════════════════════════════════════════════════════════════════

class ChessAdvisor:
    """Maximum-strength chess advisor powered by Stockfish 18."""

    def __init__(self):
        self.board = chess.Board()
        self.engine = None
        self.playing_as = chess.WHITE
        self.player_name = "Player"
        self.analysis_cache = LRUCache()
        self.game_pgn = chess.pgn.Game()
        self.pgn_node = self.game_pgn
        self.last_eval = None
        self.watcher = BoardWatcher()
        self.auto_detect = False

    def start_engine(self):
        self.engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
        self.engine.configure(ENGINE_CONFIG)

    def stop_engine(self):
        if self.engine:
            try:
                self.engine.quit()
            except Exception:
                pass
            self.engine = None

    # ── Analysis ───────────────────────────────────────────────────────

    def is_critical_position(self):
        if self.board.is_check():
            return True
        if len(self.board.move_stack) < 6:
            return False
        our_king = self.board.king(self.playing_as)
        their_king = self.board.king(not self.playing_as)
        if our_king is not None and len(self.board.attackers(not self.playing_as, our_king)) > 0:
            return True
        if their_king is not None and len(self.board.attackers(self.playing_as, their_king)) > 0:
            return True
        if len(self.board.piece_map()) <= 10:
            return True
        if sum(1 for m in self.board.legal_moves if self.board.is_capture(m)) >= 4:
            return True
        return False

    def analyze_position(self, multipv=NORMAL_MULTIPV, think_time=None):
        fen = self.board.fen()
        cache_key = (fen, multipv, think_time)
        cached = self.analysis_cache.get(cache_key)
        if cached is not None:
            return cached, False

        critical = self.is_critical_position()
        if think_time is None:
            think_time = CRITICAL_TIME if critical else DEFAULT_TIME

        result = self.engine.analyse(
            self.board,
            chess.engine.Limit(time=think_time),
            multipv=multipv,
        )
        self.analysis_cache.put(cache_key, result)
        return result, critical

    def extract_threats_from_pv(self, results):
        threats = []
        for info in results[:3]:
            pv = info.get("pv", [])
            if len(pv) >= 2:
                our_move, their_reply = pv[0], pv[1]
                temp = self.board.copy()
                temp.push(our_move)
                if temp.is_capture(their_reply):
                    captured = temp.piece_at(their_reply.to_square)
                    if captured and captured.piece_type in (chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT):
                        threats.append(("capture", their_reply, captured))
                temp.push(their_reply)
                if temp.is_check():
                    threats.append(("check_after", their_reply, None))
        return threats[:5]

    def get_game_phase(self):
        piece_count = len(self.board.piece_map())
        queens = len(self.board.pieces(chess.QUEEN, chess.WHITE)) + \
                 len(self.board.pieces(chess.QUEEN, chess.BLACK))
        move_num = self.board.fullmove_number
        if move_num <= 10 and piece_count >= 28:
            return "Opening"
        elif piece_count <= 12 or (queens == 0 and piece_count <= 16):
            return "Endgame"
        return "Middlegame"

    def get_material_balance(self):
        values = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3,
                  chess.ROOK: 5, chess.QUEEN: 9}
        white_mat = black_mat = 0
        for p in self.board.piece_map().values():
            v = values.get(p.piece_type, 0)
            if p.color == chess.WHITE:
                white_mat += v
            else:
                black_mat += v
        return (white_mat - black_mat) if self.playing_as == chess.WHITE else (black_mat - white_mat)

    # ── Display ────────────────────────────────────────────────────────

    def render_board(self, highlight_from=None, highlight_to=None):
        lines = ["",
                 "     a   b   c   d   e   f   g   h",
                 "   ╔═══╤═══╤═══╤═══╤═══╤═══╤═══╤═══╗"]
        ranks = range(7, -1, -1) if self.playing_as == chess.WHITE else range(8)
        files_list = list(range(8) if self.playing_as == chess.WHITE else range(7, -1, -1))
        last_file = files_list[-1]
        rank_list = list(ranks)
        for idx, rank in enumerate(rank_list):
            row = f" {rank+1} ║"
            for file in files_list:
                sq = chess.square(file, rank)
                piece = self.board.piece_at(sq)
                is_light = (rank + file) % 2 == 1
                if sq == highlight_from:
                    bg = "\033[43m"
                elif sq == highlight_to:
                    bg = "\033[42m"
                elif is_light:
                    bg = WHITE_BG
                else:
                    bg = DARK_BG
                if piece:
                    symbol = UNICODE_PIECES.get((piece.piece_type, piece.color), "?")
                    fg = WHITE_FG if piece.color == chess.WHITE else BLACK_FG
                    row += f"{bg}{fg} {symbol} {RESET}"
                else:
                    row += f"{bg}   {RESET}"
                if file != last_file:
                    row += "│"
            row += f"║ {rank+1}"
            lines.append(row)
            if idx < len(rank_list) - 1:
                lines.append("   ╟───┼───┼───┼───┼───┼───┼───┼───╢")
        lines += ["   ╚═══╧═══╧═══╧═══╧═══╧═══╧═══╧═══╝",
                   "     a   b   c   d   e   f   g   h", ""]
        return "\n".join(lines)

    def eval_bar(self, score, width=30):
        if score.is_mate():
            m = score.mate()
            return f"{GREEN}{'█' * width} MATE in {m}{RESET}" if m > 0 else f"{RED}{'░' * width} MATED in {abs(m)}{RESET}"
        cp = score.score() or 0
        clamped = max(-1000, min(1000, cp))
        bar_pos = max(0, min(width, int((clamped + 1000) / 2000 * width)))
        bar = f"{GREEN}{'█' * bar_pos}{RESET}{DIM}{'░' * (width - bar_pos)}{RESET}"
        if cp > 300:     label = f"{GREEN}{BOLD}+{cp/100:.2f} (winning){RESET}"
        elif cp > 100:   label = f"{GREEN}+{cp/100:.2f} (better){RESET}"
        elif cp > 30:    label = f"{GREEN}+{cp/100:.2f} (slight edge){RESET}"
        elif cp >= -30:  label = f"{YELLOW}{cp/100:+.2f} (equal){RESET}"
        elif cp >= -100: label = f"{RED}{cp/100:+.2f} (slightly worse){RESET}"
        elif cp >= -300: label = f"{RED}{cp/100:+.2f} (worse){RESET}"
        else:            label = f"{RED}{BOLD}{cp/100:+.2f} (losing){RESET}"
        return f"{bar} {label}"

    def wdl_str(self, wdl):
        if wdl is None:
            return ""
        w, d, l = wdl
        total = w + d + l
        if total == 0:
            return ""
        return (f"  {GREEN}Win: {w/total*100:.1f}%{RESET}  "
                f"{YELLOW}Draw: {d/total*100:.1f}%{RESET}  "
                f"{RED}Loss: {l/total*100:.1f}%{RESET}")

    def describe_move(self, move):
        piece = self.board.piece_at(move.from_square)
        captured = self.board.piece_at(move.to_square)
        from_sq = chess.square_name(move.from_square)
        to_sq = chess.square_name(move.to_square)
        piece_name = PIECE_NAMES.get(piece.piece_type, "?") if piece else "?"
        if self.board.is_castling(move):
            side = "kingside (O-O)" if self.board.is_kingside_castling(move) else "queenside (O-O-O)"
            return f"Castle {side} — King {from_sq}→{to_sq}, Rook moves inside"
        desc = f"{piece_name} {from_sq} → {to_sq}"
        if captured:
            desc += f" ×{PIECE_NAMES.get(captured.piece_type, '?')}"
        if self.board.is_en_passant(move):
            desc += " (en passant)"
        if move.promotion:
            desc += f" ={PIECE_NAMES.get(move.promotion, '?')}"
        self.board.push(move)
        if self.board.is_checkmate():
            desc += " CHECKMATE!"
        elif self.board.is_check():
            desc += " CHECK!"
        self.board.pop()
        return desc

    def human_instruction(self, move):
        """Return a plain-English instruction like 'Move your Pawn on e2 to e4'."""
        piece = self.board.piece_at(move.from_square)
        from_sq = chess.square_name(move.from_square)
        to_sq = chess.square_name(move.to_square)
        piece_name = PIECE_NAMES.get(piece.piece_type, "?") if piece else "?"

        if self.board.is_castling(move):
            if self.board.is_kingside_castling(move):
                return "Castle kingside — move your King two squares right"
            else:
                return "Castle queenside — move your King two squares left"

        if self.board.is_en_passant(move):
            instr = f"Move your {piece_name} on {from_sq} to {to_sq}, capturing their Pawn en passant"
        elif self.board.piece_at(move.to_square):
            captured = self.board.piece_at(move.to_square)
            cap_name = PIECE_NAMES.get(captured.piece_type, "piece")
            instr = f"Move your {piece_name} on {from_sq} to {to_sq}, capturing their {cap_name}"
        else:
            instr = f"Move your {piece_name} on {from_sq} to {to_sq}"
        if move.promotion:
            promo_name = PIECE_NAMES.get(move.promotion, "Queen")
            instr += f", then promote to {promo_name}"

        self.board.push(move)
        if self.board.is_checkmate():
            instr += " — CHECKMATE!"
        elif self.board.is_check():
            instr += " — puts their King in CHECK!"
        self.board.pop()
        return instr

    def _count_undeveloped(self):
        """Count minor pieces still on their starting squares."""
        back_rank = 0 if self.playing_as == chess.WHITE else 7
        starting = {
            chess.square(1, back_rank): chess.KNIGHT,
            chess.square(6, back_rank): chess.KNIGHT,
            chess.square(2, back_rank): chess.BISHOP,
            chess.square(5, back_rank): chess.BISHOP,
        }
        count = 0
        for sq, ptype in starting.items():
            p = self.board.piece_at(sq)
            if p and p.color == self.playing_as and p.piece_type == ptype:
                count += 1
        return count

    def _center_control(self):
        """Return (our, their) count of center square control (d4,d5,e4,e5)."""
        center = [chess.D4, chess.D5, chess.E4, chess.E5]
        ours = theirs = 0
        for sq in center:
            our_att = len(self.board.attackers(self.playing_as, sq))
            their_att = len(self.board.attackers(not self.playing_as, sq))
            # Occupying the center also counts
            p = self.board.piece_at(sq)
            if p and p.color == self.playing_as:
                our_att += 1
            elif p and p.color != self.playing_as:
                their_att += 1
            ours += our_att
            theirs += their_att
        return ours, theirs

    def _king_safety_score(self):
        """Rough king safety: count attackers near our king. Higher = more danger."""
        king_sq = self.board.king(self.playing_as)
        if king_sq is None:
            return 0
        danger = 0
        # Check squares around king
        king_file = chess.square_file(king_sq)
        king_rank = chess.square_rank(king_sq)
        for df in range(-2, 3):
            for dr in range(-2, 3):
                f, r = king_file + df, king_rank + dr
                if 0 <= f <= 7 and 0 <= r <= 7:
                    sq = chess.square(f, r)
                    attackers = self.board.attackers(not self.playing_as, sq)
                    danger += len(attackers)
        return danger

    def _find_passed_pawns(self, color):
        """Find passed pawns for the given color."""
        passed = []
        opp = not color
        for sq in self.board.pieces(chess.PAWN, color):
            file = chess.square_file(sq)
            rank = chess.square_rank(sq)
            is_passed = True
            # Check if any opponent pawn can block or capture on this file or adjacent
            for f in range(max(0, file - 1), min(8, file + 2)):
                for opp_sq in self.board.pieces(chess.PAWN, opp):
                    opp_rank = chess.square_rank(opp_sq)
                    opp_file = chess.square_file(opp_sq)
                    if opp_file == f:
                        if color == chess.WHITE and opp_rank > rank:
                            is_passed = False
                        elif color == chess.BLACK and opp_rank < rank:
                            is_passed = False
            if is_passed:
                passed.append(sq)
        return passed

    def get_position_assessment(self):
        """Return a detailed position assessment with multiple factors."""
        phase = self.get_game_phase()
        move_num = self.board.fullmove_number
        mat = self.get_material_balance()
        can_castle = self.board.has_castling_rights(self.playing_as)
        undeveloped = self._count_undeveloped()
        our_center, their_center = self._center_control()
        king_danger = self._king_safety_score()
        our_passed = self._find_passed_pawns(self.playing_as)
        their_passed = self._find_passed_pawns(not self.playing_as)

        lines = []
        lines.append(f"  {CYAN}{BOLD}--- Position Assessment ({phase}, Move {move_num}) ---{RESET}")

        # Material
        if mat > 0:
            lines.append(f"  {GREEN}  + You're up {mat} point{'s' if mat != 1 else ''} of material{RESET}")
        elif mat < 0:
            lines.append(f"  {RED}  - You're down {abs(mat)} point{'s' if abs(mat) != 1 else ''} of material{RESET}")
        else:
            lines.append(f"  {YELLOW}  = Material is even{RESET}")

        # Center control
        if our_center > their_center + 3:
            lines.append(f"  {GREEN}  + You dominate the center{RESET}")
        elif their_center > our_center + 3:
            lines.append(f"  {RED}  - Opponent controls the center — fight back!{RESET}")

        # Development (opening/early middlegame)
        if phase == "Opening" or (phase == "Middlegame" and move_num <= 15):
            if undeveloped >= 3:
                lines.append(f"  {RED}  - {undeveloped} minor pieces still undeveloped — get them out!{RESET}")
            elif undeveloped == 2:
                lines.append(f"  {YELLOW}  ~ 2 minor pieces still on back rank — keep developing{RESET}")
            elif undeveloped == 1:
                lines.append(f"  {YELLOW}  ~ 1 minor piece left to develop{RESET}")
            else:
                lines.append(f"  {GREEN}  + All minor pieces developed{RESET}")

        # Castling
        if phase == "Opening":
            if can_castle:
                if move_num >= 5:
                    lines.append(f"  {YELLOW}  ! Castle soon to protect your King{RESET}")
                else:
                    lines.append(f"  {DIM}  ~ Castling still available{RESET}")

        # King safety
        if king_danger >= 15:
            lines.append(f"  {RED}  ! Your King is exposed — be careful!{RESET}")
        elif king_danger >= 10 and phase != "Endgame":
            lines.append(f"  {YELLOW}  ~ Some pressure on your King{RESET}")

        # Passed pawns
        if our_passed:
            names = ", ".join(chess.square_name(sq) for sq in our_passed)
            lines.append(f"  {GREEN}  + You have passed pawn{'s' if len(our_passed) > 1 else ''}: {names}{RESET}")
        if their_passed:
            names = ", ".join(chess.square_name(sq) for sq in their_passed)
            lines.append(f"  {RED}  - Opponent has passed pawn{'s' if len(their_passed) > 1 else ''}: {names}{RESET}")

        # Phase-specific strategy
        lines.append(f"  {CYAN}{BOLD}  Strategy:{RESET}", )
        if phase == "Opening":
            if can_castle and undeveloped >= 2:
                lines.append(f"  {CYAN}  Develop your pieces and castle as soon as possible.{RESET}")
            elif can_castle:
                lines.append(f"  {CYAN}  You're nearly developed — castle and start your middlegame plan.{RESET}")
            elif undeveloped >= 2:
                lines.append(f"  {CYAN}  Finish developing your pieces before attacking.{RESET}")
            else:
                lines.append(f"  {CYAN}  Good development! Look to seize the initiative.{RESET}")
        elif phase == "Middlegame":
            if mat >= 3:
                lines.append(f"  {CYAN}  You're ahead — trade pieces to simplify into a winning endgame.{RESET}")
            elif mat <= -3:
                lines.append(f"  {CYAN}  Down material — look for tactical shots and avoid trades.{RESET}")
            elif king_danger >= 12:
                lines.append(f"  {CYAN}  Prioritize King safety, then look for counterplay.{RESET}")
            elif our_center > their_center + 2:
                lines.append(f"  {CYAN}  You control the center — use it to launch an attack!{RESET}")
            else:
                lines.append(f"  {CYAN}  Improve your piece activity and create threats.{RESET}")
        else:  # Endgame
            if mat >= 3:
                if our_passed:
                    lines.append(f"  {CYAN}  Push your passed pawns with King support to promote!{RESET}")
                else:
                    lines.append(f"  {CYAN}  Activate your King and create a passed pawn to win.{RESET}")
            elif mat <= -3:
                lines.append(f"  {CYAN}  Try to blockade their pawns and seek drawing chances.{RESET}")
            elif our_passed:
                lines.append(f"  {CYAN}  Advance your passed pawns — they're your winning chance!{RESET}")
            elif their_passed:
                lines.append(f"  {CYAN}  Stop their passed pawns! Blockade with a piece on their path.{RESET}")
            else:
                lines.append(f"  {CYAN}  Activate your King aggressively and create a passed pawn.{RESET}")

        return "\n".join(lines)

    def get_move_reason(self, move, score, results):
        """Explain WHY the best move is good based on position context."""
        piece = self.board.piece_at(move.from_square)
        phase = self.get_game_phase()
        reasons = []

        # Checkmate / check
        self.board.push(move)
        if self.board.is_checkmate():
            self.board.pop()
            return f"{MAGENTA}This is CHECKMATE — the game is over!{RESET}"
        gives_check = self.board.is_check()
        self.board.pop()

        if gives_check:
            reasons.append("puts the opponent in check")

        # Castling
        if self.board.is_castling(move):
            reasons.append("secures your King and connects your Rooks")

        # Captures (handle en passant separately)
        if self.board.is_en_passant(move):
            reasons.append("captures their Pawn en passant")
        elif self.board.piece_at(move.to_square):
            captured = self.board.piece_at(move.to_square)
            cap_name = PIECE_NAMES.get(captured.piece_type, "piece")
            piece_vals = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
            their_val = piece_vals.get(captured.piece_type, 0)
            our_val = piece_vals.get(piece.piece_type, 0) if piece else 0
            if their_val > our_val:
                reasons.append(f"wins material (their {cap_name} is worth more)")
            elif their_val == our_val:
                reasons.append(f"trades off a {cap_name}")
            else:
                reasons.append(f"captures their {cap_name}")

        # Development moves in opening
        if phase == "Opening" and piece:
            if piece.piece_type in (chess.KNIGHT, chess.BISHOP):
                back_rank = 0 if self.playing_as == chess.WHITE else 7
                if chess.square_rank(move.from_square) == back_rank:
                    reasons.append("develops a piece toward the center")
            if piece.piece_type == chess.PAWN:
                to_file = chess.square_file(move.to_square)
                to_rank = chess.square_rank(move.to_square)
                if to_file in (3, 4) and to_rank in (3, 4):
                    reasons.append("controls the center")

        # Center control
        to_sq = move.to_square
        center = {chess.D4, chess.D5, chess.E4, chess.E5}
        extended_center = {chess.C3, chess.C4, chess.C5, chess.C6,
                          chess.D3, chess.D6, chess.E3, chess.E6,
                          chess.F3, chess.F4, chess.F5, chess.F6}
        if to_sq in center and not captured and piece and piece.piece_type != chess.KING:
            if "center" not in " ".join(reasons):
                reasons.append("plants a piece in the center")
        elif to_sq in extended_center and piece and piece.piece_type in (chess.KNIGHT, chess.BISHOP):
            if "center" not in " ".join(reasons):
                reasons.append("moves to an active central square")

        # Pawn promotion
        if move.promotion:
            promo_name = PIECE_NAMES.get(move.promotion, "Queen")
            reasons.append(f"promotes to a {promo_name}!")

        # Endgame King activation
        if phase == "Endgame" and piece and piece.piece_type == chess.KING:
            reasons.append("activates your King (critical in endgames)")

        # Passed pawn push
        if piece and piece.piece_type == chess.PAWN and phase in ("Middlegame", "Endgame"):
            passed = self._find_passed_pawns(self.playing_as)
            if move.from_square in passed:
                reasons.append("advances your passed pawn closer to promotion")

        # Engine says it's winning
        if score.is_mate():
            m = score.mate()
            if m > 0:
                reasons.append(f"leads to forced checkmate in {m} moves")
        elif not reasons:
            cp = score.score() or 0
            if cp > 200:
                reasons.append("maintains your winning advantage")
            elif cp > 50:
                reasons.append("keeps a solid edge")
            elif cp >= -50:
                reasons.append("maintains the balance")
            else:
                reasons.append("is the best defensive option")

        if not reasons:
            return f"{CYAN}Best engine move at this depth.{RESET}"
        return f"{CYAN}Why: {'; '.join(reasons)}.{RESET}"

    def format_pv(self, pv, max_moves=PV_DEPTH_DISPLAY):
        temp = self.board.copy()
        parts = []
        for i, move in enumerate(pv[:max_moves]):
            if temp.turn == chess.WHITE:
                parts.append(f"{temp.fullmove_number}.")
            elif i == 0:
                parts.append(f"{temp.fullmove_number}...")
            parts.append(temp.san(move))
            temp.push(move)
        return " ".join(parts)

    def render_header(self):
        mn = self.board.fullmove_number
        color = "WHITE" if self.playing_as == chess.WHITE else "BLACK"
        phase = self.get_game_phase()
        mat = self.get_material_balance()
        mat_str = f"+{mat}" if mat > 0 else str(mat)
        detect = "AUTO" if self.auto_detect else "MANUAL"
        return "\n".join([
            CLEAR,
            f"{BOLD}╔══════════════════════════════════════════════════════════╗{RESET}",
            f"{BOLD}║  ♚ CHESS ADVISOR — Stockfish 18 · MAXIMUM STRENGTH ♚   ║{RESET}",
            f"{BOLD}╠══════════════════════════════════════════════════════════╣{RESET}",
            f"{BOLD}║{RESET}  {ENGINE_THREADS} cores │ 2GB hash │ NNUE │ Detection: {detect:6s}       {BOLD}║{RESET}",
            f"{BOLD}╠══════════════════════════════════════════════════════════╣{RESET}",
            f"{BOLD}║{RESET}  Playing: {BOLD}{color}{RESET}  │  Move: {mn}  │  Phase: {phase}  │  Material: {mat_str:>3}  {BOLD}║{RESET}",
            f"{BOLD}╚══════════════════════════════════════════════════════════╝{RESET}",
        ])

    # ── Move parsing ───────────────────────────────────────────────────

    def parse_move(self, move_str):
        move_str = move_str.strip()
        try:
            return self.board.parse_san(move_str)
        except (chess.InvalidMoveError, chess.IllegalMoveError, chess.AmbiguousMoveError):
            pass
        try:
            m = chess.Move.from_uci(move_str)
            if m in self.board.legal_moves:
                return m
        except (chess.InvalidMoveError, ValueError):
            pass
        for san_str, variants in [("O-O", ("OO", "0-0", "O-O", "0O")),
                                   ("O-O-O", ("OOO", "0-0-0", "O-O-O", "00O"))]:
            if move_str.upper() in variants:
                try:
                    return self.board.parse_san(san_str)
                except (chess.InvalidMoveError, chess.IllegalMoveError):
                    pass
        return None

    # ── PGN ────────────────────────────────────────────────────────────

    def export_pgn(self):
        self.game_pgn.headers["Event"] = "Chess Advisor Session"
        if self.playing_as == chess.WHITE:
            self.game_pgn.headers["White"] = self.player_name
            self.game_pgn.headers["Black"] = "Computer"
        else:
            self.game_pgn.headers["White"] = "Computer"
            self.game_pgn.headers["Black"] = self.player_name
        self.game_pgn.headers["Result"] = self.board.result() if self.board.is_game_over() else "*"
        return self.game_pgn.accept(chess.pgn.StringExporter(headers=True, variations=False, comments=True))

    def save_pgn(self):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(base_dir, "game.pgn")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(self.export_pgn())
        return path

    # ── Auto-import from Chess.app ────────────────────────────────────

    def _get_chessapp_pgn(self):
        """Try to get the current game PGN from Apple Chess.app via AppleScript."""
        try:
            # Chess.app stores its current game; we can grab it via temp PGN export
            # First try: ask Chess.app for the game document's file path
            result = subprocess.run(
                ['osascript', '-e',
                 'tell application "Chess" to return name of front document'],
                capture_output=True, text=True, timeout=3
            )
            if result.returncode != 0:
                return None

            # Use AppleScript to copy the game as PGN to a temp file
            fd, tmp = tempfile.mkstemp(suffix='.pgn', prefix='chess_import_')
            os.close(fd)
            try:
                # Chess.app supports "save" via AppleScript — save current game as PGN
                script = f'''
                    tell application "Chess"
                        set gameDoc to front document
                        save gameDoc in POSIX file "{tmp}"
                    end tell
                '''
                result = subprocess.run(
                    ['osascript', '-e', script],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0 and os.path.getsize(tmp) > 0:
                    with open(tmp, 'r') as f:
                        pgn_text = f.read()
                    os.unlink(tmp)
                    if pgn_text.strip():
                        return pgn_text
                elif os.path.exists(tmp):
                    os.unlink(tmp)
            except Exception:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

            # Fallback: check if Chess.app has a recently saved file
            # Look for the most recent .pgn in the default Chess.app save location
            chess_dir = os.path.expanduser("~/Documents")
            if os.path.isdir(chess_dir):
                pgn_files = []
                for f in os.listdir(chess_dir):
                    if f.endswith('.pgn'):
                        full = os.path.join(chess_dir, f)
                        pgn_files.append((os.path.getmtime(full), full))
                if pgn_files:
                    pgn_files.sort(reverse=True)
                    newest_time, newest_file = pgn_files[0]
                    # Only use if modified in the last 5 minutes (likely current game)
                    if time.time() - newest_time < 300:
                        with open(newest_file, 'r') as f:
                            return f.read()
        except Exception:
            pass
        return None

    # ── Turn handlers ──────────────────────────────────────────────────

    def my_turn(self):
        """Analyze position and recommend the best move."""
        out = [f"  {GREEN}{BOLD}══════ YOUR TURN ══════{RESET}"]

        critical = self.is_critical_position()
        mpv = CRITICAL_MULTIPV if critical else NORMAL_MULTIPV

        sys.stdout.write(f"  {DIM}Deep analysis ({ENGINE_THREADS} threads, adaptive)...{RESET}")
        sys.stdout.flush()
        start = time.time()
        results, _ = self.analyze_position(multipv=mpv)
        elapsed = time.time() - start
        sys.stdout.write(f"\r{'':60}\r")
        sys.stdout.flush()

        if critical:
            out.append(f"  {RED}{BOLD}⚠ CRITICAL POSITION — Extended analysis ({elapsed:.1f}s){RESET}")
        else:
            out.append(f"  {DIM}Analysis complete ({elapsed:.1f}s){RESET}")

        best = results[0]
        score = best["score"].white() if self.playing_as == chess.WHITE else best["score"].black()
        out.append(f"\n  {BOLD}Eval:{RESET} {self.eval_bar(score)}")

        wdl = best.get("wdl")
        if wdl:
            oriented = wdl.white() if self.playing_as == chess.WHITE else wdl.black()
            out.append(self.wdl_str(oriented))

        threats = self.extract_threats_from_pv(results)
        if threats:
            out.append(f"\n  {RED}{BOLD}Threats to watch:{RESET}")
            for ttype, tmove, tpiece in threats[:3]:
                if ttype == "capture" and tpiece:
                    out.append(f"    {YELLOW}⚠ {PIECE_NAMES.get(tpiece.piece_type, '?')} on {chess.square_name(tmove.to_square)} attacked{RESET}")
                elif ttype == "check_after":
                    out.append(f"    {YELLOW}⚠ Check threat after {chess.square_name(tmove.to_square)}{RESET}")

        out.append(f"\n  {BOLD}{'─'*52}{RESET}")
        out.append(f"  {BOLD}Top {len(results)} candidate moves:{RESET}\n")

        best_move = results[0]["pv"][0]
        best_san = self.board.san(best_move)
        best_desc = None
        best_instruction = None

        for i, info in enumerate(results):
            move = info["pv"][0]
            mv_score = info["score"].white() if self.playing_as == chess.WHITE else info["score"].black()
            desc = self.describe_move(move)
            instruction = self.human_instruction(move)
            san = self.board.san(move)
            pv_str = self.format_pv(info["pv"])
            depth = info.get("depth", "?")
            if mv_score.is_mate():
                m = mv_score.mate()
                sc = f"M{abs(m)}" if m > 0 else f"-M{abs(m)}"
            else:
                sc = f"{(mv_score.score() or 0)/100:+.2f}"
            if i == 0:
                best_desc = desc
                best_instruction = instruction
                out.append(f"  {GREEN}{BOLD}▶ #{i+1} {san:8s} [{sc:>7}] d{depth}{RESET}")
                out.append(f"    {GREEN}{instruction}{RESET}")
                out.append(f"    {DIM}Line: {pv_str}{RESET}")
            else:
                out.append(f"  {DIM}  #{i+1} {san:8s} [{sc:>7}] d{depth} — {instruction}{RESET}")
                if len(info["pv"]) > 1:
                    out.append(f"    {DIM}   Line: {pv_str}{RESET}")

        out.append(f"\n  {BOLD}{'─'*52}{RESET}")
        out.append(f"  {GREEN}{BOLD}➤ PLAY: {best_san}{RESET}")
        out.append(f"  {GREEN}{BOLD}  {best_instruction}{RESET}")
        out.append(f"  {self.get_move_reason(best_move, score, results)}")
        out.append(f"  {BOLD}{'─'*52}{RESET}")

        # Position assessment with strategy
        out.append(self.get_position_assessment())
        print("\n".join(out))

        eval_before = score

        while True:
            prompt = f"\n  Make this move in Chess.app, then press Enter"
            if not self.auto_detect:
                prompt = f"\n  Your move (Enter={best_san}, 'q'=quit, 'undo', 'save')"
            user_input = input(f"{prompt}: ").strip()

            if user_input == "":
                self.board.push(best_move)
                self.pgn_node = self.pgn_node.add_variation(best_move)
                self.last_eval = eval_before
                # Take reference screenshot AFTER our move for the next detection cycle
                if self.auto_detect:
                    time.sleep(0.3)  # Let Chess.app animate our move
                    self.watcher.take_reference()
                return eval_before
            elif user_input.lower() == 'q':
                raise KeyboardInterrupt
            elif user_input.lower() == 'undo':
                if len(self.board.move_stack) >= 2:
                    self.board.pop()
                    self.board.pop()
                    print(f"  {YELLOW}Undid last 2 moves.{RESET}")
                    return None
                else:
                    print(f"  {RED}Nothing to undo.{RESET}")
            elif user_input.lower() == 'save':
                print(f"  {GREEN}Game saved to {self.save_pgn()}{RESET}")
            elif user_input.lower() == 'pgn':
                print(f"\n{self.export_pgn()}\n")
            elif user_input.lower() == 'fen':
                print(f"\n  FEN: {self.board.fen()}\n")
            else:
                parsed = self.parse_move(user_input)
                if parsed:
                    self.board.push(parsed)
                    self.pgn_node = self.pgn_node.add_variation(parsed)
                    self.last_eval = eval_before
                    if self.auto_detect:
                        time.sleep(0.3)
                        self.watcher.take_reference()
                    return eval_before
                else:
                    legal = [self.board.san(m) for m in self.board.legal_moves]
                    close = [m for m in legal if m.lower().startswith(user_input.lower()[:2])]
                    hint = f" Did you mean: {', '.join(close[:5])}?" if close else ""
                    print(f"  {RED}Invalid move.{hint}{RESET}")

    def opponent_turn(self):
        """Wait for opponent's move — auto-detect or manual input."""
        out = [f"  {YELLOW}{BOLD}══════ OPPONENT'S TURN ══════{RESET}"]

        sys.stdout.write(f"  {DIM}Predicting opponent's move...{RESET}")
        sys.stdout.flush()
        results, _ = self.analyze_position(multipv=3, think_time=QUICK_TIME)
        sys.stdout.write(f"\r{'':50}\r")
        sys.stdout.flush()

        score = results[0]["score"].white() if self.playing_as == chess.WHITE else results[0]["score"].black()
        out.append(f"\n  {BOLD}Eval:{RESET} {self.eval_bar(score)}")

        wdl = results[0].get("wdl")
        if wdl:
            oriented = wdl.white() if self.playing_as == chess.WHITE else wdl.black()
            out.append(self.wdl_str(oriented))

        out.append(f"\n  {BOLD}Predicted opponent moves:{RESET}")
        for i, info in enumerate(results[:3]):
            move = info["pv"][0]
            san = self.board.san(move)
            pv_str = self.format_pv(info["pv"])
            if i == 0:
                out.append(f"    {CYAN}Most likely: {san}{RESET}")
                out.append(f"    {DIM}Continuation: {pv_str}{RESET}")
            else:
                out.append(f"    {DIM}Also possible: {san}{RESET}")

        print("\n".join(out))

        # ── Auto-detect mode ──────────────────────────────────────────
        if self.auto_detect:
            print(f"\n  {CYAN}{BOLD}Watching Chess.app for opponent's move...{RESET}")
            detected_move = self.watcher.wait_for_opponent_move(self.board, self.playing_as)

            if detected_move:
                san = self.board.san(detected_move)
                desc = self.describe_move(detected_move)
                self.board.push(detected_move)
                self.pgn_node = self.pgn_node.add_variation(detected_move)
                print(f"\r  {BOLD}Opponent played: {san}{RESET} — {desc}")
                return score
            else:
                # Fall back to manual for this move
                print(f"\r  {YELLOW}Could not auto-detect — enter move manually{RESET}")

        # ── Manual input (fallback or default) ────────────────────────
        while True:
            user_input = input(f"\n  Opponent's move: ").strip()
            if user_input.lower() == 'q':
                raise KeyboardInterrupt
            elif user_input.lower() == 'undo':
                if len(self.board.move_stack) >= 2:
                    self.board.pop()
                    self.board.pop()
                    print(f"  {YELLOW}Undid last 2 moves.{RESET}")
                    return None
                else:
                    print(f"  {RED}Nothing to undo.{RESET}")
            elif user_input.lower() == 'save':
                print(f"  {GREEN}Game saved to {self.save_pgn()}{RESET}")
            else:
                parsed = self.parse_move(user_input)
                if parsed:
                    san = self.board.san(parsed)
                    self.board.push(parsed)
                    self.pgn_node = self.pgn_node.add_variation(parsed)
                    print(f"  Opponent played: {BOLD}{san}{RESET}")
                    return score
                else:
                    legal = [self.board.san(m) for m in self.board.legal_moves]
                    close = [m for m in legal if m.lower().startswith(user_input.lower()[:2])]
                    hint = f" Did you mean: {', '.join(close[:5])}?" if close else ""
                    print(f"  {RED}Invalid move.{hint}{RESET}")

    # ── Main loop ──────────────────────────────────────────────────────

    def run(self):
        print(f"{CLEAR}", end="")
        banner = [
            f"{BOLD}╔══════════════════════════════════════════════════════════╗{RESET}",
            f"{BOLD}║  ♚ CHESS ADVISOR — Stockfish 18 · MAXIMUM STRENGTH ♚   ║{RESET}",
            f"{BOLD}╠══════════════════════════════════════════════════════════╣{RESET}",
            f"{BOLD}║{RESET}                                                          {BOLD}║{RESET}",
            f"{BOLD}║{RESET}  The world's strongest chess engine, tuned to the max.   {BOLD}║{RESET}",
            f"{BOLD}║{RESET}  {ENGINE_THREADS} CPU threads · 2GB hash · NNUE neural net · Adaptive {BOLD}║{RESET}",
            f"{BOLD}║{RESET}  Screen capture auto-detection of opponent moves          {BOLD}║{RESET}",
            f"{BOLD}║{RESET}  Just play your move — the advisor sees the rest!         {BOLD}║{RESET}",
            f"{BOLD}║{RESET}                                                          {BOLD}║{RESET}",
            f"{BOLD}╚══════════════════════════════════════════════════════════╝{RESET}",
            "",
        ]
        print("\n".join(banner))

        # Player name
        name_input = input(f"  Your name (Enter=Player): ").strip()[:50]
        if name_input:
            self.player_name = name_input

        # Color selection
        while True:
            color_input = input(f"  Are you playing as White or Black? (w/b): ").strip().lower()
            if color_input in ('w', 'white'):
                self.playing_as = chess.WHITE
                break
            elif color_input in ('b', 'black'):
                self.playing_as = chess.BLACK
                break
            print(f"  {RED}Enter 'w' or 'b'{RESET}")

        # Auto-detect current game from Chess.app, or manual resume
        game_loaded = False
        pgn_text = self._get_chessapp_pgn()
        if pgn_text:
            try:
                pgn_io = io.StringIO(pgn_text)
                game = chess.pgn.read_game(pgn_io)
                if game:
                    move_count = 0
                    for move in game.mainline_moves():
                        self.board.push(move)
                        self.pgn_node = self.pgn_node.add_variation(move)
                        move_count += 1
                    if move_count > 0:
                        game_loaded = True
                        print(f"\n  {GREEN}{BOLD}Auto-imported {move_count} moves from Chess.app!{RESET}")
                        print(f"  {DIM}Game at move {self.board.fullmove_number}, "
                              f"{'White' if self.board.turn == chess.WHITE else 'Black'} to move{RESET}")
            except Exception:
                pass

        if not game_loaded:
            print(f"\n  {DIM}Resume a game? Enter moves (space-separated) or press Enter for new game.{RESET}")
            existing = input(f"  Moves: ").strip()
            if existing:
                try:
                    pgn_io = io.StringIO(existing)
                    game = chess.pgn.read_game(pgn_io)
                    if game:
                        for move in game.mainline_moves():
                            self.board.push(move)
                            self.pgn_node = self.pgn_node.add_variation(move)
                    else:
                        raise ValueError()
                except Exception:
                    for m in existing.split():
                        if re.match(r'^\d+\.+$', m):
                            continue
                        parsed = self.parse_move(m)
                        if parsed:
                            self.board.push(parsed)
                            self.pgn_node = self.pgn_node.add_variation(parsed)
                        else:
                            safe_m = re.sub(r'[^\x20-\x7e]', '', m)
                            print(f"  {RED}Couldn't parse '{safe_m}', stopping here.{RESET}")
                            break

        # Initialize auto-detection
        print()
        if VISION_AVAILABLE:
            print(f"  {DIM}Connecting to Chess.app for auto-detection...{RESET}")
            self.auto_detect = self.watcher.initialize()
            if not self.auto_detect:
                print(f"  {DIM}Falling back to manual mode (type opponent moves).{RESET}")
        else:
            print(f"  {YELLOW}Auto-detect unavailable. Install: pip3 install Pillow{RESET}")
            print(f"  {DIM}Using manual mode (type opponent moves).{RESET}")

        # Start engine
        print(f"\n  {DIM}Initializing Stockfish 18 (max settings)...{RESET}")
        self.start_engine()
        print(f"  {GREEN}{BOLD}Engine ready — {ENGINE_THREADS} threads, 2GB hash, NNUE active{RESET}")

        mode_str = f"{GREEN}AUTO-DETECT{RESET}" if self.auto_detect else f"{YELLOW}MANUAL{RESET}"
        print(f"  Opponent detection: {mode_str}\n")

        try:
            while not self.board.is_game_over():
                is_my_turn = (self.board.turn == self.playing_as)
                hl_from = hl_to = None
                if self.board.move_stack:
                    last = self.board.peek()
                    hl_from, hl_to = last.from_square, last.to_square

                print(self.render_header() + self.render_board(
                    highlight_from=hl_from, highlight_to=hl_to))

                result = self.my_turn() if is_my_turn else self.opponent_turn()
                if result is None:
                    continue

            # Game over
            hl_from = hl_to = None
            if self.board.move_stack:
                last = self.board.peek()
                hl_from, hl_to = last.from_square, last.to_square
            print(self.render_header() + self.render_board(
                highlight_from=hl_from, highlight_to=hl_to))

            print(f"  {BOLD}{'═'*52}{RESET}")
            if self.board.is_checkmate():
                winner_white = self.board.turn == chess.BLACK
                i_won = (winner_white and self.playing_as == chess.WHITE) or \
                        (not winner_white and self.playing_as == chess.BLACK)
                if i_won:
                    print(f"  {GREEN}{BOLD}  CHECKMATE — YOU WIN!  {RESET}")
                else:
                    print(f"  {RED}{BOLD}  CHECKMATE — You lost.  {RESET}")
            elif self.board.is_stalemate():
                print(f"  {YELLOW}Stalemate — draw.{RESET}")
            elif self.board.is_insufficient_material():
                print(f"  {YELLOW}Draw — insufficient material.{RESET}")
            elif self.board.is_fifty_moves():
                print(f"  {YELLOW}Draw — fifty-move rule.{RESET}")
            elif self.board.is_repetition():
                print(f"  {YELLOW}Draw — threefold repetition.{RESET}")
            else:
                print(f"  Game over: {self.board.result()}")
            print(f"  {BOLD}{'═'*52}{RESET}")
            print(f"\n  Game saved to {self.save_pgn()}")

        except KeyboardInterrupt:
            print(f"\n\n  {DIM}Session ended.{RESET}")
            print(f"  Game saved to {self.save_pgn()}\n")
        finally:
            self.stop_engine()


if __name__ == "__main__":
    ChessAdvisor().run()
